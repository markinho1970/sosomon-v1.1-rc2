"""
Rebalancer Agent — Portfolio maintenance using Claude Sonnet.

Responsabilidades:
- Lê relatórios do Scout para obter mudanças recomendadas
- Verifica drift (>8% do peso alvo dispara rebalanceamento)
- Aplica overrides de risco (buffer de stablecoin baseado em sentiment)
- Ejeta tokens com perda >40% em 7 dias (emergency override)
- Gera propostas de rebalanceamento para revisão do founder
- Executa ordens REAIS no SoDEX via API (EIP-712)
"""

import os
import json
import uuid
from datetime import datetime
from typing import List, Dict, Any, Optional
from loguru import logger
from services.llm import generate

from database import SessionLocal
from models import AlphaIndex, IndexConstituent, AgentActivityLog, ScoutReport, RebalanceProposal, TradeExecution
from services.sosovalue import get_macro_context, get_stablecoin_buffer_from_sentiment
from services.sodex import (
    get_all_tickers,
    get_markets,
    get_portfolio_snapshot,
    execute_rebalance_trades,
)


DRIFT_THRESHOLD = 8.0              # % deviation from target weight that triggers rebalance
EJECTION_THRESHOLD = -40.0         # % 7-day loss that triggers emergency ejection
MAX_SINGLE_WEIGHT = 25.0           # no single token > 25%
DRIFT_COOLDOWN_HOURS = 48          # horas de cooldown após execução antes de novo drift trigger
SCOUT_UPGRADE_MARGIN = 15          # pts de vantagem do candidato sobre o token mais fraco para trigger Scout
MIN_ORDER_USD = 5.0                # mínimo por ordem aceito pelo SoDEX


async def check_and_propose_rebalances():
    """Main entry — check all active indexes for rebalance needs."""
    db = SessionLocal()
    try:
        macro = await get_macro_context()
        sentiment_score = macro["sosovalue_sentiment_score"]
        target_buffer = get_stablecoin_buffer_from_sentiment(sentiment_score)

        indexes = db.query(AlphaIndex).filter(AlphaIndex.is_active == True).all()
        for idx in indexes:
            await _process_index(idx, sentiment_score, target_buffer, db)
    finally:
        db.close()


