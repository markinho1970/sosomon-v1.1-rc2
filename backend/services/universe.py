"""
Universe Service — snapshot diário de todos os ativos SoSoValue+SoDEX.

Coleta candidatos de todos os 4 temas (depin, defi, ai-crypto, rwa), enriquece com
performance completa (roi 7d/30d/3m, marketcap, rank, volume) e calcula volatilidade 30d
(desvio padrão dos retornos diários — usada no campo beta_vs_ssi como proxy de risco).

Nota: endpoint /indices/{ticker}/klines retorna 401 com a chave atual.
Usamos volatilidade normalizada do próprio token como proxy de risco.
"""

import asyncio
import math
from datetime import datetime
from loguru import logger
from sqlalchemy.orm import Session

from models import UniverseAsset, IndexConstituent
from services.sosovalue import (
    get_ssi_candidates_for_theme,
    enrich_candidates_with_performance,
    get_currency_klines,
    THEME_INDEX_MAP,
)
from services.sodex import get_markets

# Inclui "defi" para capturar tokens da cesta do depin-momentum (theme=defi no DB)
THEMES = ["depin", "defi", "ai-crypto", "rwa"]


def _calc_volatility(closes: list) -> float:
    """
    Volatilidade 30d: desvio padrão dos retornos diários dos últimos 30 dias.
    Normalizado: 0.0 = estável, 1.0 = muito volátil (retorno médio diário 1σ > 5%).
    Valor retornado no campo beta_vs_ssi como proxy de risco.
    """
    if len(closes) < 10:
        return 1.0
    recent = closes[-31:] if len(closes) >= 31 else closes
    returns = [(recent[i] - recent[i-1]) / recent[i-1] for i in range(1, len(recent)) if recent[i-1] != 0]
    if len(returns) < 5:
        return 1.0
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / len(returns)
    std = math.sqrt(variance)
    # Normaliza: 5% diário de σ = "1.0 de risco". Escala linear.
    return round(std / 0.05, 2)


async def refresh_universe(db: Session) -> int:
    """
    Coleta e enriquece todos os ativos dos 4 temas. Atualiza universe_assets no DB.
    Retorna número de ativos processados.
    """
    logger.info("Universe refresh: starting")

    # Busca mercados SoDEX — chaves reais: baseCoin, status
    try:
        sodex_markets = await get_markets()
        sodex_map: dict[str, dict] = {}
        for m in (sodex_markets or []):
            # baseCoin pode ser "HYPE", quoteCoin é "USDC"
            base = (m.get("baseCoin") or "").upper()
            if base:
                sodex_map[base] = m
        logger.info(f"Universe: SoDEX markets loaded, {len(sodex_map)} trading pairs")
    except Exception as e:
        logger.warning(f"Universe: failed to get SoDEX markets: {e}")
        sodex_map = {}

    # Busca tokens na cesta atual (todos os índices, mainnet)
    basket_tokens: dict[str, dict] = {}
    constituents = db.query(IndexConstituent).filter(
        IndexConstituent.in_basket == True,
        IndexConstituent.network_mode == "mainnet",
    ).all()
    for c in constituents:
        basket_tokens[c.symbol.upper()] = {
            "in_basket": True,
            "is_anchor": c.is_anchor or False,
            "index_id": c.index_id,
        }
    logger.info(f"Universe: basket tokens: {list(basket_tokens.keys())}")

    processed = 0
    seen: set[tuple] = set()

    for theme in THEMES:
        logger.info(f"Universe: processing theme={theme}")
        candidates = await get_ssi_candidates_for_theme(theme)
        if not candidates:
            logger.warning(f"Universe: no candidates for theme={theme}")
            continue

        # Enriquecer com performance (snapshot + klines para roi)
        candidates = await enrich_candidates_with_performance(candidates)

        for token in candidates:
            symbol = token.get("symbol", "").upper()
            if not symbol:
                continue
            key = (symbol, theme)
            if key in seen:
                continue
            seen.add(key)

            # Klines para volatilidade e high_3m (cache do enrich — sem custo extra)
            cid = token.get("currency_id", "")
            token_closes = []
            try:
                klines = await get_currency_klines(cid, limit=91) if cid else []
                token_closes = [float(k.get("close") or k.get("c") or 0) for k in klines]
                token_closes = [c for c in token_closes if c > 0]
                await asyncio.sleep(3.5)
            except Exception as e:
                logger.debug(f"Universe [{symbol}]: klines error: {e}")

            volatility = _calc_volatility(token_closes)
            high_3m = max(token_closes) if token_closes else 0.0
            current_price = token_closes[-1] if token_closes else 0.0
            dist_from_high = round((current_price - high_3m) / high_3m * 100, 2) if high_3m > 0 else 0.0

            marketcap = float(token.get("marketcap", 0) or 0)
            volume_24h = float(token.get("volume_24h", 0) or 0)
            vol_mcap_ratio = round(volume_24h / marketcap, 4) if marketcap > 0 else 0.0

            # SoDEX: status field é "TRADING" ou "HALT"
            sodex_info = sodex_map.get(symbol, {})
            sodex_listed = bool(sodex_info)
            sodex_status = sodex_info.get("status", "UNKNOWN") if sodex_listed else "UNKNOWN"

            basket_info = basket_tokens.get(symbol, {})

            existing = db.query(UniverseAsset).filter(
                UniverseAsset.symbol == symbol,
                UniverseAsset.theme == theme,
            ).first()

            if existing:
                row = existing
            else:
                row = UniverseAsset(symbol=symbol, theme=theme)
                db.add(row)

            row.name = token.get("name", symbol)
            row.currency_id = cid
            row.ssi_weight = round(float(token.get("ssi_weight", 0)) * 100, 2)
            row.marketcap = marketcap
            row.mcap_rank = int(token.get("mcap_rank", 999))
            row.volume_24h = volume_24h
            row.vol_mcap_ratio = vol_mcap_ratio
            row.roi_7d = float(token.get("roi_7d", 0) or 0)
            row.roi_30d = float(token.get("roi_30d", 0) or 0)
            row.roi_3m = float(token.get("roi_3m", 0) or 0)
            row.high_3m = high_3m
            row.dist_from_high = dist_from_high
            row.beta_vs_ssi = volatility  # proxy de risco (σ normalizada 30d)
            row.current_price = current_price
            row.in_basket = basket_info.get("in_basket", False)
            row.is_anchor = basket_info.get("is_anchor", False)
            row.sodex_listed = sodex_listed
            row.sodex_status = sodex_status
            row.updated_at = datetime.utcnow()

            processed += 1

        db.commit()
        logger.info(f"Universe: theme={theme} done, {len(candidates)} tokens")

    logger.info(f"Universe refresh complete: {processed} assets saved")
    return processed


def get_universe_snapshot(db: Session, theme: str = None) -> list:
    """Lê universe_assets do DB. Opcional: filtra por tema."""
    q = db.query(UniverseAsset)
    if theme:
        q = q.filter(UniverseAsset.theme == theme)
    return q.order_by(UniverseAsset.theme, UniverseAsset.mcap_rank).all()
