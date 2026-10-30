import os
import time
import json
import hashlib
import logging
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Header
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import datetime
import uuid
from eth_account.messages import encode_defunct
from eth_account import Account

logger = logging.getLogger(__name__)

from database import get_db
from models import RebalanceProposal, AlphaIndex, AgentActivityLog, Subscriber, SubscriberPortfolio, DepositTransaction, TradeExecution
from services.sodex import get_portfolio_snapshot, get_trade_history

router = APIRouter(prefix="/api/admin", tags=["admin"])

_SIGNATURE_MAX_AGE = 3600  # 1 hora de validade da assinatura
_ADMIN_WALLET = "0x1a3ade798b60bd6e99ff3d84367cc7913115031c"  # founder wallet (lowercase)


class WalletAuthRequest(BaseModel):
    address: str
    message: str
    signature: str


@router.post("/auth")
def wallet_auth(req: WalletAuthRequest):
    """Verifica assinatura EIP-191 e retorna o endereço verificado."""
    try:
        msg_encoded = encode_defunct(text=req.message)
        recovered = Account.recover_message(msg_encoded, signature=req.signature)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid signature")

    if recovered.lower() != req.address.lower():
        raise HTTPException(status_code=401, detail="Signature does not match address")

    # Verifica timestamp na mensagem para evitar replay attacks
    try:
        ts = int(req.message.split("ts:")[-1].strip())
        if abs(time.time() - ts) > _SIGNATURE_MAX_AGE:
            raise HTTPException(status_code=401, detail="Signature expired")
    except (ValueError, IndexError):
        raise HTTPException(status_code=400, detail="Invalid message format")

    # Verifica se é a carteira admin — nega acesso a qualquer outra carteira
    if recovered.lower() != _ADMIN_WALLET:
        raise HTTPException(status_code=403, detail="Access denied: not the admin wallet")

    return {"verified": True, "address": recovered}


def require_admin(x_wallet_address: str = Header(default=""), x_signature: str = Header(default=""), x_sign_message: str = Header(default="")):
    """Verifica assinatura de wallet em cada request protegido."""
    if not x_wallet_address or not x_signature or not x_sign_message:
        raise HTTPException(status_code=401, detail="Wallet authentication required")
    try:
        msg_encoded = encode_defunct(text=x_sign_message)
        recovered = Account.recover_message(msg_encoded, signature=x_signature)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid signature")

    if recovered.lower() != x_wallet_address.lower():
        raise HTTPException(status_code=401, detail="Signature does not match address")

    if recovered.lower() != _ADMIN_WALLET:
        raise HTTPException(status_code=403, detail="Access denied: not the admin wallet")

    try:
        ts = int(x_sign_message.split("ts:")[-1].strip())
        if abs(time.time() - ts) > _SIGNATURE_MAX_AGE:
            raise HTTPException(status_code=401, detail="Signature expired — please re-authenticate")
    except (ValueError, IndexError):
        raise HTTPException(status_code=400, detail="Invalid message format")


@router.get("/proposals")
def list_proposals(db: Session = Depends(get_db), _: None = Depends(require_admin)):
    proposals = db.query(RebalanceProposal).order_by(
        RebalanceProposal.proposed_at.desc()
    ).limit(50).all()

    result = []
    for p in proposals:
        index = db.query(AlphaIndex).filter(AlphaIndex.id == p.index_id).first()
        result.append({
            "id": p.id,
            "index_id": p.index_id,
            "index_name": index.name if index else p.index_id,
            "status": p.status,
            "trigger": p.trigger,
            "proposed_at": p.proposed_at.isoformat() if p.proposed_at else None,
            "approved_at": p.approved_at.isoformat() if p.approved_at else None,
            "executed_at": p.executed_at.isoformat() if p.executed_at else None,
            "changes": p.changes or [],
            "ai_rationale": p.ai_rationale,
        })

    return {"data": result}


@router.post("/proposals/{proposal_id}/approve")
def approve_proposal(proposal_id: int, db: Session = Depends(get_db), _: None = Depends(require_admin)):
    proposal = db.query(RebalanceProposal).filter(
        RebalanceProposal.id == proposal_id
    ).first()
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found")
    if proposal.status != "pending":
        raise HTTPException(status_code=400, detail=f"Proposal is already {proposal.status}")

    proposal.status = "approved"
    proposal.approved_at = datetime.utcnow()

    log = AgentActivityLog(
        id=str(uuid.uuid4()),
        index_id=proposal.index_id,
        agent="rebalancer",
        action="rebalance",
        description=f"Rebalance proposal #{proposal_id} approved by founder. Trigger: {proposal.trigger}",
        timestamp=datetime.utcnow(),
    )
    db.add(log)
    db.commit()

    return {"success": True, "proposal_id": proposal_id, "status": "approved"}


