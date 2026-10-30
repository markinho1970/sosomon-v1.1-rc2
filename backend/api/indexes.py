from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import datetime, timedelta

from database import get_db
from models import AlphaIndex, IndexConstituent, RebalanceProposal, SubscriberPortfolio, AgentActivityLog, PortfolioSnapshot
from schemas import IndexOut, ConstituentOut, ApiResponse

router = APIRouter(prefix="/api/indexes", tags=["indexes"])



def _calc_period_return(db, index_id: str, days: int) -> float:
    # nav_per_token e identico para todos os investidores no mesmo indice.
    # Basta um portfolio_id para nao multiplicar linhas por investidor.
    from sqlalchemy import text as sa_text
    cutoff = datetime.utcnow() - timedelta(days=days)
    pid_row = db.execute(
        sa_text("SELECT id FROM portfolios WHERE index_id = :iid AND is_active = TRUE LIMIT 1"),
        {"iid": index_id}
    ).fetchone()
    if not pid_row:
        return 0.0
    pid = pid_row[0]
    snaps = db.query(PortfolioSnapshot).filter(
        PortfolioSnapshot.portfolio_id == pid,
        PortfolioSnapshot.snapshot_at >= cutoff,
        PortfolioSnapshot.nav_per_token != None,
        PortfolioSnapshot.nav_per_token > 0,
    ).order_by(PortfolioSnapshot.snapshot_at.asc()).all()
    if len(snaps) < 2:
        return 0.0
    first_nav = snaps[0].nav_per_token
    last_nav = snaps[-1].nav_per_token
    return round((last_nav - first_nav) / first_nav * 100, 2) if first_nav > 0 else 0.0

def _filter_constituents(idx, network_mode: str) -> list:
    seen = set()
    result = []
    for c in idx.constituents:
        if c.network_mode != network_mode:
            continue
        if not getattr(c, "in_basket", True):
            continue
        if c.symbol in seen:
            continue
        seen.add(c.symbol)
        result.append(ConstituentOut.model_validate(c))
    return result


@router.get("", response_model=ApiResponse)
def list_indexes(network_mode: str = Query("mainnet"), db: Session = Depends(get_db)):
    indexes = db.query(AlphaIndex).filter(AlphaIndex.is_active == True).all()
    results = []
    for idx in indexes:
        aum = db.query(func.sum(SubscriberPortfolio.current_value_usd)).filter(
            SubscriberPortfolio.index_id == idx.id,
            SubscriberPortfolio.network_mode == network_mode,
        ).scalar() or 0.0
        r60d = _calc_period_return(db, idx.id, 60)
        r90d = _calc_period_return(db, idx.id, 90)
        idx_out = IndexOut.model_validate(idx).model_copy(update={
            "aum_usd": round(aum, 2),
            "return_60d_pct": r60d,
            "return_90d_pct": r90d,
            "constituents": _filter_constituents(idx, network_mode),
        })
        results.append(idx_out)
    return ApiResponse(data=results)


@router.get("/{slug}", response_model=ApiResponse)
def get_index(slug: str, network_mode: str = Query("mainnet"), db: Session = Depends(get_db)):
    idx = db.query(AlphaIndex).filter(AlphaIndex.slug == slug).first()
    if not idx:
        raise HTTPException(status_code=404, detail="Index not found")
    aum = db.query(func.sum(SubscriberPortfolio.current_value_usd)).filter(
        SubscriberPortfolio.index_id == idx.id,
        SubscriberPortfolio.network_mode == network_mode,
    ).scalar() or 0.0
    idx_out = IndexOut.model_validate(idx).model_copy(update={
        "aum_usd": round(aum, 2),
        "constituents": _filter_constituents(idx, network_mode),
    })
    return ApiResponse(data=idx_out)