async def _process_index(idx: AlphaIndex, sentiment_score: float, target_buffer: float, db):
    """Evaluate a single index for rebalance needs."""
    logger.info(f"Rebalancer: evaluating index={idx.id}")

    # Não processa índices sem AUM — evita alterar cestas sem investidores ativos
    if not (idx.aum_usd or 0) > 0:
        logger.info(f"Rebalancer: skipping {idx.id} — AUM=$0 (sem investidores ativos)")
        return

    # CRÍTICO: apenas tokens DA CESTA ATIVA em mainnet.
    # Candidatos (in_basket=False) e tokens testnet NÃO entram na análise.
    constituents = db.query(IndexConstituent).filter(
        IndexConstituent.index_id == idx.id,
        IndexConstituent.in_basket == True,
        IndexConstituent.network_mode == "mainnet",
    ).all()

    if not constituents:
        logger.warning(f"Rebalancer: no basket constituents for {idx.id} mainnet")
        return

    # Refresh prices via SoDEX tickers
    try:
        _tickers = await get_all_tickers()
        for c in constituents:
            if c.is_stablecoin:
                continue
            sym = c.symbol
            for key in (f"{sym}-USD", f"{sym}-USDC", f"v{sym}-USDC", f"v{sym}-vUSDC"):
                _t = _tickers.get(key)
                if _t:
                    _p = float(_t.get("lastPrice", _t.get("c", 0)) or 0)
                    if _p > 0:
                        c.current_price_usd = _p
                        c.volume_24h_usd = float(_t.get("volume24h", _t.get("v", 0)) or 0)
                    break
        db.flush()
    except Exception as _e:
        logger.warning(f"Rebalancer: erro ao refreshar preços: {_e}")

    # Busca status HALT/TRADING do SoDEX mainnet para informar o LLM
    halt_tokens: set = set()
    try:
        from services.sodex import get_markets
        markets = await get_markets(testnet=False)
        for m in markets:
            status = m.get("status", m.get("tradingStatus", "")).upper()
            if status == "HALT":
                base = m.get("baseCoin", m.get("displayName", "")).replace("v", "").replace("V", "").split("/")[0]
                halt_tokens.add(base.upper())
        logger.debug(f"Rebalancer: HALT tokens mainnet = {halt_tokens}")
    except Exception as _e:
        logger.warning(f"Rebalancer: não foi possível verificar status HALT: {_e}")

    # Check risk overrides
    emergencies = _check_emergency_ejections(constituents)

    # Check drift (usando pesos-alvo reais, não peso igual)
    drift_violations = _check_drift(constituents, target_buffer)

    # Get latest scout report
    latest_scout = db.query(ScoutReport).filter(
        ScoutReport.index_id == idx.id
    ).order_by(ScoutReport.run_at.desc()).first()

    scout_changes = {
        "inclusions": latest_scout.inclusions if latest_scout else [],
        "exclusions": latest_scout.exclusions if latest_scout else [],
        "weight_changes": latest_scout.weight_changes if latest_scout else [],
    }

    # Whitelist de tokens permitidos para adição: somente o que o Scout validou via on_sodex=True
    # O Scout já verificou contra get_all_tickers() — é a fonte confiável, não get_markets()
    # (get_markets() usa campo "baseCoin" que tem prefixo "V" diferente dos símbolos candidatos)
    scout_validated_additions = {
        inc["symbol"].upper()
        for inc in scout_changes.get("inclusions", [])
        if inc.get("symbol")
    }

    # Determine if rebalance is needed
    trigger = None
    if emergencies:
        trigger = "risk_override"
    elif drift_violations:
        trigger = "drift"
    elif _has_scout_upgrade(latest_scout):
        trigger = "scout_upgrade"

    if not trigger:
        _log_no_action(idx.id, db)
        return

    # Skip if there is already a pending proposal for this index (except emergencies)
    if trigger != 'risk_override':
        existing = db.query(RebalanceProposal).filter(
            RebalanceProposal.index_id == idx.id,
            RebalanceProposal.status == 'pending',
        ).first()
        if existing:
            logger.info(f'Rebalancer: skipping {idx.id} — pending proposal #{existing.id} already exists')
            return

    # Cooldown de drift: não gerar nova proposta por drift dentro de DRIFT_COOLDOWN_HOURS
    # após a última execução. Evita re-trigger imediato quando um token se valoriza após rebalance.
    # risk_override e weekly ignoram o cooldown.
    if trigger == 'drift' and idx.last_rebalanced_at:
        hours_since = (datetime.utcnow() - idx.last_rebalanced_at).total_seconds() / 3600
        if hours_since < DRIFT_COOLDOWN_HOURS:
            logger.info(
                f"Rebalancer [{idx.id}]: drift ignorado — cooldown ativo "
                f"({hours_since:.1f}h desde último rebalance, mínimo {DRIFT_COOLDOWN_HOURS}h)"
            )
            _log_no_action(idx.id, db)
            return

    # Para trigger de DRIFT: ajustar pesos somente, nunca substituir tokens.
    # Exclusões do Scout são ignoradas — drift = preço saiu do peso-alvo,
    # não significa que o token deva ser removido.
    # Inclusions também são ignoradas — nova entrada só via weekly ou risk_override.
    if trigger == "drift":
        basket_syms = {c.symbol for c in constituents}
        scout_exclusions = scout_changes.get("exclusions", [])
        basket_exclusions = [e for e in scout_exclusions if e.get("symbol") in basket_syms]

        # Se Scout quer excluir tokens da cesta em um drift trigger, há contradição:
        # drift ajusta pesos, não remove tokens. Bloqueia.
        if basket_exclusions:
            logger.warning(
                f"Rebalancer [{idx.id}]: drift trigger bloqueado — Scout recomenda excluir "
                f"{[e['symbol'] for e in basket_exclusions]} mas drift só ajusta pesos, "
                "não substitui tokens. Ignorando exclusões do Scout para drift."
            )

        # Drift usa somente weight_changes do Scout; exclusions e inclusions são zeradas.
        scout_changes = {
            "inclusions": [],
            "exclusions": [],
            "weight_changes": scout_changes.get("weight_changes", []),
        }

        # Sem dados de peso do Scout e sem violações reais de drift: nada a fazer.
        if not scout_changes["weight_changes"] and not drift_violations:
            _log_no_action(idx.id, db)
            return

    # Generate rebalance proposal using LLM
    proposal_changes = await _generate_proposal(
        idx=idx,
        constituents=constituents,
        trigger=trigger,
        emergencies=emergencies,
        scout_changes=scout_changes,
        target_buffer=target_buffer,
        sentiment_score=sentiment_score,
        scout_validated_additions=scout_validated_additions,
        halt_tokens=halt_tokens,
        db=db,
    )

    if proposal_changes:
        _save_proposal(idx.id, trigger, proposal_changes, db, network_mode="mainnet")


def _has_scout_upgrade(latest_scout) -> bool:
    """
    True se o Scout encontrou um candidato claramente melhor que o token mais fraco da cesta.
    Critério: score do melhor candidato de entrada >= score do pior token de saída + SCOUT_UPGRADE_MARGIN.
    Evita trigger por candidatos marginalmente melhores.
    """
    if not latest_scout:
        return False
    inclusions = latest_scout.inclusions or []
    exclusions = latest_scout.exclusions or []
    if not inclusions or not exclusions:
        return False
    best_in_score = max((i.get("score", 0) for i in inclusions), default=0)
    worst_out_score = min((e.get("score", 100) for e in exclusions), default=100)
    qualifies = (best_in_score - worst_out_score) >= SCOUT_UPGRADE_MARGIN
    if qualifies:
        logger.info(
            f"Rebalancer: Scout upgrade trigger — "
            f"melhor candidato score={best_in_score}, pior token score={worst_out_score} "
            f"(margem={best_in_score - worst_out_score} >= {SCOUT_UPGRADE_MARGIN})"
        )
    return qualifies


def _check_min_order_viability(changes: List[Dict], snapshot: Dict):
    """
    Separa ordens abaixo do mínimo SoDEX ($5) para evitar rejeição na exchange.
    Ordens 'remove' e 'maintain' passam sempre — apenas trades ativos são checados.
    Retorna (viable_changes, skipped_changes).
    """
    total_usd = snapshot.get("total_usd", 0) or 0
    # Normaliza positions para dict keyed por symbol
    raw_positions = snapshot.get("positions", [])
    if isinstance(raw_positions, list):
        positions = {p.get("asset", p.get("symbol", "")): p for p in raw_positions}
    else:
        positions = raw_positions

    viable, skipped = [], []
    for change in changes:
        action = change.get("action", "")
        if action in ("remove", "maintain"):
            viable.append(change)
            continue

        symbol = change["symbol"]
        pos = positions.get(symbol, {})
        current_usd = pos.get("usd_value", pos.get("value_usd", 0)) if pos else 0
        current_pct = (current_usd / total_usd * 100) if total_usd > 0 else 0
        target_pct = change.get("new_weight", 0)
        trade_usd = abs(target_pct - current_pct) / 100 * total_usd if total_usd > 0 else 0

        if trade_usd < MIN_ORDER_USD:
            skipped.append({**change, "skip_reason": "below_min_notional", "trade_usd": round(trade_usd, 2)})
        else:
            viable.append(change)

    return viable, skipped


