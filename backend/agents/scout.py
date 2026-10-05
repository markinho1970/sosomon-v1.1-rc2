"""
Scout Agent — Daily token screening for SoSoMon indexes.

Fonte de dados: SoSoValue SSI + SoDEX (sem CoinGecko).
- SSI constituents são o universo de candidatos (pré-qualificados pela SoSoValue)
- SoDEX enriquece com preços on-chain e momentum para tokens listados
- Blacklist + cooldown filtram tokens indesejados
- Gemini gera rationale baseado nos dados disponíveis
"""

import os
import json
import uuid
import asyncio
from datetime import datetime, timedelta
from typing import List, Dict, Any
from loguru import logger
from services.llm import generate

from database import SessionLocal
from models import AlphaIndex, IndexConstituent, AgentActivityLog, ScoutReport, RebalanceProposal
from services import sosovalue
from services.sosovalue import get_macro_context
from services.sodex import get_all_tickers, get_markets, get_candles


# Tokens permanentemente excluídos de todos os índices.
TOKEN_BLACKLIST_SYMBOLS: set = {
    "FARTCOIN",
    "PIEVERSE",
    "SKYAI",
}

# Tokens âncora permanentes — NUNCA podem ser excluídos pelo Scout.
# São os pilares temáticos de cada índice + token do ecossistema SoSoValue.
# Mudanças nessa lista requerem decisão explícita do founder.
PERMANENT_ANCHORS: set = {
    "MAG7ssi",   # AI & Tech — pilar SSI
    "DEFIssi",   # DeFi Infrastructure — pilar SSI
    "USSIssi",   # Real Assets — pilar SSI
    "WSOSO",     # Ecossistema SoSoValue — permanente em todas as cestas
}


async def run_all_indexes():
    """Main entry point — roda o Scout para todos os índices ativos.

    Macro context e currency cache são buscados uma única vez e compartilhados
    entre temas para minimizar chamadas à API SoSoValue (20 req/min).
    """
    db = SessionLocal()
    try:
        # Pre-warm currency cache (1 chamada) + macro context (4 chamadas paralelas)
        # Feito aqui para não repetir por tema
        await sosovalue._ensure_currency_cache()
        logger.info("Scout: buscando contexto macro SoSoValue...")
        macro = await get_macro_context()

        indexes = db.query(AlphaIndex).filter(AlphaIndex.is_active == True).all()
        for i, idx in enumerate(indexes):
            if i > 0:
                logger.info("Scout: aguardando 8s entre temas (rate limit SoSoValue)")
                await asyncio.sleep(8)
            await run_scout_for_index(idx.id, idx.theme, db, macro=macro)
    finally:
        db.close()


