"""
Universe API — expõe snapshot diário de todos os ativos SoSoValue+SoDEX.
Endpoint público (sem autenticação): dados educativos/transparência.
"""

from fastapi import APIRouter, Depends, BackgroundTasks, Query
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
from loguru import logger

from database import get_db
from services.universe import get_universe_snapshot, refresh_universe

router = APIRouter(prefix="/api/universe", tags=["universe"])


def _row_to_dict(row) -> dict:
    return {
        "symbol": row.symbol,
        "name": row.name or row.symbol,
        "theme": row.theme,
        "ssi_weight": row.ssi_weight,
        "marketcap": row.marketcap,
        "mcap_rank": row.mcap_rank,
        "volume_24h": row.volume_24h,
        "vol_mcap_ratio": row.vol_mcap_ratio,
        "roi_7d": row.roi_7d,
        "roi_30d": row.roi_30d,
        "roi_3m": row.roi_3m,
        "high_3m": row.high_3m,
        "dist_from_high": row.dist_from_high,
        "beta_vs_ssi": row.beta_vs_ssi,
        "current_price": row.current_price,
        "in_basket": row.in_basket,
        "is_anchor": row.is_anchor,
        "sodex_listed": row.sodex_listed,
        "sodex_status": row.sodex_status,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@router.get("")
def get_universe(
    theme: str = Query(None, description="Filtrar por tema: depin, ai-crypto, rwa"),
    db: Session = Depends(get_db),
):
    rows = get_universe_snapshot(db, theme=theme)
    assets = [_row_to_dict(r) for r in rows]
    updated_at = rows[0].updated_at.isoformat() if rows else None
    return {
        "data": assets,
        "total": len(assets),
        "updated_at": updated_at,
        "themes": ["depin", "ai-crypto", "rwa"],
    }


@router.post("/refresh")
async def trigger_refresh(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Força refresh manual do universo (processo em background, pode demorar ~5min)."""
    async def _do_refresh():
        from database import SessionLocal
        db2 = SessionLocal()
        try:
            count = await refresh_universe(db2)
            logger.info(f"Universe manual refresh done: {count} assets")
        except Exception as e:
            logger.error(f"Universe manual refresh failed: {e}")
        finally:
            db2.close()

    background_tasks.add_task(_do_refresh)
    return {"success": True, "message": "Refresh iniciado em background (~5 min)"}