def _check_emergency_ejections(constituents: List[IndexConstituent]) -> List[Dict]:
    """Find tokens that lost >40% in 7 days — immediate ejection required."""
    ejections = []
    for c in constituents:
        if not c.is_stablecoin and c.price_change_7d <= EJECTION_THRESHOLD:
            ejections.append({
                "symbol": c.symbol,
                "reason": f"Emergency ejection: {c.price_change_7d:.1f}% loss in 7 days (threshold: {EJECTION_THRESHOLD}%)",
                "old_weight": c.weight,
                "new_weight": 0,
                "action": "eject",
            })
            logger.warning(f"EMERGENCY EJECTION queued: {c.symbol} ({c.price_change_7d:.1f}% 7d)")
    return ejections


def _check_drift(constituents: List[IndexConstituent], target_buffer: float) -> List[Dict]:
    """
    Verifica se algum token drifou do seu peso-ALVO real (c.weight).

    Estima o peso de mercado atual a partir de variações de preço 7d:
      valor_estimado_i = peso_alvo_i * (1 + price_change_7d_i / 100)
      peso_atual_i = valor_estimado_i / total * (100 - buffer)

    O drift é comparado contra o peso-alvo de CADA token (não peso igual).
    """
    non_stable = [c for c in constituents if not c.is_stablecoin]
    if not non_stable:
        return []

    # Peso estimado de mercado baseado em variação de preço 7d
    est_values = {
        c.symbol: c.weight * (1 + (c.price_change_7d or 0) / 100)
        for c in non_stable
    }
    total_est = sum(est_values.values()) or 1.0
    usable = 100.0 - target_buffer

    violations = []
    for c in non_stable:
        est_current = est_values[c.symbol] / total_est * usable
        drift = abs(est_current - c.weight)
        if drift > DRIFT_THRESHOLD:
            violations.append({
                "symbol":         c.symbol,
                "current_weight": round(est_current, 1),
                "target_weight":  round(c.weight, 1),
                "drift":          round(drift, 1),
            })
    return violations