async def run_scout_for_index(index_id: str, theme: str, db, macro: dict = None):
    """Roda o Scout para um índice: SSI → filtros → rationale → relatório."""
    logger.info(f"Scout [{theme}]: iniciando run index={index_id}")

    # 0. Configuração do índice (número alvo de tokens)
    idx_obj = db.query(AlphaIndex).filter(AlphaIndex.id == index_id).first()
    target_n = int(idx_obj.target_constituents) if idx_obj and idx_obj.target_constituents else 5

    # 1. Contexto macro (compartilhado via run_all_indexes; fallback se chamado isolado)
    if macro is None:
        macro = await get_macro_context()
    sentiment_score = macro["sosovalue_sentiment_score"]

    # 2. Candidatos do SSI com tickers resolvidos (substitui THEME_UNIVERSE + CoinGecko)
    candidates = await sosovalue.get_ssi_candidates_for_theme(theme)
    if not candidates:
        logger.warning(f"Scout [{theme}]: SoSoValue SSI sem dados, abortando")
        return

    logger.info(f"Scout [{theme}]: {len(candidates)} candidatos do SSI: {[c['symbol'] for c in candidates]}")

    # 2b. Enriquecer candidatos com performance real da SoSoValue
    # (marketcap, volume_24h, mcap_rank, roi_7d, roi_30d, roi_3m via klines)
    logger.info(f"Scout [{theme}]: enriquecendo {len(candidates)} candidatos com performance SoSoValue...")
    candidates = await sosovalue.enrich_candidates_with_performance(candidates)
    for c in candidates:
        logger.info(
            f"  {c['symbol']:12} | ssi_w={c.get('ssi_weight',0)*100:.1f}% "
            f"| mcap_rank=#{c.get('mcap_rank',999)} "
            f"| 7d={c.get('roi_7d',0):+.1f}% 30d={c.get('roi_30d',0):+.1f}% 3m={c.get('roi_3m',0):+.1f}%"
        )

    # 3. Notícias temáticas e benchmark
    theme_news = await sosovalue.get_news_for_theme(theme, limit=3)
    news_summary = "; ".join([n.get("title", "") or "" for n in theme_news[:3] if n]) if theme_news else ""

    benchmark = await sosovalue.get_benchmark_for_theme(theme)
    if benchmark:
        logger.info(f"Scout [{theme}]: benchmark {benchmark.get('ssi_ticker')} → 30d: {benchmark.get('roi_1m', benchmark.get('1month_roi', 0))*100:.1f}%")

    # 4. Mercados SoDEX (enriquecimento on-chain)
    sodex_tickers = await get_all_tickers()
    sodex_markets = await get_markets()
    # baseCoin é o campo correto da SoDEX API (não baseCurrency/base)
    sodex_symbols = {m.get("baseCoin", "").upper() for m in sodex_markets if m.get("baseCoin")}
    logger.info(f"Scout [{theme}]: {len(sodex_symbols)} mercados SoDEX disponíveis")

    # 5. Aplica blacklist
    blacklisted = [t for t in candidates if t["symbol"] in TOKEN_BLACKLIST_SYMBOLS]
    if blacklisted:
        logger.info(f"Scout [{theme}]: blacklist removeu {[t['symbol'] for t in blacklisted]}")
    candidates = [t for t in candidates if t["symbol"] not in TOKEN_BLACKLIST_SYMBOLS]

    if not candidates:
        logger.warning(f"Scout [{theme}]: nenhum candidato restou após blacklist")
        return

    # 6. Aplica cooldown pós-ejeção (90 dias a partir de propostas executadas)
    _cutoff = datetime.utcnow() - timedelta(days=90)
    _executed = db.query(RebalanceProposal).filter(
        RebalanceProposal.index_id == index_id,
        RebalanceProposal.status == "executed",
        RebalanceProposal.proposed_at >= _cutoff,
    ).all()
    _ejected_symbols: set = set()
    for _p in _executed:
        for _ch in (_p.changes or []):
            if _ch.get("action") in ("remove", "eject"):
                _sym = (_ch.get("symbol") or "").upper()
                if _sym:
                    _ejected_symbols.add(_sym)
    _current_syms = {
        c.symbol.upper() for c in
        db.query(IndexConstituent).filter(IndexConstituent.index_id == index_id).all()
    }
    _ejected_symbols -= _current_syms
    if _ejected_symbols:
        _cooling = [t["symbol"] for t in candidates if t["symbol"].upper() in _ejected_symbols]
        if _cooling:
            logger.info(f"Scout [{theme}]: cooldown 90d bloqueou {_cooling}")
        candidates = [t for t in candidates if t["symbol"].upper() not in _ejected_symbols]

    # 7. Inicializa campos de mercado
    for token in candidates:
        token.setdefault("current_price_usd", 0.0)
        token.setdefault("volume_24h_usd", 0.0)
        token.setdefault("price_change_7d", 0.0)
        token.setdefault("price_change_30d", 0.0)
        token.setdefault("market_cap_usd", 0.0)
        token.setdefault("on_sodex", False)

    # Mapa de status HALT por baseCoin para filtrar na inclusão
    halt_set = {
        m.get("baseCoin", "").lstrip("v").lstrip("V").upper()
        for m in sodex_markets
        if m.get("status", "").upper() == "HALT"
    }
    logger.debug(f"Scout [{theme}]: tokens HALT no SoDEX = {sorted(halt_set)}")

    # 7b. Tokens da cesta que não vieram do SSI (vXAUt, vBTC, WSOSO etc.)
    # O SSI cobre apenas os tokens que ele próprio indexa — tokens estratégicos
    # escolhidos pelo founder ficam de fora do universo SSI e eram avaliados
    # sem dados históricos. Aqui os adicionamos como candidatos para que o
    # scoring (step 10) use roi_7d/30d/3m reais da SoSoValue, não zero.
    _candidate_syms_set = {c["symbol"].upper() for c in candidates}
    _basket_rows = db.query(IndexConstituent).filter(
        IndexConstituent.index_id == index_id,
        IndexConstituent.in_basket == True,
    ).all()
    _basket_extras = []
    for _bc in _basket_rows:
        _sym_bare = (_bc.symbol[1:] if _bc.symbol.lower().startswith("v") and len(_bc.symbol) > 1 else _bc.symbol).upper()
        if _bc.symbol.upper() in _candidate_syms_set or _sym_bare in _candidate_syms_set or _bc.symbol.upper() in {"USDC", "USDT"}:
            continue
        # Resolve currency_id: remove prefixo 'v' e busca no cache SoSoValue
        _stripped = _bc.symbol[1:] if _bc.symbol.lower().startswith("v") and len(_bc.symbol) > 1 else _bc.symbol
        _cid = sosovalue._CURRENCY_CACHE.get(_stripped.upper(), "")
        _is_halt = _bc.symbol.lstrip("vV").upper() in halt_set
        _basket_extras.append({
            "symbol": _bc.symbol,
            "name": _bc.name or _bc.symbol,
            "currency_id": _cid,
            "ssi_weight": float(_bc.weight or 0) / 100.0,
            "current_price_usd": 0.0,
            "volume_24h_usd": 0.0,
            "price_change_7d": 0.0,
            "price_change_30d": 0.0,
            "market_cap_usd": 0.0,
            "on_sodex": True,
            "is_halt": _is_halt,
            "in_current_basket": True,
        })
    if _basket_extras:
        logger.info(
            f"Scout [{theme}]: {len(_basket_extras)} tokens da cesta fora do SSI — "
            f"buscando klines SoSoValue: {[t['symbol'] for t in _basket_extras]}"
        )
        _basket_extras = await sosovalue.enrich_candidates_with_performance(_basket_extras)
        candidates.extend(_basket_extras)

    # Captura roi_30d do step 7 (SoSoValue klines) — campo definido por enrich_candidates_with_performance.
    # Não usar price_change_30d: esse campo é inicializado em 0.0 e só é atualizado pelo step 8 (SoDEX candles),
    # que pode zerar o valor para mercados com histórico curto. O benchmark também vem da SoSoValue →
    # usar roi_30d (mesma fonte) na proteção garante comparação consistente.
    ssi_fresh_30d: dict = {
        t["symbol"]: t["roi_30d"]
        for t in candidates
        if t.get("roi_30d") is not None
    }

    # 8. Enriquece com dados on-chain do SoDEX
    for token in candidates:
        sym = token["symbol"]
        # Tenta múltiplos aliases: "ETH-USD", "MAG7.ssi-USD" (para MAG7ssi), etc.
        sym_dot = sym.replace("ssi", ".ssi") if sym.endswith("ssi") else sym
        market_key = None
        for candidate_key in [f"{sym}-USD", f"{sym_dot}-USD", sym, sym_dot]:
            if candidate_key in sodex_tickers:
                market_key = candidate_key
                break
        if market_key:
            t = sodex_tickers[market_key]
            token["current_price_usd"] = float(t.get("lastPrice", t.get("price", 0)) or 0)
            token["volume_24h_usd"] = float(t.get("volume24h", t.get("volume", 0)) or 0)
            token["on_sodex"] = True
            token["is_halt"] = sym.upper() in halt_set
            # Momentum 30d via candles SoDEX
            candles = await get_candles(market_key, interval="1D", limit=30)
            if len(candles) >= 2:
                first = float(candles[0].get("close", candles[0].get("c", 0)) or 0)
                last = float(candles[-1].get("close", candles[-1].get("c", 0)) or 0)
                if first > 0:
                    token["price_change_30d"] = round(((last - first) / first) * 100, 2)

    # 9. Constituintes atuais do índice
    current_constituents = db.query(IndexConstituent).filter(
        IndexConstituent.index_id == index_id
    ).all()
    current_symbols = {c.symbol: c for c in current_constituents}

    # Tokens âncora: nunca podem ser removidos pelo Scout
    anchor_symbols = {c.symbol for c in current_constituents if getattr(c, "is_anchor", False)}
    if anchor_symbols:
        logger.info(f"Scout [{theme}]: tokens âncora protegidos (nunca removíveis): {sorted(anchor_symbols)}")

    # 9.5. Correlação entre tokens da cesta atual (detecção de exposição redundante)
    basket_market_keys: dict = {}
    for sym, c in current_symbols.items():
        if not getattr(c, "in_basket", True):
            continue
        sym_dot = sym.replace("ssi", ".ssi") if sym.endswith("ssi") else sym
        for k in [f"{sym}-USD", f"{sym_dot}-USD", sym, sym_dot]:
            if k in sodex_tickers:
                basket_market_keys[sym] = k
                break
    correlation_ctx = await _compute_basket_correlation(basket_market_keys)
    if correlation_ctx:
        logger.info(f"Scout [{theme}]: {correlation_ctx}")

    # 10. Scoring por peso SSI + momentum SoDEX
    scored_tokens = _apply_momentum_scoring(candidates)

    # 11. Rationale AI para top candidatos
    top_candidates = scored_tokens[:12]
    candidates_with_rationale = await _generate_ai_rationale(
        top_candidates, theme, sentiment_score,
        news_context=news_summary,
        benchmark=benchmark,
        correlation_ctx=correlation_ctx,
    )

    # 12. Calcula inclusões / exclusões / mudanças de peso
    benchmark_30d_pct = 0.0
    if benchmark:
        raw = benchmark.get("roi_1m", benchmark.get("1month_roi", 0)) or 0
        benchmark_30d_pct = raw * 100
    inclusions, exclusions, weight_changes = _compute_changes(
        candidates=candidates_with_rationale,
        current_symbols=current_symbols,
        sentiment_score=sentiment_score,
        theme=theme,
        target_n=target_n,
        anchor_symbols=anchor_symbols,
        benchmark_30d=benchmark_30d_pct,
        fresh_30d=ssi_fresh_30d,
    )

    # 13. Salva ScoutReport
    report = ScoutReport(
        index_id=index_id,
        run_at=datetime.utcnow(),
        tokens_screened=len(candidates),
        tokens_qualified=len(candidates),
        inclusions=inclusions,
        exclusions=exclusions,
        weight_changes=weight_changes,
        raw_output=json.dumps(candidates_with_rationale, indent=2),
    )
    db.add(report)

    # 14. Log de atividade
    summary = _build_activity_summary(inclusions, exclusions, weight_changes)
    activity = AgentActivityLog(
        id=str(uuid.uuid4()),
        index_id=index_id,
        agent="scout",
        action="rebalance" if (inclusions or exclusions) else "no_action",
        description=summary,
        timestamp=datetime.utcnow(),
        data={"inclusions": len(inclusions), "exclusions": len(exclusions), "weight_changes": len(weight_changes)},
    )
    db.add(activity)

    # 15. Cria RebalanceProposal quando Scout detecta inclusões ou exclusões
    if inclusions or exclusions:
        # Monta lista de mudanças no formato esperado pelo admin console
        proposal_changes = []
        for inc in inclusions:
            # target_weight é injetado por _compute_changes após normalização SSI com cap
            target_w = inc.get("target_weight", 0)
            proposal_changes.append({
                "action": "add",
                "symbol": inc["symbol"],
                "name": inc.get("name", inc["symbol"]),
                "target_weight": round(float(target_w), 1),
                "rationale": inc.get("rationale", ""),
            })
        for exc in exclusions:
            constituent = current_symbols.get(exc["symbol"])
            proposal_changes.append({
                "action": "remove",
                "symbol": exc["symbol"],
                "old_weight": float(getattr(constituent, "weight", 0) or 0),
                "target_weight": 0,
                "reason": exc.get("reason", ""),
            })

        # Monta análise detalhada em português para tomada de decisão do admin
        def _pct(v):
            if v is None:
                return "n/d"
            return f"{float(v):+.1f}%"

        lines = [
            f"## Análise Scout — {theme.upper()} — {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC",
            "",
            f"**Benchmark 30d:** {_pct(benchmark_30d_pct)}",
            f"**Sentimento macro:** {sentiment_score}/100",
            "",
        ]

        if inclusions:
            lines.append("### Tokens para INCLUSÃO")
            for inc in inclusions:
                cand = next(
                    (c for c in candidates_with_rationale if c.get("symbol") == inc["symbol"]),
                    {},
                )
                lines.append(f"\n**{inc['symbol']}** — {inc.get('name', '')}")
                lines.append(f"- Peso SSI: {cand.get('ssi_weight', 'n/d')}")
                lines.append(f"- Retorno 30d: {_pct(cand.get('roi_30d'))}")
                lines.append(f"- Retorno 7d: {_pct(cand.get('roi_7d'))}")
                lines.append(f"- Momentum score: {cand.get('momentum_score', 'n/d')}")
                lines.append(f"- Rationale: {inc.get('rationale', 'n/d')}")

        if exclusions:
            lines.append("\n### Tokens para EXCLUSÃO")
            for exc in exclusions:
                constituent = current_symbols.get(exc["symbol"])
                cand = next(
                    (c for c in candidates_with_rationale if c.get("symbol") == exc["symbol"]),
                    {},
                )
                curr_w = float(getattr(constituent, "weight", 0) or 0)
                perf_30d = cand.get("roi_30d") or getattr(constituent, "price_change_30d", None)
                lines.append(f"\n**{exc['symbol']}** (peso atual: {curr_w:.1f}%)")
                lines.append(f"- Retorno 30d: {_pct(perf_30d)}")
                lines.append(f"- Motivo: {exc.get('reason', 'n/d')}")
                if perf_30d is not None and benchmark_30d_pct > 0:
                    rel = float(perf_30d) - benchmark_30d_pct
                    lines.append(f"- Performance vs benchmark: {_pct(rel)}")

        lines += [
            "",
            "### Para o Admin — Pontos a avaliar antes de aprovar",
            "1. Os tokens para inclusão estão listados/com liquidez no SoDEX?",
            "2. Os tokens para exclusão têm ordens abertas que precisam ser fechadas?",
            "3. O sentimento macro justifica a mudança neste momento?",
            "4. Verificar se as exclusões superam o benchmark antes de remover.",
            "5. Confirmar que os pesos resultantes respeitam o cap máximo por token.",
        ]

        ai_rationale = "\n".join(lines)

        proposal = RebalanceProposal(
            index_id=index_id,
            proposed_at=datetime.utcnow(),
            status="pending",
            trigger="scout",
            changes=proposal_changes,
            ai_rationale=ai_rationale,
            network_mode="mainnet",
        )
        db.add(proposal)
        logger.info(
            f"Scout [{theme}]: RebalanceProposal criada — "
            f"{len(inclusions)} inclusões, {len(exclusions)} exclusões"
        )

    db.commit()

    logger.success(
        f"Scout [{theme}]: completo | candidatos={len(candidates)} | "
        f"+{len(inclusions)} -{len(exclusions)} peso_changes={len(weight_changes)}"
    )