@router.post("/proposals/{proposal_id}/reject")
def reject_proposal(proposal_id: int, db: Session = Depends(get_db), _: None = Depends(require_admin)):
    proposal = db.query(RebalanceProposal).filter(
        RebalanceProposal.id == proposal_id
    ).first()
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found")
    if proposal.status != "pending":
        raise HTTPException(status_code=400, detail=f"Proposal is already {proposal.status}")

    proposal.status = "rejected"
    db.commit()

    return {"success": True, "proposal_id": proposal_id, "status": "rejected"}


class RunRebalancerRequest(BaseModel):
    dry_run: bool = True


@router.post("/run-rebalancer")
async def run_rebalancer(background_tasks: BackgroundTasks, req: RunRebalancerRequest, db: Session = Depends(get_db), _: None = Depends(require_admin)):
    """Dispara o ciclo completo do Rebalancer agent em background (retorna imediatamente)."""
    from agents.rebalancer import check_and_propose_rebalances
    background_tasks.add_task(check_and_propose_rebalances)
    pending = db.query(func.count(RebalanceProposal.id)).filter(
        RebalanceProposal.status == "pending"
    ).scalar() or 0
    return {"success": True, "message": "Rebalancer iniciado em background — verifique Proposals em instantes", "pending_proposals": pending}


@router.post("/run-scout")
async def run_scout(background_tasks: BackgroundTasks, _: None = Depends(require_admin), db: Session = Depends(get_db)):
    """Dispara o Scout agent em background (retorna imediatamente)."""
    from agents.scout import run_all_indexes
    background_tasks.add_task(run_all_indexes)
    pending = db.query(func.count(RebalanceProposal.id)).filter(
        RebalanceProposal.status == "pending"
    ).scalar() or 0
    return {"success": True, "message": "Scout iniciado em background — verifique Proposals em alguns minutos", "pending_proposals": pending}


_AUDIT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "audit", "proposals"))


def _write_audit_record(proposal) -> dict:
    """Persiste proposta executada em JSON com hash SHA-256 para auditoria externa."""
    os.makedirs(_AUDIT_DIR, exist_ok=True)

    changes = proposal.changes or []
    if isinstance(changes, str):
        try:
            changes = json.loads(changes)
        except Exception:
            changes = []

    record = {
        "proposal_id": proposal.id,
        "index_id":    proposal.index_id,
        "trigger":     proposal.trigger,
        "status":      proposal.status,
        "proposed_at": proposal.proposed_at.isoformat() if proposal.proposed_at else None,
        "approved_at": proposal.approved_at.isoformat() if proposal.approved_at else None,
        "executed_at": proposal.executed_at.isoformat() if proposal.executed_at else None,
        "changes":     changes,
        "ai_rationale": proposal.ai_rationale or "",
        "network_mode": proposal.network_mode or "mainnet",
        "audited_at":  datetime.utcnow().isoformat(),
    }

    canonical = json.dumps(record, sort_keys=True, indent=2)
    sha256    = hashlib.sha256(canonical.encode()).hexdigest()
    record["sha256"] = sha256

    ts       = proposal.executed_at.strftime("%Y%m%d_%H%M%S") if proposal.executed_at else "unknown"
    filename = f"proposal_{proposal.id}_{ts}.json"
    filepath = os.path.join(_AUDIT_DIR, filename)

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2)

    logger.info(f"Audit record written: {filename} (sha256={sha256[:16]}…)")
    return {"path": filepath, "sha256": sha256}


class ExecuteProposalRequest(BaseModel):
    dry_run: bool = False