@router.get("/{slug}/risk", response_model=ApiResponse)
def get_index_risk(slug: str, network_mode: str = Query("mainnet"), db: Session = Depends(get_db)):
    """Dados quantitativos de risco por índice — para auditoria pelo investidor."""
    import json
    idx = db.query(AlphaIndex).filter(AlphaIndex.slug == slug).first()
    if not idx:
        raise HTTPException(status_code=404, detail="Index not found")

    constituents = db.query(IndexConstituent).filter(
        IndexConstituent.index_id == idx.id,
        IndexConstituent.network_mode == network_mode,
    ).all()

    tokens_at_risk = []
    for c in constituents:
        chg_7d = c.price_change_7d or 0.0
        ejection_risk_pct = round(max(0.0, -chg_7d / 40.0 * 100.0), 1)
        tokens_at_risk.append({
            "symbol":            c.symbol,
            "weight":            c.weight,
            "price_usd":         c.current_price_usd,
            "change_7d_pct":     round(chg_7d, 2),
            "change_30d_pct":    round(c.price_change_30d or 0.0, 2),
            "ejection_risk_pct": ejection_risk_pct,
            "at_risk":           ejection_risk_pct >= 50,
            "ai_rationale":      c.ai_rationale or "",
        })

    # Gap 2: tokens em cooldown de ejeção (excluídos pelo scout nos últimos 90 dias)
    cutoff = datetime.utcnow() - timedelta(days=90)
    cooldown_logs = (
        db.query(AgentActivityLog)
        .filter(
            AgentActivityLog.index_id == idx.id,
            AgentActivityLog.agent == "scout",
            AgentActivityLog.action == "exclusion",
            AgentActivityLog.timestamp >= cutoff,
        )
        .order_by(AgentActivityLog.timestamp.desc())
        .all()
    )
    cooldown_tokens = []
    seen_symbols: set = set()
    now = datetime.utcnow()
    for log in cooldown_logs:
        sym = log.token_symbol
        if sym and sym not in seen_symbols:
            seen_symbols.add(sym)
            reentry = log.timestamp + timedelta(days=90)
            cooldown_tokens.append({
                "symbol":         sym,
                "ejected_at":     log.timestamp.isoformat(),
                "reentry_date":   reentry.isoformat(),
                "days_remaining": max(0, (reentry - now).days),
                "reason":         log.description,
            })

    last_proposal = db.query(RebalanceProposal).filter(
        RebalanceProposal.index_id == idx.id,
        RebalanceProposal.network_mode == network_mode,
    ).order_by(RebalanceProposal.id.desc()).first()

    proposal_data = None
    if last_proposal:
        try:
            changes = json.loads(last_proposal.changes) if isinstance(last_proposal.changes, str) else last_proposal.changes
        except Exception:
            changes = []
        proposal_data = {
            "status":      last_proposal.status,
            "trigger":     last_proposal.trigger,
            "proposed_at": last_proposal.proposed_at.isoformat() if last_proposal.proposed_at else None,
            "changes":     changes or [],
            "ai_rationale": last_proposal.ai_rationale or "",
        }

    # Concentration risk (HHI — Herfindahl-Hirschman Index)
    basket = [c for c in constituents if getattr(c, "in_basket", True) and c.weight and c.weight > 0]
    concentration = None
    if basket:
        total_w = sum(c.weight for c in basket) or 1.0
        norm_w = [c.weight / total_w for c in basket]
        hhi = round(sum(w * w for w in norm_w), 3)
        dominant = max(basket, key=lambda c: c.weight)
        concentration = {
            "hhi": hhi,
            "effective_n": round(1 / hhi, 1) if hhi > 0 else 0,
            "level": "high" if hhi > 0.35 else "medium" if hhi > 0.20 else "low",
            "max_token": dominant.symbol,
            "max_weight_pct": round(dominant.weight, 1),
            "token_count": len(basket),
        }

    return ApiResponse(data={
        "index_id":              idx.id,
        "network_mode":          network_mode,
        "stablecoin_buffer_pct": idx.stablecoin_buffer_pct or 0.0,
        "risk_rules": {
            "ejection_threshold_7d_pct": -40,
            "buffer_trigger_low_pct":    25,
            "buffer_low_allocation_pct": 30,
            "buffer_trigger_critical_pct": 15,
            "buffer_critical_allocation_pct": 50,
            "ejection_cooldown_days":    90,
            "max_single_token_weight":   25,
        },
        "tokens":           tokens_at_risk,
        "cooldown_tokens":  cooldown_tokens,
        "last_proposal":    proposal_data,
        "concentration":    concentration,
    })