async def _compute_basket_correlation(basket_market_keys: dict) -> str:
    """
    Busca candles 30d para tokens da cesta e calcula correlação de Pearson par-a-par.
    Retorna texto de aviso para pares com |r| > 0.80 (exposição redundante).
    """
    if len(basket_market_keys) < 2:
        return ""

    closes_map: dict = {}
    for sym, market_key in basket_market_keys.items():
        try:
            candles = await get_candles(market_key, interval="1D", limit=32)
            closes = [float(c.get("close", c.get("c", 0)) or 0) for c in candles]
            closes = [p for p in closes if p > 0]
            if len(closes) >= 10:
                closes_map[sym] = closes
        except Exception as exc:
            logger.debug(f"Scout correlação: candles {market_key} falhou: {exc}")

    if len(closes_map) < 2:
        return ""

    def to_returns(ps):
        return [(ps[i] - ps[i - 1]) / ps[i - 1] for i in range(1, len(ps)) if ps[i - 1] > 0]

    def pearson(xs, ys):
        n = min(len(xs), len(ys))
        if n < 5:
            return 0.0
        xs, ys = xs[-n:], ys[-n:]
        mx, my = sum(xs) / n, sum(ys) / n
        num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        denom = (sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys)) ** 0.5
        return round(num / denom, 3) if denom > 0 else 0.0

    rets = {sym: to_returns(closes) for sym, closes in closes_map.items()}
    syms = list(rets.keys())

    high_pairs = []
    for i in range(len(syms)):
        for j in range(i + 1, len(syms)):
            r = pearson(rets[syms[i]], rets[syms[j]])
            if abs(r) >= 0.80:
                high_pairs.append((syms[i], syms[j], r))

    if not high_pairs:
        return ""

    lines = ["BASKET CORRELATION ALERT (30d daily returns — possible redundant exposure):"]
    for a, b, r in sorted(high_pairs, key=lambda x: -abs(x[2])):
        lines.append(f"- {a} & {b}: r={r:.2f} ({'positive' if r > 0 else 'negative'} correlation)")
    return "\n".join(lines)