async def _generate_proposal(
    idx, constituents, trigger, emergencies, scout_changes, target_buffer, sentiment_score,
    scout_validated_additions: set, halt_tokens: set, db
) -> Optional[List[Dict]]:
    """Use LLM to generate the rebalancing proposal with full rationale."""

    # Apenas tokens da cesta ativa (in_basket=True) são incluídos no estado atual
    basket = [c for c in constituents if getattr(c, "in_basket", True)]
    current_state = "\n".join([
        f"- {c.symbol}: TARGET={c.weight:.1f}%, "
        f"price ${c.current_price_usd:.4f}, "
        f"7d {c.price_change_7d:+.1f}%, "
        f"SoDEX={'HALT ⚠️' if c.symbol.upper().lstrip('V') in halt_tokens else 'TRADING'}"
        for c in basket
    ])

    halt_warning = ""
    if halt_tokens:
        halt_warning = f"\nHALT TOKENS ON SoDEX MAINNET (cannot trade): {', '.join(sorted(halt_tokens))}\nDo NOT propose increase/add for HALT tokens — orders will be rejected by the exchange.\n"

    scout_summary = json.dumps(scout_changes, indent=2)
    emergency_summary = json.dumps(emergencies, indent=2) if emergencies else "None"

    # Tokens permitidos para adição: somente o que o Scout validou no SoDEX via on_sodex=True
    # Exclui tokens já na cesta e WSOSO (governance token)
    current_symbols = {c.symbol.upper() for c in constituents}
    allowed_additions = sorted(scout_validated_additions - current_symbols - {"WSOSO"})
    allowed_additions_str = ", ".join(allowed_additions) if allowed_additions else "none — keep existing basket unchanged"

    prompt = f"""You are the Rebalancer agent for SoSoMon, managing the "{idx.name}" index.

TRIGGER: {trigger.upper()}

CURRENT BASKET STATE (only these {len(basket)} tokens exist in the basket — do NOT reference any other token):
{current_state}
{halt_warning}
EMERGENCY EJECTIONS (execute immediately):
{emergency_summary}

SCOUT AGENT RECOMMENDATIONS (from last daily screening):
{scout_summary}

ALLOWED TOKENS FOR ADDITION (HARD RULE — you may ONLY add symbols from this list):
{allowed_additions_str}
DO NOT invent token symbols. DO NOT use any symbol not in the basket or in the allowed list above.

CONSTRAINTS:
- The TARGET weight shown for each token is its designed allocation — DO NOT change weights without a specific reason (drift, emergency, or Scout recommendation).
- DRIFT ONLY adjusts tokens that have actually drifted. If a token is near its target weight, set action="maintain" and keep old_weight == new_weight.
- ANCHOR TOKENS (NEVER remove, never reduce below their target): MAG7ssi, DEFIssi, USSIssi, WSOSO. These are the thematic pillars. Removing them would destroy the index thesis.
- Target stablecoin buffer: {target_buffer:.0f}% (sentiment {sentiment_score}/100)
- Max single token weight: {MAX_SINGLE_WEIGHT}%
- HARD LIMIT: basket must have EXACTLY {idx.target_constituents} non-stablecoin tokens after rebalance.
- CRITICAL: new_weight of all non-remove tokens must sum to exactly 100.0%.
- CONSERVATIVE: ONLY remove a token if it is in EMERGENCY EJECTIONS or Scout explicitly recommended removal. Do NOT restructure the basket without cause.
- STABLE CORE: prefer small weight adjustments over full replacements.
- HALT tokens listed above CANNOT be traded — do NOT propose add/increase for them.
- If ALLOWED TOKENS FOR ADDITION is "none", do NOT add any new token. Maintain the existing basket exactly.

Generate a rebalancing proposal. Respond ONLY with valid JSON:
{{
  "changes": [
    {{
      "symbol": "TOKEN",
      "action": "add|remove|increase|decrease|maintain",
      "old_weight": 0.0,
      "new_weight": 0.0,
      "rationale": "one sentence"
    }}
  ],
  "summary": "2-3 sentence summary of this rebalance",
  "risk_notes": "any risk considerations"
}}"""

    try:
        content = await generate(prompt, max_tokens=1024, temperature=0.0)
        # Extract JSON from model output
        start = content.find("{")
        end = content.rfind("}") + 1
        parsed = json.loads(content[start:end])

        # Log activity
        activity = AgentActivityLog(
            id=str(uuid.uuid4()),
            index_id=idx.id,
            agent="rebalancer",
            action="rebalance",
            description=parsed.get("summary", "Rebalance proposal generated."),
            timestamp=datetime.utcnow(),
            data={"trigger": trigger, "changes_count": len(parsed.get("changes", []))},
        )
        db.add(activity)
        db.commit()

        changes = parsed.get("changes", [])

        # Valida: tokens em "add" devem ter sido validados pelo Scout via SoDEX
        # Se Scout não recomendou nenhuma inclusão, nenhum "add" é permitido
        invalid_adds = [
            c["symbol"] for c in changes
            if c.get("action") == "add"
            and c["symbol"].upper() not in scout_validated_additions
        ]
        if invalid_adds:
            logger.warning(
                f"Rebalancer [{idx.id}]: proposta adiciona tokens não validados pelo Scout: "
                f"{invalid_adds} — descartando proposta"
            )
            return None

        # Rejeita WSOSO como constituinte — é o token do SoDEX
        changes = [c for c in changes if not (c.get("action") in ("add", "maintain") and c.get("symbol") == "WSOSO")]

        # Rejeita "add" com peso zero — LLM quer adicionar mas não tem espaço.
        # Converte para maintain se o token já está na cesta, ou descarta se é novo.
        basket_symbols = {c.symbol for c in basket}
        zero_adds = [c for c in changes if c.get("action") == "add" and c.get("new_weight", 0) <= 0]
        if zero_adds:
            for c in zero_adds:
                if c["symbol"] in basket_symbols:
                    c["action"] = "maintain"
                    c["new_weight"] = next((b.weight for b in basket if b.symbol == c["symbol"]), 0)
                    logger.warning(f"Rebalancer [{idx.id}]: {c['symbol']} add com peso=0 → convertido para maintain")
                else:
                    logger.warning(f"Rebalancer [{idx.id}]: {c['symbol']} add com peso=0 → descartado (sem espaço na proposta)")
            changes = [c for c in changes if not (c.get("action") == "add" and c.get("new_weight", 0) <= 0)]

        # Garante que o número de tokens adicionados não ultrapassa target_constituents
        adds = [c for c in changes if c.get("action") in ("add", "maintain", "increase", "decrease")]
        if len(adds) > idx.target_constituents:
            logger.warning(
                f"Rebalancer [{idx.id}]: {len(adds)} tokens propostos, target={idx.target_constituents} — truncando"
            )
            keep_symbols = {c["symbol"] for c in sorted(adds, key=lambda x: x.get("new_weight", 0), reverse=True)[:idx.target_constituents]}
            changes = [c for c in changes if c.get("action") == "remove" or c.get("symbol") in keep_symbols]

        # Tokens in-basket não mencionados na proposta precisam de ação explícita.
        # Se os ativos já somam ~100%, são remoções implícitas — tornar explícito para evitar
        # que fiquem "fantasmas" na cesta após execução.
        changes_symbols = {c["symbol"] for c in changes}
        active_total_proposed = sum(
            c.get("new_weight", 0) for c in changes if c.get("action") not in ("remove",)
        )
        for constituent in basket:
            if constituent.symbol in changes_symbols:
                continue
            if active_total_proposed >= 99.5:
                changes.append({
                    "symbol": constituent.symbol,
                    "action": "remove",
                    "old_weight": constituent.weight,
                    "new_weight": 0.0,
                    "rationale": (
                        f"Removido: proposta aloca 100% do peso para outros tokens, "
                        f"sem espaço para {constituent.symbol}."
                    ),
                })
                logger.info(
                    f"Rebalancer [{idx.id}]: {constituent.symbol} marcado como 'remove' implícito "
                    f"(ativo_total={active_total_proposed:.1f}%, token não listado na proposta LLM)"
                )
            else:
                changes.append({
                    "symbol": constituent.symbol,
                    "action": "maintain",
                    "old_weight": constituent.weight,
                    "new_weight": constituent.weight,
                    "rationale": "Mantido sem alteração — não mencionado na proposta e há espaço disponível.",
                })

        # Rejeita proposta se há menos tokens ativos que o target
        active_check = [c for c in changes if c.get("action") != "remove"]
        if len(active_check) < idx.target_constituents:
            logger.warning(
                f"Rebalancer [{idx.id}]: apenas {len(active_check)} tokens ativos, target={idx.target_constituents} — rejeitando proposta"
            )
            return None

        # Validação hard: âncoras não podem ser reduzidas pelo LLM.
        # Prompts são instruções, não garantias — este guard é o código de proteção real.
        anchor_syms = {c.symbol for c in basket if getattr(c, "is_anchor", False)}
        for ch in changes:
            if ch.get("symbol") in anchor_syms and ch.get("action") in ("decrease", "remove", "eject"):
                anchor_db = next((c for c in basket if c.symbol == ch["symbol"]), None)
                original_w = anchor_db.weight if anchor_db else ch.get("old_weight", 0)
                logger.warning(
                    f"Rebalancer [{idx.id}]: âncora {ch['symbol']} seria reduzida "
                    f"({ch.get('old_weight', '?')}%→{ch.get('new_weight', '?')}%) pelo LLM — forçando maintain"
                )
                ch["action"] = "maintain"
                ch["new_weight"] = original_w

        # Validação: pesos de tokens não-removidos devem somar 100%.
        # Normaliza APENAS tokens com mudança ativa; maintains forçam old_weight
        # (sem isso, a normalização alteraria pesos de âncoras que o LLM marcou como maintain).
        active = [c for c in changes if c.get("action") != "remove"]
        for c in active:
            if c.get("action") == "maintain":
                c["new_weight"] = c.get("old_weight", c.get("new_weight", 0))
        maintains = [c for c in active if c.get("action") == "maintain"]
        changing = [c for c in active if c.get("action") != "maintain"]
        maintain_total = sum(c.get("new_weight", 0) for c in maintains)
        total = sum(c.get("new_weight", 0) for c in active)
        if total > 0 and abs(total - 100.0) > 0.5 and changing:
            logger.warning(
                f"Rebalancer [{idx.id}]: pesos somam {total:.2f}% — normalizando tokens ativos para 100%"
            )
            available = 100.0 - maintain_total
            changing_total = sum(c.get("new_weight", 0) for c in changing) or 1.0
            for c in changing:
                c["new_weight"] = round(c.get("new_weight", 0) * available / changing_total, 2)
            total2 = maintain_total + sum(c.get("new_weight", 0) for c in changing)
            if abs(total2 - 100.0) > 0.01:
                biggest = max(changing, key=lambda x: x.get("new_weight", 0)) if changing else max(active, key=lambda x: x.get("new_weight", 0))
                biggest["new_weight"] = round(biggest["new_weight"] + (100.0 - total2), 2)

        return changes

    except Exception as e:
        logger.error(f"Rebalancer Claude call failed: {e}")
        return None