@router.post("/proposals/{proposal_id}/execute")
async def execute_proposal(
    proposal_id: int,
    req: ExecuteProposalRequest,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    """Executa proposta aprovada via SoDEX (dry_run=False envia ordens reais)."""
    from agents.rebalancer import apply_proposal
    proposal = db.query(RebalanceProposal).filter(RebalanceProposal.id == proposal_id).first()
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found")
    if proposal.status not in ("approved", "pending"):
        raise HTTPException(status_code=400, detail=f"Proposal status is '{proposal.status}' — must be 'approved' to execute")

    # Auto-approve if still pending and founder is explicitly executing
    if proposal.status == "pending":
        proposal.status = "approved"
        proposal.approved_at = datetime.utcnow()
        db.commit()

    try:
        await apply_proposal(proposal_id, db, dry_run=req.dry_run)
        db.refresh(proposal)

        audit_record   = None
        onchain_result = None

        if not req.dry_run and proposal.status == "executed":
            # Gap 3: escrever audit record com SHA-256
            try:
                audit_record = _write_audit_record(proposal)
            except Exception as ae:
                logger.warning(f"Audit record failed (non-critical): {ae}")

            # Gap 4: emitir evento on-chain na rede Base
            try:
                from services.onchain_logger import emit_rebalance_event
                onchain_result = await emit_rebalance_event(proposal)
            except Exception as oe:
                logger.warning(f"On-chain emission failed (non-critical): {oe}")

        return {
            "success":        True,
            "proposal_id":    proposal_id,
            "status":         proposal.status,
            "dry_run":        req.dry_run,
            "orders_count":   len(proposal.execution_orders or []),
            "execution_orders": proposal.execution_orders or [],
            "audit":          audit_record,
            "onchain":        onchain_result,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/portfolio")
async def admin_portfolio(network_mode: str = "mainnet", _: None = Depends(require_admin)):
    """Snapshot atual do portfolio na SoDEX: saldos, valores USD, pesos."""
    try:
        testnet = (network_mode == "testnet")
        snapshot = await get_portfolio_snapshot(testnet=testnet)
        return {"data": snapshot}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/trades")
async def admin_trades(network_mode: str = "mainnet", limit: int = 50, db: Session = Depends(get_db), _: None = Depends(require_admin)):
    """Histórico de trades executados na SoDEX.
    Mainnet: busca trades reais na SoDEX API.
    Testnet: lê ordens simuladas (dry_run) salvas na agent_activity.
    """
    try:
        testnet = (network_mode == "testnet")

        if testnet:
            logs = db.query(AgentActivityLog).filter(
                AgentActivityLog.agent == "deposit_monitor",
                AgentActivityLog.action == "tokens_purchased",
            ).order_by(AgentActivityLog.timestamp.desc()).limit(limit).all()

            trades = []
            for log in logs:
                data = log.data or {}
                orders = data.get("orders", [])
                # investor_wallet gravado no log desde a correção; fallback via deposit_transaction mais próximo em timestamp
                wallet = data.get("investor_wallet", "")
                if not wallet and log.timestamp:
                    dtxs = db.query(DepositTransaction).filter(
                        DepositTransaction.index_id == log.index_id,
                        DepositTransaction.network_mode == "testnet",
                    ).all()
                    if dtxs:
                        log_ts = log.timestamp.replace(tzinfo=None)
                        best = min(dtxs, key=lambda x: abs(
                            ((x.created_at if isinstance(x.created_at, datetime) else datetime.fromisoformat(str(x.created_at).split("+")[0].split("Z")[0])).replace(tzinfo=None) - log_ts).total_seconds()
                        ))
                        sub = db.query(Subscriber).filter(Subscriber.id == best.subscriber_id).first()
                        wallet = sub.wallet_address if sub else ""
                for o in orders:
                    raw_status = o.get("status", "")
                    if raw_status in ("dry_run", "placed"):
                        display_status = "simulated"
                    elif "skip" in raw_status:
                        display_status = "skipped"
                    else:
                        continue
                    trades.append({
                        "symbol":           o.get("symbol", ""),
                        "side":             o.get("side", "buy"),
                        "quantity":         float(o.get("quantity", 0)),
                        "price":            float(o.get("price", 0)) if o.get("price") else 0.0,
                        "usd_value":        round(float(o.get("usd_value", 0)), 2),
                        "status":           display_status,
                        "index_id":         log.index_id,
                        "timestamp":        log.timestamp.isoformat() if log.timestamp else "",
                        "is_simulated":     True,
                        "investor_wallet":  wallet,
                        "allocated_usd":    round(float(data.get("allocated_usd", 0)), 2),
                    })
            return {"data": trades, "count": len(trades)}

        # Mainnet: lê do histórico permanente local (SoDEX API só retém ~30 dias)
        rows = (
            db.query(TradeExecution)
            .filter(TradeExecution.network_mode == "mainnet")
            .order_by(TradeExecution.executed_at.desc())
            .limit(limit)
            .all()
        )
        trades = []
        for r in rows:
            # Busca wallet do investidor via portfolio → subscriber
            wallet = ""
            if r.portfolio_id:
                p = db.query(SubscriberPortfolio).filter(SubscriberPortfolio.id == r.portfolio_id).first()
                if p:
                    sub = db.query(Subscriber).filter(Subscriber.id == p.subscriber_id).first()
                    wallet = sub.wallet_address if sub else ""
            trades.append({
                "symbol":          r.symbol,
                "side":            r.side,
                "quantity":        r.quantity,
                "price":           r.price_usd,
                "usd_value":       round(r.notional_usd, 2),
                "status":          r.status,
                "skip_reason":     r.skip_reason,
                "source":          r.source,
                "index_id":        r.index_id,
                "order_id":        r.order_id,
                "timestamp":       r.executed_at.isoformat() if r.executed_at else "",
                "is_simulated":    False,
                "investor_wallet": wallet,
            })
        return {"data": trades, "count": len(trades)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/run-nav-update")
async def run_nav_update(background_tasks: BackgroundTasks, _: None = Depends(require_admin)):
    """Dispara atualização de NAV em background (retorna imediatamente)."""
    from services.nav_updater import update_all_navs
    background_tasks.add_task(update_all_navs)
    return {"success": True, "message": "NAV update iniciado em background — preços e portfolios serão atualizados em ~1min"}


@router.get("/investors")
async def admin_investors(
    network_mode: str = "mainnet",
    page: int = 1,
    per_page: int = 25,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    """
    Lista investimentos individuais — 1 linha por DepositTransaction.
    Cada depósito é tratado como investimento distinto, mesmo que o mesmo
    investidor tenha feito múltiplos depósitos no mesmo índice.
    Quantidade de tokens usa posições reais do SoDEX × cota do pool.
    """
    from models import IndexConstituent

    # Posições reais do SoDEX — fonte verdade para quantidades de tokens
    # Keyed por símbolo normalizado (sem 'v' prefix e sem pontos): defissi, aave, uni, link
    sodex_positions: dict = {}
    try:
        snap = await get_portfolio_snapshot()
        for pos in snap.get("positions", []):
            raw = pos.get("asset", "")
            asset = raw[1:] if raw.startswith("v") else raw
            key = asset.replace(".", "").lower()
            sodex_positions[key] = {
                "amount":    float(pos.get("amount", 0)),
                "usd_value": float(pos.get("usd_value", 0)),
            }
    except Exception:
        pass  # fallback para estimativa se SoDEX indisponível

    # Todos os portfolios para calcular NAV atual e total de shares por índice
    all_portfolios = (
        db.query(SubscriberPortfolio)
        .filter(SubscriberPortfolio.network_mode == network_mode)
        .all()
    )

    # Mapa (subscriber_id, index_id) → portfolio
    portfolio_map: dict = {}
    for p in all_portfolios:
        portfolio_map[(p.subscriber_id, p.index_id)] = p

    # Total de shares por índice (somando todos os portfolios do índice)
    index_total_shares: dict = {}
    for p in all_portfolios:
        iid = p.index_id
        index_total_shares[iid] = index_total_shares.get(iid, 0.0) + float(p.index_tokens_held or 0)

    # Constituintes da cesta por índice (cache)
    basket_cache: dict = {}

    # Todos os depósitos confirmados, do mais recente ao mais antigo
    all_txs = (
        db.query(DepositTransaction)
        .filter(DepositTransaction.network_mode == network_mode)
        .order_by(DepositTransaction.created_at.desc())
        .all()
    )

    rows = []
    for tx in all_txs:
        sub = db.query(Subscriber).filter(Subscriber.id == tx.subscriber_id).first()
        if not sub:
            continue

        portfolio = portfolio_map.get((tx.subscriber_id, tx.index_id))
        if not portfolio:
            continue

        idx = db.query(AlphaIndex).filter(AlphaIndex.id == tx.index_id).first()

        # NAV atual do portfolio (valor por share)
        port_shares = float(portfolio.index_tokens_held or 0)
        port_value  = float(portfolio.current_value_usd or 0)
        current_nav = (port_value / port_shares) if port_shares > 0 else 0.0

        # Valor atual proporcional às cotas deste depósito específico
        tx_shares = float(tx.shares_issued or 0)
        deposited = float(tx.amount_usd or 0)
        current   = tx_shares * current_nav
        pnl       = current - deposited
        pnl_pct   = (pnl / deposited * 100) if deposited > 0 else 0.0

        # % do pool total do índice para este depósito
        tot_shares = index_total_shares.get(tx.index_id, 1.0)
        pool_pct   = (tx_shares / tot_shares * 100) if tot_shares > 0 else 0.0

        # Cesta proporcional ao valor atual deste depósito
        if tx.index_id not in basket_cache:
            basket_cache[tx.index_id] = (
                db.query(IndexConstituent)
                .filter(
                    IndexConstituent.index_id == tx.index_id,
                    IndexConstituent.in_basket == True,
                    IndexConstituent.network_mode == network_mode,
                )
                .order_by(IndexConstituent.weight.desc())
                .all()
            )
        basket_items = []
        for c in basket_cache[tx.index_id]:
            price    = float(c.current_price_usd or 0)
            sym_norm = c.symbol.replace(".", "").lower()
            sodex_p  = sodex_positions.get(sym_norm)

            if sodex_p and sodex_p["amount"] > 0:
                # Posição real do SoDEX × cota proporcional deste depósito
                total_qty = sodex_p["amount"]
                est_qty   = total_qty * (pool_pct / 100)
                total_usd = sodex_p["usd_value"]
                if total_usd == 0 and price > 0:
                    total_usd = total_qty * price  # DEFIssi: SoDEX retorna $0 — usa preço ticker
                est_usd = total_usd * (pool_pct / 100)
            else:
                # Fallback: estimar por valor × peso quando SoDEX indisponível
                est_usd = current * (c.weight / 100) if current > 0 and c.weight else 0
                est_qty = (est_usd / price) if price > 0 else 0

            basket_items.append({
                "symbol":    c.symbol,
                "weight":    c.weight,
                "price":     round(price, 4),
                "est_usd":   round(est_usd, 2),
                "est_qty":   round(est_qty, 6),
                "change_7d": round(float(c.price_change_7d or 0), 2),
            })

        rows.append({
            "id":                str(tx.id),            # ID do depósito (único por investimento)
            "portfolio_id":      str(portfolio.id),
            "wallet_address":    sub.wallet_address,
            "index_id":          tx.index_id,
            "index_name":        idx.name if idx else tx.index_id,
            "deposited_usd":     round(deposited, 2),
            "current_value_usd": round(current, 2),
            "pnl_usd":           round(pnl, 2),
            "pnl_pct":           round(pnl_pct, 2),
            "shares":            round(tx_shares, 6),
            "pool_share_pct":    round(pool_pct, 2),
            "is_pro":            sub.is_pro or False,
            "nav_at_buy":        round(float(tx.nav_at_purchase or 0), 6),
            "high_water_mark":   round(float(portfolio.high_water_mark_usd or 0), 2),
            "deposit_date":      tx.created_at.isoformat() if tx.created_at else None,
            "buy_confirmed":     tx.buy_confirmed or False,
            "tx_hash":           tx.tx_hash or None,
            "basket":            basket_items,
        })

    total_dep   = sum(r["deposited_usd"] for r in rows)
    total_cur   = sum(r["current_value_usd"] for r in rows)
    total_pnl   = total_cur - total_dep
    total_count = len(rows)
    per_page    = max(1, min(per_page, 100))
    start       = (page - 1) * per_page
    total_pages = max(1, (total_count + per_page - 1) // per_page)

    return {
        "data": {
            "portfolios":          rows[start: start + per_page],
            "total_deposited_usd": round(total_dep, 2),
            "total_current_usd":   round(total_cur, 2),
            "total_pnl_usd":       round(total_pnl, 2),
            "total_pnl_pct":       round((total_pnl / total_dep * 100) if total_dep > 0 else 0, 2),
            "count":               total_count,
            "page":                page,
            "per_page":            per_page,
            "total_pages":         total_pages,
        }
    }


@router.get("/fund-wallet")
async def admin_fund_wallet(network_mode: str = "mainnet", _: None = Depends(require_admin)):
    """Retorna saldos ETH e USDC da fund wallet na rede especificada."""
    from services.deposit_monitor import get_fund_wallet_info
    try:
        info = await get_fund_wallet_info(network_mode)
        return {"data": info}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/stats")
def admin_stats(network_mode: str = "mainnet", db: Session = Depends(get_db), _: None = Depends(require_admin)):
    # AUM and investor count filtered by network
    portfolios = db.query(SubscriberPortfolio).filter(
        SubscriberPortfolio.network_mode == network_mode
    ).all()

    total_aum = sum(p.current_value_usd for p in portfolios)
    sub_ids = list({p.subscriber_id for p in portfolios})
    total_subs = len(sub_ids)
    pro_subs = 0
    if sub_ids:
        pro_subs = db.query(func.count(Subscriber.id)).filter(
            Subscriber.id.in_(sub_ids), Subscriber.is_pro == True
        ).scalar() or 0

    pending_proposals = db.query(func.count(RebalanceProposal.id)).filter(
        RebalanceProposal.status == "pending"
    ).scalar() or 0

    indexes = db.query(AlphaIndex).filter(AlphaIndex.is_active == True).all()
    index_summary = []
    for i in indexes:
        idx_portfolios = [p for p in portfolios if p.index_id == i.id]
        index_summary.append({
            "id": i.id,
            "name": i.name,
            "aum_usd": round(sum(p.current_value_usd for p in idx_portfolios), 2),
            "subscriber_count": len({p.subscriber_id for p in idx_portfolios}),
            "return_30d_pct": i.return_30d_pct,
            "last_rebalanced_at": i.last_rebalanced_at.isoformat() if i.last_rebalanced_at else None,
        })

    return {
        "data": {
            "total_subscribers": total_subs,
            "pro_subscribers": pro_subs,
            "total_aum_usd": round(total_aum, 2),
            "pending_proposals": pending_proposals,
            "indexes": index_summary,
            "network_mode": network_mode,
        }
    }


@router.get("/report")
def admin_report(network_mode: str = "mainnet", db: Session = Depends(get_db), _: None = Depends(require_admin)):
    """Relatório gerencial consolidado da plataforma."""
    now = datetime.utcnow()

    indexes = db.query(AlphaIndex).filter(AlphaIndex.is_active == True).all()
    all_portfolios = db.query(SubscriberPortfolio).all()
    mainnet_portfolios = [p for p in all_portfolios if p.network_mode == "mainnet"]
    testnet_portfolios = [p for p in all_portfolios if p.network_mode == "testnet"]
    mainnet_ids = {p.subscriber_id for p in mainnet_portfolios}
    testnet_ids = {p.subscriber_id for p in testnet_portfolios}
    all_ids = mainnet_ids | testnet_ids

    # IDs e portfolios filtrados pela rede selecionada
    selected_portfolios = mainnet_portfolios if network_mode == "mainnet" else testnet_portfolios
    selected_ids = mainnet_ids if network_mode == "mainnet" else testnet_ids

    index_summary = []
    for idx in indexes:
        idx_portfolios = [p for p in selected_portfolios if p.index_id == idx.id]
        index_summary.append({
            "id": idx.id,
            "name": idx.name,
            "aum_usd": round(sum(p.current_value_usd or 0 for p in idx_portfolios), 2),
            "nav_usd": round(idx.nav_usd or 1, 4),
            "return_7d_pct": round(idx.return_7d_pct or 0, 2),
            "return_30d_pct": round(idx.return_30d_pct or 0, 2),
            "total_return_pct": round(idx.total_return_pct or 0, 2),
            "subscriber_count": len({p.subscriber_id for p in idx_portfolios}),
            "management_fee_pct": idx.management_fee_pct or 0.75,
            "last_rebalanced_at": idx.last_rebalanced_at.isoformat() if idx.last_rebalanced_at else None,
        })

    pro_count = 0
    if selected_ids:
        pro_count = db.query(func.count(Subscriber.id)).filter(
            Subscriber.id.in_(list(selected_ids)), Subscriber.is_pro == True
        ).scalar() or 0

    proposals = db.query(RebalanceProposal).all()
    by_status: dict = {}
    by_trigger: dict = {}
    for p in proposals:
        by_status[p.status] = by_status.get(p.status, 0) + 1
        t = p.trigger or "unknown"
        by_trigger[t] = by_trigger.get(t, 0) + 1

    executed = [p for p in proposals if p.status == "executed" and p.executed_at and p.proposed_at]
    avg_hours = None
    if executed:
        deltas = [(p.executed_at - p.proposed_at).total_seconds() / 3600 for p in executed]
        avg_hours = round(sum(deltas) / len(deltas), 1)

    recent_all = db.query(AgentActivityLog).order_by(AgentActivityLog.timestamp.desc()).limit(50).all()
    activity = []
    for a in recent_all:
        a_data = a.data or {}
        log_net = a_data.get("network_mode")
        if log_net and log_net != network_mode:
            continue
        activity.append({
            "agent": a.agent,
            "action": a.action,
            "description": (a.description or "")[:120],
            "timestamp": a.timestamp.isoformat() if a.timestamp else None,
        })
        if len(activity) >= 10:
            break

    return {
        "data": {
            "generated_at": now.isoformat() + "Z",
            "platform": {
                "network_mode": network_mode,
                "total_aum_usd": round(sum(p.current_value_usd or 0 for p in selected_portfolios), 2),
                "total_indexes": len(indexes),
                "total_investors": len(selected_ids),
                "pro_investors": pro_count,
                "mainnet": {
                    "investors": len(mainnet_ids),
                    "aum_usd": round(sum(p.current_value_usd or 0 for p in mainnet_portfolios), 2),
                },
                "testnet": {
                    "investors": len(testnet_ids),
                    "aum_usd": round(sum(p.current_value_usd or 0 for p in testnet_portfolios), 2),
                },
            },
            "indexes": index_summary,
            "proposals": {
                "total": len(proposals),
                "by_status": by_status,
                "by_trigger": by_trigger,
                "avg_approval_hours": avg_hours,
            },
            "recent_activity": activity,
        }
    }


@router.get("/movements")
def admin_movements(
    network_mode: str = "mainnet",
    limit: int = 50,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    """Histórico de movimentações do fundo: depósitos, estornos, saques, créditos manuais."""
    MOVEMENT_ACTIONS = ["deposit_detected", "deposit_refunded", "withdrawal_executed", "manual_credit"]
    logs = (
        db.query(AgentActivityLog)
        .filter(AgentActivityLog.action.in_(MOVEMENT_ACTIONS))
        .order_by(AgentActivityLog.timestamp.desc())
        .limit(limit)
        .all()
    )

    movements = []
    for log in logs:
        data = log.data or {}
        log_net = data.get("network_mode", "mainnet")
        if log_net != network_mode:
            continue

        mov_type = {
            "deposit_detected": "deposit",
            "deposit_refunded": "refund",
            "withdrawal_executed": "withdrawal",
            "manual_credit": "manual",
        }.get(log.action, "other")

        movements.append({
            "id": log.id,
            "type": mov_type,
            "action": log.action,
            "amount_usd": data.get("amount_usd", 0),
            "wallet": data.get("from") or data.get("wallet_address") or data.get("wallet", ""),
            "index_id": log.index_id or "",
            "tx_hash": data.get("tx_hash", ""),
            "refund_tx": data.get("refund_tx", ""),
            "refund_ok": data.get("refund_ok"),
            "refund_basescan": data.get("refund_basescan", ""),
            "basescan": data.get("basescan", ""),
            "manual_credit": data.get("manual_credit", False),
            "reason": data.get("reason", ""),
            "description": (log.description or "")[:180],
            "timestamp": log.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ") if log.timestamp else "",
            "network_mode": log_net,
        })

    return {"movements": movements}


@router.get("/alerts")
async def admin_alerts(db: Session = Depends(get_db), _: None = Depends(require_admin)):
    """Retorna alertas do sistema: RPC health, tokens sem preco, scout parado, propostas pendentes."""
    import datetime as dt
    from models import IndexConstituent, ScoutReport, RebalanceProposal

    alerts = []
    now = dt.datetime.utcnow()

    # 1. RPC status
    try:
        from services.deposit_monitor import get_rpc_status
        for net_name, status in get_rpc_status().items():
            if not status.get("healthy", True):
                fc = status.get("fail_count", 0)
                alerts.append({
                    "id": f"rpc_{net_name}",
                    "severity": "critical",
                    "category": "rpc",
                    "title": f"RPC {net_name} offline",
                    "message": f"Todos os endpoints falhando ({fc} tentativas consecutivas). Ultimo erro: {status.get('last_error', '?')}",
                    "since": status.get("last_error_at"),
                })
    except Exception:
        pass

    # 2. Tokens sem preco (nao stablecoin)
    try:
        zero_price = (
            db.query(IndexConstituent)
            .filter(
                IndexConstituent.current_price_usd <= 0.0001,
                IndexConstituent.is_stablecoin == False,
            )
            .all()
        )
        for t in zero_price:
            alerts.append({
                "id": f"price_{t.symbol}_{t.index_id}",
                "severity": "critical",
                "category": "price",
                "title": f"{t.symbol} com preco zero",
                "message": f"{t.symbol} no indice '{t.index_id}' tem preco ${(t.current_price_usd or 0):.6f}. Verificar coingecko_id: '{t.coingecko_id}'.",
                "since": None,
            })
    except Exception:
        pass

    # 3. Scout estagnado (> 25h sem rodar)
    try:
        last_scout = db.query(func.max(ScoutReport.run_at)).scalar()
        if last_scout:
            age_h = (now - last_scout).total_seconds() / 3600
            if age_h > 25:
                alerts.append({
                    "id": "scout_stale",
                    "severity": "warning",
                    "category": "agent",
                    "title": "Scout parado",
                    "message": f"Ultimo run do Scout ha {age_h:.0f}h. Esperado: diario as 06:00 UTC.",
                    "since": last_scout.isoformat() if last_scout else None,
                })
    except Exception:
        pass

    # 4. Propostas pendentes
    try:
        pending_count = (
            db.query(func.count(RebalanceProposal.id))
            .filter(RebalanceProposal.status == "pending")
            .scalar()
        ) or 0
        if pending_count > 0:
            alerts.append({
                "id": "pending_proposals",
                "severity": "info",
                "category": "proposals",
                "title": f"{pending_count} proposta(s) pendente(s)",
                "message": f"Ha {pending_count} proposta(s) de rebalanceamento aguardando aprovacao.",
                "since": None,
            })
    except Exception:
        pass

    # 5. ETH gas — verifica saldo e transações possíveis
    try:
        from services.deposit_monitor import get_fund_wallet_info
        winfo = await get_fund_wallet_info("mainnet")
        possible = winfo.get("possible_txs")
        eth_bal  = winfo.get("eth_balance") or 0
        if possible is not None:
            if possible < 50:
                alerts.append({
                    "id": "eth_gas_critical",
                    "severity": "critical",
                    "category": "gas",
                    "title": "ETH crítico — repor gas urgente",
                    "message": (
                        f"Fund wallet com apenas {eth_bal:.6f} ETH na Base chain. "
                        f"Restam ~{possible} transações antes de esgotar. "
                        "Deposite ETH em 0x935b...1D0b para continuar operando."
                    ),
                    "since": None,
                })
            elif possible < 200:
                alerts.append({
                    "id": "eth_gas_warning",
                    "severity": "warning",
                    "category": "gas",
                    "title": "ETH baixo para gas",
                    "message": (
                        f"Fund wallet com {eth_bal:.6f} ETH (~{possible} transações restantes). "
                        "Considere repor ETH na Base chain em breve."
                    ),
                    "since": None,
                })
    except Exception:
        pass

    return {
        "alerts": alerts,
        "healthy": all(a["severity"] != "critical" for a in alerts),
        "checked_at": now.isoformat() + "Z",
    }


# ── Fee Management Endpoints ──────────────────────────────────────────────────

@router.get("/fees/summary")
async def fees_summary(
    _: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Resumo de taxas coletadas e pendentes."""
    import json as _json

    # Histórico de fees já coletadas
    collected = (
        db.query(AgentActivityLog)
        .filter(AgentActivityLog.agent == "fee_manager")
        .order_by(AgentActivityLog.timestamp.desc())
        .all()
    )

    total_mgmt = 0.0
    total_perf = 0.0
    history = []
    for log in collected:
        data = {}
        if log.data:
            try:
                data = log.data if isinstance(log.data, dict) else _json.loads(log.data)
            except Exception:
                pass
        fee_val = data.get("fee_amount_usd", 0.0) or 0.0
        fee_type = data.get("type", "")
        if "management" in log.action:
            total_mgmt += fee_val
        elif "performance" in log.action:
            total_perf += fee_val
        history.append({
            "id": log.id,
            "action": log.action,
            "type": fee_type,
            "fee_amount_usd": fee_val,
            "index_id": log.index_id,
            "index_name": data.get("index_name", log.index_id),
            "timestamp": log.timestamp.isoformat() if log.timestamp else None,
            "description": log.description,
        })

    # Taxas pendentes — simula o que seria cobrado agora
    from services.fee_manager import _monthly_mgmt_rate, PERFORMANCE_FEE_PCT
    pending_mgmt = []
    pending_perf = []

    indexes = db.query(AlphaIndex).filter(AlphaIndex.is_active == True).all()
    monthly_rate = _monthly_mgmt_rate()
    for idx in indexes:
        aum = idx.aum_usd or 0.0
        if aum > 0:
            pending_mgmt.append({
                "index_id": idx.id,
                "index_name": idx.name,
                "aum_usd": aum,
                "fee_amount_usd": round(aum * monthly_rate, 4),
            })

    portfolios = db.query(SubscriberPortfolio).all()
    for p in portfolios:
        hwm = p.high_water_mark_usd or p.deposited_usd or 0.0
        current = p.current_value_usd or 0.0
        if current > hwm:
            gain = current - hwm
            fee = round(gain * (PERFORMANCE_FEE_PCT / 100.0), 4)
            if fee >= 0.01:
                idx = next((i for i in indexes if i.id == p.index_id), None)
                pending_perf.append({
                    "portfolio_id": p.id,
                    "index_name": idx.name if idx else p.index_id,
                    "current_value_usd": current,
                    "high_water_mark_usd": hwm,
                    "gain_above_hwm": round(gain, 4),
                    "fee_amount_usd": fee,
                })

    return {
        "collected": {
            "management_total_usd": round(total_mgmt, 4),
            "performance_total_usd": round(total_perf, 4),
            "grand_total_usd": round(total_mgmt + total_perf, 4),
            "count": len(collected),
            "history": history,
        },
        "pending": {
            "management": pending_mgmt,
            "management_total_usd": round(sum(p["fee_amount_usd"] for p in pending_mgmt), 4),
            "performance": pending_perf,
            "performance_total_usd": round(sum(p["fee_amount_usd"] for p in pending_perf), 4),
        },
        "config": {
            "management_fee_annual_pct": 2.0,
            "management_fee_monthly_pct": round(monthly_rate * 100, 4),
            "performance_fee_pct": PERFORMANCE_FEE_PCT,
            "collection_schedule": "Dia 1 de cada mês às 08:00 UTC",
        },
    }


@router.post("/fees/collect")
async def fees_collect(
    dry_run: bool = True,
    _: None = Depends(require_admin),
):
    """Dispara cobrança de fees manualmente. dry_run=true para simulação."""
    from services.fee_manager import run_monthly_fees
    result = await run_monthly_fees(dry_run=dry_run)
    return result


@router.get("/fees/history")
async def fees_history(
    limit: int = 50,
    _: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Histórico completo de cobranças de taxas."""
    logs = (
        db.query(AgentActivityLog)
        .filter(AgentActivityLog.agent == "fee_manager")
        .order_by(AgentActivityLog.timestamp.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": l.id,
            "action": l.action,
            "description": l.description,
            "timestamp": l.timestamp.isoformat() if l.timestamp else None,
            "data": l.data,
        }
        for l in logs
    ]