def _apply_momentum_scoring(tokens: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Score multi-dimensional usando dados reais da SoSoValue:
    - Base: peso SSI normalizado (posição relativa no índice da SoSoValue)
    - Momentum: ROI ponderado 7d(30%) + 30d(40%) + 3m(30%) — via klines SoSoValue
    - Qualidade: bônus por mcap_rank (top 50 = +10%, top 100 = +5%)
    Fallback para price_change_30d do SoDEX se klines não disponíveis.
    """
    max_weight = max(t.get("ssi_weight", 0) for t in tokens) if tokens else 1.0
    if max_weight == 0:
        max_weight = 1.0

    for token in tokens:
        base = token.get("ssi_weight", 0) / max_weight  # 0..1

        # ROI: usa dados SoSoValue quando disponíveis, fallback para SoDEX 30d
        roi_7d  = token.get("roi_7d")  or token.get("price_change_7d",  0) or 0
        roi_30d = token.get("roi_30d") or token.get("price_change_30d", 0) or 0
        roi_3m  = token.get("roi_3m",  0) or 0

        # Normaliza retornos para escala -1..+1 (referência: ±20% em 30d, ±40% em 3m)
        m7  = max(-1.0, min(1.0, roi_7d  / 20.0))
        m30 = max(-1.0, min(1.0, roi_30d / 20.0))
        m3m = max(-1.0, min(1.0, roi_3m  / 40.0))
        momentum = 0.30 * m7 + 0.40 * m30 + 0.30 * m3m  # -1..+1

        # Bônus por market cap rank (qualidade institucional)
        rank = token.get("mcap_rank", 999)
        quality = 0.10 if rank <= 50 else (0.05 if rank <= 100 else 0.0)

        token["scout_score"] = base * (1 + 0.25 * momentum) + quality
        # Guardar componentes para o prompt AI
        token["roi_7d"]  = roi_7d
        token["roi_30d"] = roi_30d
        token["roi_3m"]  = roi_3m

    tokens.sort(key=lambda x: x["scout_score"], reverse=True)
    return tokens


async def _generate_ai_rationale(
    tokens: List[Dict[str, Any]],
    theme: str,
    sentiment_score: float,
    news_context: str = "",
    benchmark: dict = None,
    correlation_ctx: str = "",
) -> List[Dict[str, Any]]:
    """Gemini gera rationale usando dados SSI + SoDEX."""

    token_summaries = "\n".join([
        f"- {t['symbol']}: SSI weight {t.get('ssi_weight', 0)*100:.1f}%"
        + (f", mcap rank #{t['mcap_rank']}" if t.get("mcap_rank") and t["mcap_rank"] < 999 else "")
        + (f", price ${t['current_price_usd']:.4f}" if t.get("current_price_usd") else "")
        + (f", mcap ${t['marketcap']/1e9:.2f}B" if t.get("marketcap", 0) > 1e8 else "")
        + f", 7d {t.get('roi_7d', 0):+.1f}% / 30d {t.get('roi_30d', 0):+.1f}% / 3m {t.get('roi_3m', 0):+.1f}%"
        + (" [on SoDEX]" if t.get("on_sodex") else " [not on SoDEX]")
        for t in tokens
    ])

    benchmark_line = ""
    if benchmark:
        ticker = benchmark.get("ssi_ticker", "")
        roi_30 = benchmark.get("1month_roi", 0)
        roi_7 = benchmark.get("7day_roi", 0)
        benchmark_line = f"\nSoSoValue {ticker} benchmark: 7d {roi_7*100:+.1f}%, 30d {roi_30*100:+.1f}%"

    news_line = f"\nLatest news: {news_context}" if news_context else ""
    correlation_line = f"\n\n{correlation_ctx}" if correlation_ctx else ""

    prompt = f"""You are a crypto analyst managing a thematic index: {theme.upper()}.
All candidates are pre-qualified by SoSoValue's SSI index — a trusted institutional crypto index.

Market sentiment: {sentiment_score}/100 ({_sentiment_label(sentiment_score)}){benchmark_line}{news_line}{correlation_line}

Candidates (ranked by SSI weight + momentum):
{token_summaries}

For each token, write ONE sentence (max 20 words) explaining why it belongs in this index.
Focus on: role in the theme, momentum, and institutional recognition by SoSoValue SSI.
When relevant, reference correlation data above to justify diversification choices.

Respond ONLY with a JSON array:
[
  {{"symbol": "RENDER", "rationale": "..."}},
  ...
]"""

    try:
        content = await generate(prompt, max_tokens=512, temperature=0.0)
        start = content.find("[")
        end = content.rfind("]") + 1
        parsed_list = json.loads(content[start:end]) if start >= 0 else []
        rationale_map = {
            item["symbol"]: item["rationale"]
            for item in parsed_list
            if isinstance(item, dict) and "symbol" in item
        }
    except Exception as e:
        logger.error(f"Scout [{theme}]: geração de rationale falhou: {e}")
        rationale_map = {}

    for token in tokens:
        token["ai_rationale"] = rationale_map.get(
            token["symbol"],
            f"Constituent of SoSoValue {theme} SSI index with {token.get('ssi_weight', 0)*100:.1f}% weight.",
        )

    return tokens


MAX_WEIGHT_PCT = 25.0  # cap por token — evita concentração excessiva


def _compute_changes(
    candidates: List[Dict],
    current_symbols: Dict,
    sentiment_score: float,
    theme: str = "",
    target_n: int = 5,
    anchor_symbols: set = None,
    benchmark_30d: float = 0.0,
    fresh_30d: dict = None,
) -> tuple:
    """Determina inclusões/exclusões/mudanças de peso vs composição atual.
    Respeita target_n: número alvo de tokens na cesta (fixo por índice).
    Cap de MAX_WEIGHT_PCT por token para evitar concentração.
    Tokens âncora (is_anchor=True) são IMUNES a exclusão pelo Scout.
    Tokens da cesta com price_change_30d > benchmark_30d também são protegidos.
    """
    from services.sosovalue import get_stablecoin_buffer_from_sentiment
    from loguru import logger as _log

    anchor_symbols = anchor_symbols or set()

    # Apenas os top N candidatos entram na cesta alvo
    top_n = candidates[:target_n]
    top_n_symbols = {t["symbol"] for t in top_n}
    current_symbol_set = set(current_symbols.keys()) - {"USDC", "USDT"}

    # Inclusões: candidatos no top N que ainda não estão na cesta E confirmados no SoDEX TRADING (não HALT)
    not_on_sodex_new = [t["symbol"] for t in top_n if t["symbol"] not in current_symbol_set and not t.get("on_sodex")]
    if not_on_sodex_new:
        _log.warning(f"Scout [{theme}]: bloqueando {not_on_sodex_new} — não listados no SoDEX TRADING")
    halt_blocked = [t["symbol"] for t in top_n if t["symbol"] not in current_symbol_set and t.get("on_sodex") and t.get("is_halt")]
    if halt_blocked:
        _log.warning(f"Scout [{theme}]: bloqueando {halt_blocked} — status HALT no SoDEX")
    inclusions = [
        {"symbol": t["symbol"], "name": t["name"], "rationale": t.get("ai_rationale", "")}
        for t in top_n
        if t["symbol"] not in current_symbol_set and t.get("on_sodex", False) and not t.get("is_halt", False)
    ]

    # Exclusões: tokens atuais que saíram do top N
    # Tokens âncora (permanentes + campo is_anchor) são IMUNES ao Scout.
    # REGRA: só recomendar exclusão de tokens efetivamente na cesta (in_basket=True e weight>0).
    # Candidatos históricos com in_basket=False / weight=0 existem no DB mas não estão na cesta —
    # incluí-los nas exclusões gera propostas sem efeito real e confunde a análise do admin.
    all_anchors = anchor_symbols | PERMANENT_ANCHORS
    tokens_to_remove = [
        sym for sym in current_symbol_set
        if sym not in top_n_symbols
        and sym not in all_anchors
        and getattr(current_symbols.get(sym), "in_basket", False)
        and (current_symbols.get(sym).weight or 0) > 0
    ]
    protected = [sym for sym in current_symbol_set if sym not in top_n_symbols and sym in all_anchors]
    if protected:
        _log.info(f"Scout [{theme}]: âncoras protegidas contra remoção: {sorted(protected)}")

    # PROTEÇÃO POR PERFORMANCE: tokens da cesta (in_basket=True) que superam o benchmark 30d são imunes a exclusão.
    # Lógica: se o token está performando melhor que o índice de referência, removê-lo seria
    # prejudicial ao fundo — o benchmark é o custo de oportunidade do índice temático.
    # Usa dados frescos de klines (fresh_30d) em vez do DB para evitar valores desatualizados
    # no horário de execução do Scout (NAV Updater roda depois).
    if benchmark_30d > 0 and tokens_to_remove:
        _fresh = fresh_30d or {}
        perf_protected = []
        remaining = []
        for sym in tokens_to_remove:
            constituent = current_symbols.get(sym)
            # Só protege tokens efetivamente na cesta — candidatos não precisam de proteção
            if not getattr(constituent, "in_basket", False):
                remaining.append(sym)
                continue
            # Prefere dado fresco de klines (roi_30d da SoSoValue); fallback para DB se não disponível
            change_30d = _fresh.get(sym) if sym in _fresh else getattr(constituent, "price_change_30d", None)
            if change_30d is not None and change_30d > benchmark_30d:
                perf_protected.append(sym)
            else:
                remaining.append(sym)
        if perf_protected:
            _log.info(
                f"Scout [{theme}]: proteção por performance ({benchmark_30d:.1f}% benchmark 30d) — "
                f"{sorted(perf_protected)} superam o benchmark → imunes a exclusão"
            )
            tokens_to_remove = remaining

    exclusions = []
    for sym in tokens_to_remove:
        exclusions.append({"symbol": sym, "reason": f"Replaced by better-ranked token — outside top {target_n} for {theme}"})

    # REGRA CRÍTICA: não recomendar exclusão sem substituto validado no SoDEX.
    # Se inclusions=[], não há tokens confirmados para preencher as vagas —
    # manter a cesta como está é mais seguro que esvaziar sem substitutos.
    if not inclusions and exclusions:
        _log.warning(
            f"Scout [{theme}]: {len(exclusions)} exclusões suprimidas — "
            "nenhum substituto validado no SoDEX TRADING (inclusions vazia). "
            "Manter cesta atual é preferível a esvaziar sem reposição."
        )
        exclusions = []

    # Pesos SSI normalizados com cap de MAX_WEIGHT_PCT sobre os top N
    target_buffer = get_stablecoin_buffer_from_sentiment(sentiment_score)
    usable_pct = 100.0 - target_buffer
    total_ssi = sum(t.get("ssi_weight", 1.0) for t in top_n) or 1.0

    raw_weights = {
        t["symbol"]: (t.get("ssi_weight", 1.0) / total_ssi) * usable_pct
        for t in top_n
    }
    # Aplica cap iterativamente redistribuindo excesso
    for _ in range(5):
        excess = sum(max(0, w - MAX_WEIGHT_PCT) for w in raw_weights.values())
        if excess < 0.01:
            break
        uncapped = {s: w for s, w in raw_weights.items() if w < MAX_WEIGHT_PCT}
        uncapped_total = sum(uncapped.values()) or 1.0
        for s in raw_weights:
            if raw_weights[s] >= MAX_WEIGHT_PCT:
                raw_weights[s] = MAX_WEIGHT_PCT
            else:
                raw_weights[s] += excess * (raw_weights[s] / uncapped_total)

    weight_changes = []
    for token in top_n:
        new_w = round(raw_weights[token["symbol"]], 1)
        current = current_symbols.get(token["symbol"])
        if current and abs(new_w - current.weight) > 2:
            weight_changes.append({
                "symbol": token["symbol"],
                "old_weight": current.weight,
                "new_weight": new_w,
            })

    # Preenche target_weight nas inclusões usando o peso SSI calculado (com cap)
    for inc in inclusions:
        inc["target_weight"] = round(raw_weights.get(inc["symbol"], 0.0), 1)

    return inclusions, exclusions, weight_changes


def _build_activity_summary(inclusions, exclusions, weight_changes) -> str:
    parts = []
    if inclusions:
        parts.append(f"Inclusions: {', '.join(i['symbol'] for i in inclusions)}")
    if exclusions:
        parts.append(f"Exclusions: {', '.join(e['symbol'] for e in exclusions)}")
    if weight_changes:
        changes = [f"{w['symbol']} {w['old_weight']}%→{w['new_weight']}%" for w in weight_changes[:3]]
        parts.append(f"Weight changes: {', '.join(changes)}")
    if not parts:
        return "Scout screening complete. No changes recommended. All positions within thresholds."
    return "Scout screening complete. " + " | ".join(parts)


def _sentiment_label(score: float) -> str:
    if score <= 20: return "Extreme Fear"
    if score <= 40: return "Fear"
    if score <= 60: return "Neutral"
    if score <= 80: return "Greed"
    return "Extreme Greed"