def _save_proposal(index_id: str, trigger: str, changes: List[Dict], db, network_mode: str = "mainnet"):
    """Save rebalance proposal to DB for founder review."""
    proposal = RebalanceProposal(
        index_id=index_id,
        proposed_at=datetime.utcnow(),
        status="pending",
        trigger=trigger,
        changes=changes,
        ai_rationale=f"Proposed by Rebalancer agent. Trigger: {trigger}. {len(changes)} changes.",
        network_mode=network_mode,
    )
    db.add(proposal)
    db.commit()
    logger.success(f"Rebalancer: proposal saved for index={index_id}, trigger={trigger}, changes={len(changes)}")


def _log_no_action(index_id: str, db):
    """Log that no rebalance was needed."""
    activity = AgentActivityLog(
        id=str(uuid.uuid4()),
        index_id=index_id,
        agent="rebalancer",
        action="no_action",
        description="Drift check complete. All positions within 8% threshold. No rebalance needed.",
        timestamp=datetime.utcnow(),
    )
    db.add(activity)
    db.commit()


async def _execute_full_rebalance(
    snapshot: dict,
    all_basket,               # list[IndexConstituent] — tokens atuais da cesta
    new_target_weights: dict, # {symbol: pct} — nova composição alvo (sem zeros)
    dry_run: bool,
    testnet: bool,
) -> list:
    """
    Full rebalance: vende TODOS os tokens da cesta atual, depois recompra
    todos os tokens da nova composição. Garante 100% reinvestimento sem
    USDC sobrando fora do NAV.

    Dry run simula as ordens de compra com base nos proceeds estimados das vendas.
    """
    import math as _math_fr
    # Monta índice de posições para lookup rápido (com e sem prefixo v)
    _pos_idx: dict = {}
    for p in snapshot["positions"]:
        raw = p.get("asset", "")
        _pos_idx[raw] = p
        stripped = raw.lstrip("v").lstrip("V")
        if stripped != raw: _pos_idx[stripped] = p
        nodot = stripped.replace(".", "")
        if nodot != stripped: _pos_idx[nodot] = p

    # Phase 1: vende TODOS os tokens de investimento (nao operacionais).
    # FULL mode = liquidacao total, independente de estar ou nao na nova composicao.
    # Tokens operacionais (USDC, ETH, stablecoins) sao preservados.
    _OPERATIONAL = {"USDC", "USDT", "USD", "ETH", "WETH", "DAI", "BUSD"}
    sell_targets = {}
    for _p in snapshot["positions"]:
        _raw = _p.get("asset", "")
        _clean = _raw.lstrip("v").lstrip("V")
        _nodot = _clean.replace(".", "")
        # pula tokens operacionais
        if _clean in _OPERATIONAL or _nodot in _OPERATIONAL or _raw in _OPERATIONAL:
            continue
        if _p.get("usd_value", 0) > 0.50:
            sell_targets[_nodot or _clean or _raw] = 0.0

    logger.info(
        f"Full rebalance Phase 1: vender {list(sell_targets.keys())} | "
        f"Phase 2: comprar {list(new_target_weights.keys())}"
    )

    sell_orders = await execute_rebalance_trades(
        target_weights=sell_targets,
        dry_run=dry_run,
        testnet=testnet,
    )

    if dry_run:
        # Simula Phase 2 com base nos proceeds estimados das vendas (dry_run)
        fee_rate = 0.001  # 0.1% SoDEX maker/taker
        sell_proceeds = sum(
            o.get("usd_value", 0) * (1 - fee_rate)
            for o in sell_orders
            if o.get("status") == "dry_run"
        )
        # USDC operacional já existente na conta (não é usado para compra)
        op_usdc = _pos_idx.get("USDC", _pos_idx.get("vUSDC", {})).get("usd_value", 0)
        available_usdc = sell_proceeds  # proceeds das vendas (não toca o USDC operacional)

        tickers = await get_all_tickers()
        markets = await get_markets(testnet=testnet)
        mkt_info: dict = {}
        for _m in markets:
            _base = _m.get("baseCoin", "").lstrip("v").lstrip("V").replace(".", "").upper()
            mkt_info[_base] = _m

        buy_orders = []
        # Normaliza pesos para garantir soma = 100%
        total_pct = sum(new_target_weights.values())
        for sym, pct in new_target_weights.items():
            if sym in ("USDC", "USDT", "USD"):
                continue
            normalized_pct = pct / total_pct * 100
            buy_usd = available_usdc * (normalized_pct / 100)
            if buy_usd < 1.0:
                continue
            ticker = tickers.get(f"{sym}-USDC", {})
            price = float(ticker.get("lastPrice") or ticker.get("lastPx") or ticker.get("c") or 0)
            mkt = mkt_info.get(sym.upper(), {})
            qty_prec = int(mkt.get("quantityPrecision") or 4)
            px_prec = int(mkt.get("pricePrecision") or 2)
            qty = _math_fr.floor((buy_usd / price if price > 0 else 0) * 10**qty_prec) / 10**qty_prec if price > 0 else 0
            buy_orders.append({
                "symbol":    f"{sym}-USDC",
                "side":      "BUY",
                "quantity":  f"{qty:.{qty_prec}f}",
                "price":     f"{price:.{px_prec}f}" if price > 0 else "0",
                "usd_value": round(buy_usd, 4),
                "reason":    f"Full rebalance: {sym} 0.0% -> {normalized_pct:.1f}%",
                "status":    "dry_run",
            })
            logger.info(
                f"[DRY RUN] Full BUY {sym}: ${buy_usd:.2f} "
                f"(proceeds=${sell_proceeds:.2f} × {normalized_pct:.1f}%)"
            )
        return sell_orders + buy_orders

    else:
        # Live Phase 2: executa compras (snapshot terá USDC das vendas)
        buy_orders = await execute_rebalance_trades(
            target_weights=new_target_weights,
            dry_run=False,
            testnet=testnet,
        )
        return sell_orders + buy_orders


async def apply_proposal(proposal_id: int, db, dry_run: bool = False):
    """
    Executa proposta aprovada de rebalanceamento via SoDEX API.
    Chamado pelo founder após revisar e aprovar a proposta no admin UI.

    Args:
        proposal_id: ID da proposta no banco
        dry_run: True = simula sem executar ordens reais no SoDEX
    """
    proposal = db.query(RebalanceProposal).filter(
        RebalanceProposal.id == proposal_id,
        RebalanceProposal.status == "approved",
    ).first()

    if not proposal:
        raise ValueError(f"Proposal {proposal_id} not found or not approved")

    try:
        # Determina a rede da proposta (move para cima para usar em todas as chamadas SoDEX)
        network_mode = proposal.network_mode or "mainnet"
        _use_testnet = (network_mode == "testnet")

        # Normaliza: scout pode gerar target_weight em vez de new_weight — unifica aqui
        normalized_changes = []
        for ch in (proposal.changes or []):
            c = dict(ch)
            if "new_weight" not in c and "target_weight" in c:
                c["new_weight"] = c["target_weight"]
            normalized_changes.append(c)
        proposal.changes = normalized_changes

        # 1. Snapshot atual do portfolio no SoDEX (gateway correto por rede)
        snapshot = await get_portfolio_snapshot(testnet=_use_testnet)
        logger.info(
            f"Rebalancer: portfolio snapshot — "
            f"total ${snapshot['total_usd']:,.2f} | "
            f"{len(snapshot['positions'])} posições | "
            f"rede: {snapshot['network']}"
        )

        # 2. Pre-check: filtra ordens abaixo do mínimo SoDEX ($5) antes de enviar
        viable_changes, skipped_changes = _check_min_order_viability(proposal.changes, snapshot)
        for s in skipped_changes:
            logger.warning(
                f"apply_proposal [{proposal_id}]: {s['symbol']} ignorado — "
                f"notional ${s.get('trade_usd', 0):.2f} < mínimo ${MIN_ORDER_USD:.0f} (below_min_notional)"
            )

        # Monta mapa de pesos alvo com renormalização proporcional (Option C):
        # Quando remove um token e adiciona outro com peso diferente,
        # os tokens permanentes absorvem proporcionalmente o peso liberado,
        # garantindo que a soma dos pesos = 100% e nenhum USDC sobra sem ser reinvestido.
        removed_syms  = {c["symbol"] for c in viable_changes if c.get("action") == "remove"}
        added_weights = {c["symbol"]: c["new_weight"] for c in viable_changes if c.get("action") != "remove"}

        target_weights = {}
        # Tokens removidos: SELL tudo (peso 0)
        for sym in removed_syms:
            target_weights[sym] = 0.0

        # Pesos alocados para tokens novos
        allocated_pct = sum(added_weights.values())

        # Busca constituintes atuais da cesta (excluindo os que serão removidos)
        _constituents = db.query(IndexConstituent).filter(
            IndexConstituent.index_id == proposal.index_id,
            IndexConstituent.in_basket == True,
            IndexConstituent.network_mode == network_mode,
        ).all()
        permanent = [c for c in _constituents if c.symbol not in removed_syms and c.symbol not in added_weights]

        # Renormaliza os permanentes para preencher o espaço restante (100% - allocated_pct)
        remaining_pct = 100.0 - allocated_pct
        perm_current_total = sum(getattr(c, "weight", 0) or 0 for c in permanent)
        if permanent and perm_current_total > 0 and remaining_pct > 0:
            for c in permanent:
                w = getattr(c, "weight", 0) or 0
                target_weights[c.symbol] = round(w / perm_current_total * remaining_pct, 4)
            logger.info(
                f"Rebalancer: renormalização proporcional — {len(permanent)} tokens permanentes redistribuem "                f"{remaining_pct:.2f}% (tokens adicionados alocam {allocated_pct:.2f}%)"
            )

        # Adiciona tokens novos com seus pesos definidos
        target_weights.update(added_weights)

        logger.info(f"Rebalancer: target_weights finais = {target_weights}")

        # 3. Executa ordens no SoDEX — FULL ou DELTA mode automático.
        # FULL: vende tudo e recompra tudo quando delta-only produziria ordens < $5 (mínimo SoDEX).
        # DELTA: só os ajustes necessários (mais eficiente, menos taxas).
        _sodex_min = 5.0
        _total_snap = snapshot["total_usd"]
        _needs_full = False
        # Checa se algum ajuste para token permanente ficaria abaixo do mínimo
        _pos_snap: dict = {}
        for _p in snapshot["positions"]:
            _r = _p.get("asset", "")
            _pos_snap[_r] = _p
            _s = _r.lstrip("v").lstrip("V")
            if _s != _r: _pos_snap[_s] = _p
            _nd = _s.replace(".", "")
            if _nd != _s: _pos_snap[_nd] = _p
        for _sym, _pct in target_weights.items():
            if _pct == 0.0: continue  # SELL total — ok, valor da posicao e grande
            _cur = _pos_snap.get(_sym, {}).get("usd_value", 0)
            _tgt = _total_snap * (_pct / 100)
            _delta = abs(_tgt - _cur)
            if 0.50 < _delta < _sodex_min:
                _needs_full = True
                logger.info(
                    f"Rebalancer: FULL mode — {_sym} delta ${_delta:.2f} < ${_sodex_min} "
                    f"(minimo SoDEX). Iniciando full rebalance."
                )
                break

        if _needs_full:
            _new_weights = {s: p for s, p in target_weights.items() if p > 0}
            executed_orders = await _execute_full_rebalance(
                snapshot=snapshot,
                all_basket=_constituents,
                new_target_weights=_new_weights,
                dry_run=dry_run,
                testnet=_use_testnet,
            )
        else:
            executed_orders = await execute_rebalance_trades(
                target_weights=target_weights,
                dry_run=dry_run,
                testnet=_use_testnet,
            )

        logger.info(f"Rebalancer: {len(executed_orders)} ordens {'simuladas' if dry_run else 'executadas'} no SoDEX")

        # ══════════════════════════════════════════════════════════════════════════
        # CONSISTÊNCIA CRÍTICA — GUARD FORTE (3 camadas)
        # Regra: o basket no DB SÓ é atualizado se TODAS as ordens críticas forem
        # confirmadas como "placed" pelo SoDEX. Qualquer falha → raise → rollback.
        # Isso garante que DB basket == portfolio real SoDEX SEMPRE.
        # ══════════════════════════════════════════════════════════════════════════
        if not dry_run and len(viable_changes) > 0:
            placed_orders = [o for o in executed_orders if o.get("status") == "placed"]
            placed_syms   = {o["symbol"].replace("-USDC", "") for o in placed_orders}

            # Camada 1: pelo menos UMA ordem confirmada
            if not placed_orders:
                failed_syms = [c["symbol"] for c in viable_changes]
                unplaced = [(o["symbol"], o.get("status")) for o in executed_orders] if executed_orders else []
                raise ValueError(
                    "GUARD: Nenhuma ordem confirmada no SoDEX para " + str(failed_syms) +
                    " (tentativas: " + str(unplaced) + ") — DB nao atualizado."
                )

            # Camada 2: todos os SELLs de tokens REMOVIDOS devem estar placed
            # (sem SELL confirmado = token ainda no SoDEX = basket DB estaria errado)
            _removed_in_changes = [c["symbol"] for c in viable_changes if c.get("action") == "remove"]
            for _sym in _removed_in_changes:
                if _sym not in placed_syms:
                    raise ValueError(
                        f"GUARD: SELL de {_sym} nao confirmado no SoDEX — "
                        f"token ainda existe no portfolio. DB nao atualizado. "
                        f"Ordens placed: {list(placed_syms)}"
                    )

            # Camada 3: tokens ADICIONADOS devem ter BUY placed
            _added_in_changes = [c["symbol"] for c in viable_changes if c.get("action") != "remove"]
            for _sym in _added_in_changes:
                if _sym not in placed_syms:
                    logger.warning(
                        f"GUARD: BUY de {_sym} nao confirmado no SoDEX — "
                        f"token nao entrou no portfolio. DB nao atualizado para este token."
                    )
                    # Remove este token das changes para nao atualizar basket incorretamente
                    proposal.changes = [
                        c for c in proposal.changes
                        if not (c.get("action") != "remove" and c["symbol"] == _sym)
                    ]

            logger.info(
                f"GUARD OK: {len(placed_orders)} ordens confirmadas no SoDEX — "
                f"SELLs: {[s for s in _removed_in_changes if s in placed_syms]} | "
                f"BUYs: {[s for s in _added_in_changes if s in placed_syms]}"
            )

        # Salva as ordens executadas (com order IDs) no banco como evidência
        proposal.execution_orders = executed_orders
        # Registra cada ordem como TradeExecution com rastreabilidade de taxas.
        # Fee estimada 0.1% do notional (padrao SoDEX). Investidor paga via reducao do NAV.
        if not dry_run:
            removed_syms = [c["symbol"] for c in proposal.changes if c.get("action") == "remove"]
            added_syms   = [c["symbol"] for c in proposal.changes if c.get("action") == "add"]
            if removed_syms and added_syms:
                rsuffix = " (" + ", ".join(removed_syms) + " -> " + ", ".join(added_syms) + ")"
            elif removed_syms:
                rsuffix = " (saida: " + ", ".join(removed_syms) + ")"
            elif added_syms:
                rsuffix = " (entrada: " + ", ".join(added_syms) + ")"
            else:
                rsuffix = ""
            for order in executed_orders:
                if order.get("status") not in ("placed", "dry_run"):
                    continue
                sym      = order["symbol"].replace("-USDC", "")
                notional = float(order.get("usd_value", 0))
                fee_usd  = round(notional * 0.001, 6)
                slabel   = "SELL" if order["side"] == "SELL" else "BUY"
                reason   = "Rebalance #" + str(proposal_id) + ": " + slabel + " " + sym + rsuffix
                trade = TradeExecution(
                    source="rebalance", proposal_id=proposal_id,
                    index_id=proposal.index_id, network_mode=network_mode,
                    symbol=sym, side=order["side"].lower(),
                    quantity=float(order.get("quantity", 0)),
                    price_usd=float(order.get("price", 0)),
                    notional_usd=notional, fee_usd=fee_usd, fee_pct=0.001,
                    fee_reason=reason, status="filled",
                    order_id=order.get("order_id", ""),
                    executed_at=datetime.utcnow(),
                )
                db.add(trade)
                logger.info("TradeExecution: " + reason + " | notional=$" + str(round(notional,2)) + " fee=$" + str(fee_usd))


        # 4. Busca preços ao vivo para inicializar price_at_nav_ref de novos tokens
        live_tickers = await get_all_tickers()

        # 5. Atualiza constituents no banco
        for change in proposal.changes:
            if dry_run: continue  # nao atualiza basket em simulacao
            # Filtra por network_mode para evitar atualizar a rede errada
            constituent = db.query(IndexConstituent).filter(
                IndexConstituent.index_id == proposal.index_id,
                IndexConstituent.symbol == change["symbol"],
                IndexConstituent.network_mode == network_mode,
            ).first()

            if change["action"] == "remove":
                if constituent:
                    # Marca como fora da cesta (mantém histórico de preços)
                    constituent.weight = 0.0
                    constituent.in_basket = False

            else:
                # Entrada ou reweight
                if constituent:
                    # Token já existe (cesta ou candidato) — atualiza peso e promove para cesta
                    constituent.weight = change["new_weight"]
                    constituent.in_basket = True
                else:
                    # Token novo — cria constituinte no banco
                    ticker = live_tickers.get(change["symbol"]) or live_tickers.get(change["symbol"] + "-USDC")
                    live_price = 0.0
                    if ticker:
                        try:
                            live_price = float(ticker.get("lastPrice") or 0)
                        except Exception:
                            live_price = 0.0

                    constituent = IndexConstituent(
                        index_id=proposal.index_id,
                        symbol=change["symbol"],
                        name=change.get("name") or change["symbol"],
                        weight=change["new_weight"],
                        current_price_usd=live_price,
                        price_at_nav_ref=live_price,   # inicializa com preço ao vivo
                        market_cap_usd=0.0,
                        volume_24h_usd=0.0,
                        price_change_7d=0.0,
                        price_change_30d=0.0,
                        ai_rationale=change.get("rationale") or "",
                        is_stablecoin=False,
                        network_mode=network_mode,
                        in_basket=True,
                    )
                    db.add(constituent)
                    logger.info(
                        f"Rebalancer: novo token {change['symbol']} criado no banco "
                        f"(price_at_nav_ref=${live_price:.4f}, rede={network_mode})"
                    )

        proposal.status = "executed" if not dry_run else "approved"  # dry_run nao muda status
        if not dry_run:
            proposal.executed_at = datetime.utcnow()

        # Update index last_rebalanced_at
        idx = db.query(AlphaIndex).filter(AlphaIndex.id == proposal.index_id).first()
        if idx:
            idx.last_rebalanced_at = datetime.utcnow()
            idx.rebalance_summary = proposal.ai_rationale

        db.commit()
        logger.success(f"Proposal {proposal_id} executed successfully")

    except Exception as e:
        logger.error(f"Proposal execution failed: {e}")
        # CRITICO: rollback desfaz mudancas pendentes (basket) antes de commitar
        try:
            db.rollback()
            _p = db.query(RebalanceProposal).filter_by(id=proposal_id).first()
            if _p:
                _p.status = "failed"
                _p.execution_error = str(e)[:2000]
            db.commit()
        except Exception as _ce:
            logger.error(f"Erro ao salvar status failed: {_ce}")
        raise
