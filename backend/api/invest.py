from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel
from datetime import datetime, timezone, timedelta
from loguru import logger
import os, uuid, time as _time

from eth_account.messages import encode_defunct
from eth_account import Account

from database import get_db
from sqlalchemy import func
from models import AlphaIndex, Subscriber, SubscriberPortfolio, AgentActivityLog, InvestmentIntent, InvestmentConsent, IndexConstituent, PortfolioSnapshot, RedemptionTransaction, DepositTransaction, TradeExecution

router = APIRouter(prefix="/api/invest", tags=["invest"])

_NETWORK_CONFIGS = {
    "mainnet": {
        "mode": "mainnet", "chain_id": 8453, "network": "base",
        "usdc_contract": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "basescan_url": "https://basescan.org", "rpc_url": "https://mainnet.base.org",
        "fund_wallet": os.getenv("FUND_WALLET_ADDRESS", ""),
    },
    "testnet": {
        "mode": "testnet", "chain_id": 84532, "network": "base-sepolia",
        "usdc_contract": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
        "basescan_url": "https://sepolia.basescan.org", "rpc_url": "https://sepolia.base.org",
        "fund_wallet": os.getenv("TESTNET_FUND_WALLET_ADDRESS", os.getenv("FUND_WALLET_ADDRESS", "")),
    },
}


@router.get("/network-config")
async def get_network_config(mode: str = "mainnet"):
    """Retorna configuração da rede solicitada (mainnet ou testnet)."""
    return _NETWORK_CONFIGS.get(mode, _NETWORK_CONFIGS["mainnet"])


@router.get("/fund-wallet")
async def get_fund_wallet_endpoint(mode: str = "mainnet"):
    """Retorna endereço e saldo USDC da fund wallet na rede solicitada."""
    from services.deposit_monitor import get_fund_wallet_info
    return await get_fund_wallet_info(mode)


TERMS_VERSION = "v1.0"


class ConsentRequest(BaseModel):
    wallet_address: str
    index_id: str
    signature: str
    signed_message: str
    network_mode: str = "mainnet"  # "mainnet" | "testnet"


class IntentRequest(BaseModel):
    wallet_address: str
    index_id: str


@router.post("/register-consent")
def register_consent(req: ConsentRequest, request: Request, db: Session = Depends(get_db)):
    """
    Verifica assinatura EIP-191 do termo de risco e registra o consentimento.
    Também cria InvestmentIntent para associar o próximo depósito ao índice escolhido.
    """
    # Verificar assinatura
    try:
        msg_encoded = encode_defunct(text=req.signed_message)
        recovered = Account.recover_message(msg_encoded, signature=req.signature)
    except Exception:
        raise HTTPException(status_code=400, detail="Assinatura inválida")

    if recovered.lower() != req.wallet_address.lower():
        raise HTTPException(status_code=401, detail="Assinatura não corresponde à carteira")

    # Verificar que o índice existe
    index = db.query(AlphaIndex).filter(
        AlphaIndex.id == req.index_id,
        AlphaIndex.is_active == True,
    ).first()
    if not index:
        raise HTTPException(status_code=404, detail="Índice não encontrado")

    wallet = req.wallet_address.lower()

    # Salvar consentimento
    consent = InvestmentConsent(
        wallet_address=wallet,
        index_id=req.index_id,
        terms_version=TERMS_VERSION,
        signature=req.signature,
        signed_message=req.signed_message,
        signed_at=datetime.now(timezone.utc),
        ip_hint=request.client.host if request.client else None,
    )
    db.add(consent)

    network_mode = req.network_mode if req.network_mode in ("mainnet", "testnet") else "mainnet"

    # Registrar intenção de depósito (válida por 24h) com rede correta
    intent = InvestmentIntent(
        wallet_address=wallet,
        index_id=req.index_id,
        network_mode=network_mode,
        created_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        fulfilled=False,
    )
    db.add(intent)
    db.commit()

    return {
        "success": True,
        "wallet_address": wallet,
        "index_id": req.index_id,
        "index_name": index.name,
        "terms_version": TERMS_VERSION,
        "intent_expires_in_hours": 24,
        "message": f"Termo assinado e intenção registrada. Seu próximo depósito será creditado no índice '{index.name}'.",
    }


@router.get("/check-consent/{wallet_address}/{index_id}")
def check_consent(wallet_address: str, index_id: str, db: Session = Depends(get_db)):
    """Verifica se o usuário já tem consentimento registrado para este índice."""
    wallet = wallet_address.lower()
    consent = db.query(InvestmentConsent).filter(
        InvestmentConsent.wallet_address == wallet,
        InvestmentConsent.index_id == index_id,
        InvestmentConsent.terms_version == TERMS_VERSION,
    ).order_by(InvestmentConsent.signed_at.desc()).first()

    intent = db.query(InvestmentIntent).filter(
        InvestmentIntent.wallet_address == wallet,
        InvestmentIntent.index_id == index_id,
        InvestmentIntent.fulfilled == False,
    ).order_by(InvestmentIntent.created_at.desc()).first()

    return {
        "has_consent": consent is not None,
        "consent_date": consent.signed_at.isoformat() if consent else None,
        "terms_version": consent.terms_version if consent else None,
        "has_active_intent": intent is not None,
        "intent_expires_at": intent.expires_at.isoformat() if intent and intent.expires_at else None,
    }


@router.get("/fund-wallet")
async def get_fund_wallet(mode: str = "mainnet"):
    """Returns fund wallet address and current USDC balance on the specified network."""
    from services.deposit_monitor import get_fund_wallet_info
    return await get_fund_wallet_info(mode)


class InvestRequest(BaseModel):
    wallet_address: str
    index_id: str
    amount_usd: float


@router.post("")
def invest(req: InvestRequest, db: Session = Depends(get_db)):
    # Endpoint desativado — investimentos só são registrados via deposit_monitor
    # (detecção on-chain de transferências USDC na rede Base)
    raise HTTPException(
        status_code=410,
        detail="Direct investment via API is disabled. Send USDC to the fund wallet on Base network. Your portfolio is credited automatically within 2-3 minutes.",
    )
    if req.amount_usd < 5:
        raise HTTPException(status_code=400, detail="Minimum investment is $5")

    index = db.query(AlphaIndex).filter(
        AlphaIndex.id == req.index_id,
        AlphaIndex.is_active == True,
    ).first()
    if not index:
        raise HTTPException(status_code=404, detail="Index not found")

    # Get or create subscriber
    req.wallet_address = req.wallet_address.lower()
    subscriber = db.query(Subscriber).filter(
        Subscriber.wallet_address == req.wallet_address
    ).first()
    if not subscriber:
        subscriber = Subscriber(
            id=str(uuid.uuid4()),
            wallet_address=req.wallet_address,
            referral_code=str(uuid.uuid4())[:8].upper(),
        )
        db.add(subscriber)
        db.flush()

    tokens_to_mint = req.amount_usd / max(index.nav_usd, 0.0001)

    portfolio = db.query(SubscriberPortfolio).filter(
        SubscriberPortfolio.subscriber_id == subscriber.id,
        SubscriberPortfolio.index_id == req.index_id,
    ).first()

    if portfolio:
        portfolio.deposited_usd += req.amount_usd
        portfolio.current_value_usd += req.amount_usd
        portfolio.index_tokens_held += tokens_to_mint
        portfolio.last_updated_at = datetime.utcnow()
        if portfolio.current_value_usd > portfolio.high_water_mark_usd:
            portfolio.high_water_mark_usd = portfolio.current_value_usd
    else:
        portfolio = SubscriberPortfolio(
            subscriber_id=subscriber.id,
            index_id=req.index_id,
            deposited_usd=req.amount_usd,
            current_value_usd=req.amount_usd,
            index_tokens_held=tokens_to_mint,
            high_water_mark_usd=req.amount_usd,
        )
        db.add(portfolio)
        index.subscriber_count = (index.subscriber_count or 0) + 1

    index.aum_usd = (index.aum_usd or 0) + req.amount_usd
    db.commit()

    return {
        "success": True,
        "wallet_address": req.wallet_address,
        "index_id": req.index_id,
        "amount_usd": req.amount_usd,
        "tokens_received": round(tokens_to_mint, 6),
        "nav_usd": index.nav_usd,
    }


class WithdrawRequest(BaseModel):
    wallet_address: str
    index_id: str
    amount_usd: float
    network_mode: str = "mainnet"


class WithdrawPreviewRequest(BaseModel):
    wallet_address: str
    index_id: str
    amount_usd: float
    network_mode: str = "mainnet"


@router.post("/withdraw-preview")
def withdraw_preview(req: WithdrawPreviewRequest, db: Session = Depends(get_db)):
    """Calcula taxas e P&L sem executar nada. Usar antes do saque real."""
    if req.amount_usd < 1:
        raise HTTPException(status_code=400, detail="Valor mínimo de saque: $1")

    subscriber = db.query(Subscriber).filter(
        Subscriber.wallet_address == req.wallet_address.lower()
    ).first()
    if not subscriber:
        raise HTTPException(status_code=404, detail="Investidor não encontrado")

    portfolio = db.query(SubscriberPortfolio).filter(
        SubscriberPortfolio.subscriber_id == subscriber.id,
        SubscriberPortfolio.index_id == req.index_id,
        SubscriberPortfolio.network_mode == getattr(req, "network_mode", "mainnet"),
    ).first()
    if not portfolio:
        raise HTTPException(status_code=404, detail="Sem posição neste index")

    db_value = portfolio.current_value_usd or 0
    # Tolerância de 2%: NAV live no frontend pode diferir do DB (atualização horária)
    # Se o valor enviado está dentro da tolerância, trata como saque total
    if req.amount_usd > db_value * 1.02:
        raise HTTPException(status_code=400, detail=f"Valor excede saldo atual (${db_value:.2f})")
    amount_usd = min(req.amount_usd, db_value)

    from services.withdrawal_executor import preview_withdrawal
    try:
        preview = preview_withdrawal(portfolio, None, amount_usd)
        preview["to_wallet"] = subscriber.wallet_address
        return preview
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


class WithdrawExecuteRequest(BaseModel):
    wallet_address: str
    index_id: str
    amount_usd: float
    simulate: bool = False   # True = sandbox, não transmite para blockchain
    network_mode: str = "mainnet"


@router.post("/withdraw-execute")
async def withdraw_execute(req: WithdrawExecuteRequest, db: Session = Depends(get_db)):
    """
    Executa o saque on-chain (ou simula se simulate=True).

    simulate=True → roda todas as verificações e assina mas NÃO transmite.
    Útil para testar o fluxo completo sem risco.
    """
    if req.amount_usd < 1:
        raise HTTPException(status_code=400, detail="Valor mínimo: $1")

    subscriber = db.query(Subscriber).filter(
        Subscriber.wallet_address == req.wallet_address.lower()
    ).first()
    if not subscriber:
        raise HTTPException(status_code=404, detail="Investidor não encontrado")

    portfolio = db.query(SubscriberPortfolio).filter(
        SubscriberPortfolio.subscriber_id == subscriber.id,
        SubscriberPortfolio.index_id == req.index_id,
        SubscriberPortfolio.network_mode == req.network_mode,
    ).first()
    if not portfolio:
        raise HTTPException(status_code=404, detail="Sem posição neste index")

    db_value = portfolio.current_value_usd or 0
    if req.amount_usd > db_value * 1.02:
        raise HTTPException(status_code=400, detail=f"Valor excede saldo atual (${db_value:.2f})")
    amount_usd = min(req.amount_usd, db_value)

    index = db.query(AlphaIndex).filter(AlphaIndex.id == req.index_id).first()

    # Calcular preview (taxas e P&L)
    from services.withdrawal_executor import preview_withdrawal, execute_withdrawal
    try:
        preview = preview_withdrawal(portfolio, index, amount_usd)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    net_usd = preview["net_usd"]
    if net_usd <= 0 and not req.simulate:
        raise HTTPException(status_code=400, detail="Valor líquido após taxas é zero ou negativo.")

    # Vender tokens no SoDEX proporcionalmente ao saque
    basket = db.query(IndexConstituent).filter(
        IndexConstituent.index_id == req.index_id,
        IndexConstituent.in_basket == True,
        IndexConstituent.network_mode == req.network_mode,
    ).all()

    from services.sodex import execute_sell_for_withdrawal
    sell_result = await execute_sell_for_withdrawal(
        amount_usd   = amount_usd,
        constituents = basket,
        dry_run      = req.simulate,
    )
    logger.info(f"withdraw_execute: sell result — recovered=${sell_result['recovered_usd']:.2f}, skipped=${sell_result['skipped_usd']:.2f}")

    # Aguardar liquidação das ordens no SoDEX (LIMIT orders levam ~5s para preencher)
    if not req.simulate:
        import asyncio
        await asyncio.sleep(8)

    # Executar (ou simular) o envio on-chain
    tx_result = await execute_withdrawal(
        recipient   = subscriber.wallet_address,
        amount_usd  = net_usd,
        simulate    = req.simulate,
    )

    # Só atualiza o banco se a tx foi bem-sucedida E não é simulação
    if tx_result["success"] and not req.simulate:
        nav = index.nav_usd if index and index.nav_usd else 1.0
        tokens_burned = amount_usd / nav
        cost_basis    = (portfolio.avg_cost_basis_per_share or nav) * tokens_burned
        pnl_usd       = preview["pnl_usd"]
        pnl_pct       = preview["pnl_pct"]

        portfolio.current_value_usd  = max(0, (portfolio.current_value_usd or 0) - amount_usd)
        portfolio.index_tokens_held  = max(0, (portfolio.index_tokens_held or 0) - tokens_burned)
        portfolio.total_shares_redeemed = (portfolio.total_shares_redeemed or 0) + tokens_burned
        portfolio.last_updated_at    = datetime.now(timezone.utc)
        if portfolio.current_value_usd < (portfolio.high_water_mark_usd or 0):
            portfolio.high_water_mark_usd = portfolio.current_value_usd

        if index:
            index.aum_usd = max(0, (index.aum_usd or 0) - amount_usd)
            if portfolio.current_value_usd <= 0:
                index.subscriber_count = max(0, (index.subscriber_count or 1) - 1)

        # Registro auditável do resgate
        redemption = RedemptionTransaction(
            subscriber_id        = subscriber.id,
            portfolio_id         = portfolio.id,
            index_id             = req.index_id,
            tx_hash              = tx_result.get("tx_hash") or None,
            amount_usd           = req.amount_usd,
            shares_burned        = round(tokens_burned, 8),
            nav_at_redemption    = round(nav, 6),
            net_usd              = round(net_usd, 4),
            management_fee_usd   = round(preview["management_fee_usd"], 4),
            performance_fee_usd  = round(preview["performance_fee_usd"], 4),
            gas_fee_usd          = round(preview.get("gas_fee_est_usd", 0), 4),
            pnl_usd              = round(pnl_usd, 4),
            pnl_pct              = round(pnl_pct, 4),
            cost_basis_proportional = round(cost_basis, 4),
            is_simulated         = False,
            network_mode         = getattr(portfolio, "network_mode", "mainnet"),
            created_at           = datetime.now(timezone.utc),
        )
        db.add(redemption)

        activity = AgentActivityLog(
            id        = str(uuid.uuid4()),
            index_id  = req.index_id,
            agent     = "withdraw",
            action    = "withdrawal_executed",
            description = (
                f"Saque de ${req.amount_usd:.2f} por {subscriber.wallet_address[:8]}…"
                f" | Líquido: ${net_usd:.2f} | Taxa gestão: ${preview['management_fee_usd']:.4f}"
                f" | Taxa perf: ${preview['performance_fee_usd']:.4f}"
                f" | Tx: {tx_result.get('basescan','')}"
            ),
            timestamp = datetime.now(timezone.utc),
            data      = {
                "tx_hash":          tx_result.get("tx_hash", ""),
                "basescan":         tx_result.get("basescan", ""),
                "to":               subscriber.wallet_address,
                "withdrawal_usd":   req.amount_usd,
                "net_usd":          net_usd,
                "management_fee":   preview["management_fee_usd"],
                "performance_fee":  preview["performance_fee_usd"],
                "gas_fee":          preview["gas_fee_est_usd"],
                "pnl_usd":          pnl_usd,
                "pnl_pct":          pnl_pct,
                "shares_burned":    round(tokens_burned, 8),
                "network":          "base",
                "chain_id":         8453,
            },
        )
        db.add(activity)
        db.commit()

    return {
        "success":           tx_result["success"],
        "simulate":          req.simulate,
        "tx_hash":           tx_result.get("tx_hash"),
        "basescan":          tx_result.get("basescan"),
        "withdrawal_usd":    req.amount_usd,
        "net_usd":           net_usd,
        "management_fee":    preview["management_fee_usd"],
        "performance_fee":   preview["performance_fee_usd"],
        "gas_fee":           preview["gas_fee_est_usd"],
        "pnl_usd":           preview["pnl_usd"],
        "pnl_pct":           preview["pnl_pct"],
        "pnl_label":         preview["pnl_label"],
        "warnings":          preview["warnings"],
        "error":             tx_result.get("error"),
        "checks":            tx_result.get("checks", {}),
        "message":           tx_result.get("message", ""),
    }


class RedeemSharesRequest(BaseModel):
    wallet_address: str
    index_id: str
    shares: float                  # quantidade de cotas a resgatar
    simulate: bool = False
    network_mode: str = "mainnet"


@router.post("/redeem-shares")
async def redeem_shares(req: RedeemSharesRequest, db: Session = Depends(get_db)):
    """
    Resgata uma quantidade específica de cotas (shares) ao invés de um valor em USD.
    O valor em USD equivalente é calculado com o NAV atual: amount_usd = shares × NAV.
    Permite ao investidor resgatar exatamente N cotas sem precisar calcular o USD.
    """
    if req.shares <= 0:
        raise HTTPException(status_code=400, detail="Quantidade de cotas deve ser positiva")

    wallet = req.wallet_address.lower()
    subscriber = db.query(Subscriber).filter(Subscriber.wallet_address == wallet).first()
    if not subscriber:
        raise HTTPException(status_code=404, detail="Investidor não encontrado")

    network_mode = req.network_mode if req.network_mode in ("mainnet", "testnet") else "mainnet"
    portfolio = db.query(SubscriberPortfolio).filter(
        SubscriberPortfolio.subscriber_id == subscriber.id,
        SubscriberPortfolio.index_id == req.index_id,
        SubscriberPortfolio.network_mode == network_mode,
    ).first()
    if not portfolio:
        raise HTTPException(status_code=404, detail="Sem posição neste índice")

    shares_held = portfolio.index_tokens_held or 0.0
    if req.shares > shares_held:
        raise HTTPException(
            status_code=400,
            detail=f"Cotas insuficientes. Você tem {shares_held:.6f} cotas, solicitou {req.shares:.6f}."
        )

    index = db.query(AlphaIndex).filter(AlphaIndex.id == req.index_id).first()
    nav = index.nav_usd if index and index.nav_usd else 1.0

    # Converte cotas → USD e delega ao fluxo normal de saque
    amount_usd = req.shares * nav

    from services.withdrawal_executor import preview_withdrawal, execute_withdrawal
    try:
        preview = preview_withdrawal(portfolio, index, amount_usd)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    net_usd = preview["net_usd"]
    if net_usd <= 0 and not req.simulate:
        raise HTTPException(status_code=400, detail="Valor líquido após taxas é zero ou negativo.")

    tx_result = await execute_withdrawal(
        recipient  = subscriber.wallet_address,
        amount_usd = net_usd,
        simulate   = req.simulate,
    )

    if tx_result["success"] and not req.simulate:
        cost_basis = (portfolio.avg_cost_basis_per_share or nav) * req.shares

        portfolio.current_value_usd     = max(0, (portfolio.current_value_usd or 0) - amount_usd)
        portfolio.index_tokens_held     = max(0, shares_held - req.shares)
        portfolio.total_shares_redeemed = (portfolio.total_shares_redeemed or 0) + req.shares
        portfolio.last_updated_at       = datetime.now(timezone.utc)
        if portfolio.current_value_usd < (portfolio.high_water_mark_usd or 0):
            portfolio.high_water_mark_usd = portfolio.current_value_usd

        if index:
            index.aum_usd = max(0, (index.aum_usd or 0) - amount_usd)
            if portfolio.current_value_usd <= 0:
                index.subscriber_count = max(0, (index.subscriber_count or 1) - 1)

        redemption = RedemptionTransaction(
            subscriber_id           = subscriber.id,
            portfolio_id            = portfolio.id,
            index_id                = req.index_id,
            tx_hash                 = tx_result.get("tx_hash") or None,
            amount_usd              = round(amount_usd, 4),
            shares_burned           = round(req.shares, 8),
            nav_at_redemption       = round(nav, 6),
            net_usd                 = round(net_usd, 4),
            management_fee_usd      = round(preview["management_fee_usd"], 4),
            performance_fee_usd     = round(preview["performance_fee_usd"], 4),
            gas_fee_usd             = round(preview.get("gas_fee_est_usd", 0), 4),
            pnl_usd                 = round(preview["pnl_usd"], 4),
            pnl_pct                 = round(preview["pnl_pct"], 4),
            cost_basis_proportional = round(cost_basis, 4),
            is_simulated            = False,
            network_mode            = network_mode,
            created_at              = datetime.now(timezone.utc),
        )
        db.add(redemption)
        db.add(AgentActivityLog(
            id          = str(uuid.uuid4()),
            index_id    = req.index_id,
            agent       = "withdraw",
            action      = "redemption_by_shares",
            description = (
                f"Resgate de {req.shares:.6f} cotas por {wallet[:8]}… "
                f"| USD: ${amount_usd:.2f} | Líquido: ${net_usd:.2f} "
                f"| NAV: ${nav:.4f}"
            ),
            timestamp   = datetime.now(timezone.utc),
            data        = {
                "tx_hash":       tx_result.get("tx_hash", ""),
                "basescan":      tx_result.get("basescan", ""),
                "shares_burned": round(req.shares, 8),
                "amount_usd":    round(amount_usd, 4),
                "net_usd":       round(net_usd, 4),
                "nav":           round(nav, 6),
                "network_mode":  network_mode,
            },
        ))
        db.commit()

    return {
        "success":          tx_result["success"],
        "simulate":         req.simulate,
        "shares_redeemed":  req.shares,
        "amount_usd":       round(amount_usd, 4),
        "nav_at_redemption": round(nav, 6),
        "net_usd":          net_usd,
        "management_fee":   preview["management_fee_usd"],
        "performance_fee":  preview["performance_fee_usd"],
        "gas_fee":          preview.get("gas_fee_est_usd", 0),
        "pnl_usd":          preview["pnl_usd"],
        "pnl_pct":          preview["pnl_pct"],
        "pnl_label":        preview["pnl_label"],
        "tx_hash":          tx_result.get("tx_hash"),
        "basescan":         tx_result.get("basescan"),
        "warnings":         preview["warnings"],
        "error":            tx_result.get("error"),
        "message":          tx_result.get("message", ""),
    }


@router.post("/withdraw")
def withdraw(req: WithdrawRequest, db: Session = Depends(get_db)):
    # Endpoint legado desativado — usar /withdraw-preview + /withdraw-execute
    raise HTTPException(status_code=410, detail="Use /withdraw-preview e /withdraw-execute")

    req_wallet = req.wallet_address.lower()
    subscriber = db.query(Subscriber).filter(
        Subscriber.wallet_address == req_wallet
    ).first()
    if not subscriber:
        raise HTTPException(status_code=404, detail="No position found")

    portfolio = db.query(SubscriberPortfolio).filter(
        SubscriberPortfolio.subscriber_id == subscriber.id,
        SubscriberPortfolio.index_id == req.index_id,
    ).first()
    if not portfolio:
        raise HTTPException(status_code=404, detail="No position in this index")

    if req.amount_usd > portfolio.current_value_usd:
        raise HTTPException(status_code=400, detail="Insufficient balance")

    index = db.query(AlphaIndex).filter(AlphaIndex.id == req.index_id).first()
    nav = index.nav_usd if index and index.nav_usd else 1.0

    tokens_to_burn = req.amount_usd / nav

    profit_above_hwm = max(0, portfolio.current_value_usd - portfolio.high_water_mark_usd)
    perf_fee = round(min(profit_above_hwm, req.amount_usd) * 0.15, 4)
    net_usd = round(req.amount_usd - perf_fee, 4)

    portfolio.current_value_usd -= req.amount_usd
    portfolio.index_tokens_held = max(0, portfolio.index_tokens_held - tokens_to_burn)
    portfolio.last_updated_at = datetime.utcnow()
    if portfolio.current_value_usd < portfolio.high_water_mark_usd:
        portfolio.high_water_mark_usd = portfolio.current_value_usd

    if index:
        index.aum_usd = max(0, (index.aum_usd or 0) - req.amount_usd)
        if portfolio.current_value_usd <= 0:
            index.subscriber_count = max(0, (index.subscriber_count or 1) - 1)

    db.commit()

    return {
        "success": True,
        "wallet_address": req.wallet_address,
        "index_id": req.index_id,
        "amount_usd": req.amount_usd,
        "tokens_burned": round(tokens_to_burn, 6),
        "performance_fee_usd": perf_fee,
        "net_usd": net_usd,
        "message": f"Withdrawal of ${net_usd} USDC will be sent to your wallet on Base within 24h.",
    }


@router.get("/portfolio/{wallet_address}")
def get_portfolio(wallet_address: str, network_mode: str = "mainnet", db: Session = Depends(get_db)):
    wallet_address = wallet_address.lower()
    subscriber = db.query(Subscriber).filter(
        Subscriber.wallet_address == wallet_address
    ).first()
    if not subscriber:
        return {"portfolios": [], "subscriber": None}

    portfolios = []
    # Filtra portfolios pelo network_mode do request — mainnet e testnet não se misturam
    filtered_portfolios = [
        p for p in subscriber.portfolios
        if getattr(p, "network_mode", "mainnet") == network_mode
    ]
    for p in filtered_portfolios:
        index = db.query(AlphaIndex).filter(AlphaIndex.id == p.index_id).first()
        if not index:
            continue

        all_time_return = (
            ((p.current_value_usd - p.deposited_usd) / p.deposited_usd * 100)
            if p.deposited_usd > 0 else 0
        )
        profit_above_hwm = max(0, p.current_value_usd - p.high_water_mark_usd)
        accrued_fee = round(profit_above_hwm * 0.15, 2)

        nav = index.nav_usd or 1.0
        unrealized_pnl = round((nav - (p.avg_cost_basis_per_share or nav)) * (p.index_tokens_held or 0), 4)

        portfolios.append({
            "index_id": p.index_id,
            "index_name": index.name,
            "theme": index.theme,
            "deposited_usd": p.deposited_usd,
            "current_value_usd": p.current_value_usd,
            "index_tokens_held": p.index_tokens_held,
            "all_time_return_pct": round(all_time_return, 2),
            "return_7d_pct": round(index.return_7d_pct or 0, 2),
            "return_30d_pct": round(index.return_30d_pct or 0, 2),
            "btc_benchmark_30d": round(index.btc_benchmark_30d or 0, 2),
            "high_water_mark_usd": p.high_water_mark_usd,
            "days_invested": p.days_invested,
            "accrued_performance_fee_usd": accrued_fee,
            # Campos de auditoria de cotas
            "nav_at_first_deposit": round(p.nav_at_first_deposit or 1.0, 6),
            "avg_cost_basis_per_share": round(p.avg_cost_basis_per_share or 1.0, 6),
            "total_shares_deposited": round(p.total_shares_deposited or 0.0, 8),
            "total_shares_redeemed": round(p.total_shares_redeemed or 0.0, 8),
            "unrealized_pnl_usd": unrealized_pnl,
        })

    return {
        "portfolios": portfolios,
        "subscriber": {
            "id": subscriber.id,
            "wallet_address": subscriber.wallet_address,
            "is_pro": subscriber.is_pro,
            "days_streak": subscriber.days_streak,
        },
    }


@router.get("/portfolio/{wallet_address}/breakdown")
async def get_portfolio_breakdown(wallet_address: str, network_mode: str = "mainnet", db: Session = Depends(get_db)):
    """Breakdown de holdings por token usando posições reais do SoDEX × cota do pool."""
    from services.sodex import get_portfolio_snapshot
    wallet_address = wallet_address.lower()
    subscriber = db.query(Subscriber).filter(
        Subscriber.wallet_address == wallet_address
    ).first()
    if not subscriber:
        return []

    # Posições reais do SoDEX — fonte verdade para quantidades
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

    # Total de shares por índice (para calcular cota proporcional do investidor)
    all_portfolios = db.query(SubscriberPortfolio).filter(
        SubscriberPortfolio.network_mode == network_mode
    ).all()
    index_total_shares: dict = {}
    for ap in all_portfolios:
        iid = ap.index_id
        index_total_shares[iid] = index_total_shares.get(iid, 0.0) + float(ap.index_tokens_held or 0)

    result = []
    portfolios = [
        p for p in subscriber.portfolios
        if getattr(p, "network_mode", "mainnet") == network_mode
    ]
    for p in portfolios:
        if not (p.index_tokens_held or 0) > 0 and not (p.current_value_usd or 0) > 0:
            continue
        index = db.query(AlphaIndex).filter(AlphaIndex.id == p.index_id).first()
        if not index:
            continue

        # Cota proporcional deste portfolio no pool total do índice
        tot_shares = index_total_shares.get(p.index_id, 1.0)
        pool_pct = float(p.index_tokens_held or 0) / tot_shares if tot_shares > 0 else 1.0

        constituents = db.query(IndexConstituent).filter(
            IndexConstituent.index_id == p.index_id,
            IndexConstituent.in_basket == True,
            IndexConstituent.network_mode == network_mode,
            IndexConstituent.weight > 0,
        ).all()
        tokens = []
        for c in constituents:
            price    = float(c.current_price_usd or 0)
            sym_norm = c.symbol.replace(".", "").lower()
            sodex_p  = sodex_positions.get(sym_norm)

            if sodex_p and sodex_p["amount"] > 0:
                # Posição real SoDEX × cota proporcional deste investidor
                total_qty = sodex_p["amount"]
                qty       = round(total_qty * pool_pct, 6)
                total_usd = sodex_p["usd_value"]
                if total_usd == 0 and price > 0:
                    total_usd = total_qty * price  # DEFIssi: API retorna $0
                usd_value = round(total_usd * pool_pct, 2)
            else:
                # Fallback: estimativa por peso quando SoDEX indisponível
                total_weight = sum(c2.weight for c2 in constituents)
                share = c.weight / total_weight if total_weight > 0 else 0
                usd_value = round(float(p.current_value_usd or 0) * share, 2)
                qty = round(usd_value / price, 6) if price > 0 else 0

            tokens.append({
                "symbol":    c.symbol,
                "name":      c.name,
                "weight":    c.weight,
                "usd_value": usd_value,
                "quantity":  qty,
                "price":     price,
                "change_7d":  round(float(c.price_change_7d  or 0), 2),
                "change_30d": round(float(c.price_change_30d or 0), 2),
            })
        result.append({
            "index_id":   p.index_id,
            "index_name": index.name,
            "total_value": p.current_value_usd,
            "tokens": tokens,
        })
    return result


# ── Live prices — polling endpoint para o dashboard (5s cache) ────────────────
_LIVE_PRICE_CACHE: dict = {}

@router.get("/live-prices")
async def get_live_prices(network_mode: str = "mainnet"):
    """
    Retorna preços ao vivo do SoDEX para todos os tokens negociáveis.
    Cache de 5 segundos para não sobrecarregar a API do SoDEX com polling frequente.
    Chave do preço = símbolo SoDEX sem sufixo (ex: 'vAAVE', 'vDEFI.ssi', 'WSOSO').
    """
    global _LIVE_PRICE_CACHE
    now = _time.monotonic()
    cached = _LIVE_PRICE_CACHE.get(network_mode, {})
    if now - cached.get("at", 0.0) < 5.0 and cached.get("prices"):
        return {"prices": cached["prices"], "cached": True}
    try:
        from services.sodex import get_all_tickers
        tickers = await get_all_tickers(testnet=(network_mode == "testnet"))
        prices: dict = {}
        for key, ticker in tickers.items():
            # get_all_tickers retorna dois formatos:
            # - Par de mercado: "vAAVE_vUSDC" ou "AAVE-USDC" (contém '-' ou '_') → ignorar
            # - Símbolo limpo: "AAVE", "DEFIssi", "MAG7ssi" (sem separador) → usar
            if "-" in key or "_" in key:
                continue
            price = float(ticker.get("lastPrice", ticker.get("c", 0)) or 0)
            if price <= 0:
                continue
            prices[key] = round(price, 8)
        _LIVE_PRICE_CACHE[network_mode] = {"prices": prices, "at": now}
        return {"prices": prices, "cached": False}
    except Exception as e:
        logger.warning(f"live-prices: erro ao buscar tickers SoDEX: {e}")
        # Retorna cache antigo se disponível
        if cached.get("prices"):
            return {"prices": cached["prices"], "cached": True}
        return {"prices": {}, "cached": False}


# ── Insights de médio prazo — oportunidades + riscos de concentração ───────────

@router.get("/insights")
async def get_insights(wallet_address: str, network_mode: str = "mainnet", db: Session = Depends(get_db)):
    """
    Retorna insights personalizados de médio prazo para o investidor:
    - Oportunidades em índices que ainda não investe (outperformance vs BTC)
    - Alertas de alta concentração de risco (HHI) nas cestas atuais
    """
    wallet = wallet_address.lower()
    subscriber = db.query(Subscriber).filter(Subscriber.wallet_address == wallet).first()

    active_index_ids: set = set()
    if subscriber:
        active_portfolios = db.query(SubscriberPortfolio).filter(
            SubscriberPortfolio.subscriber_id == subscriber.id,
            SubscriberPortfolio.network_mode == network_mode,
            SubscriberPortfolio.current_value_usd > 0,
        ).all()
        active_index_ids = {p.index_id for p in active_portfolios}

    all_indexes = db.query(AlphaIndex).filter(AlphaIndex.is_active == True).all()
    insights = []

    # 1. Oportunidades — índices com outperformance vs BTC que o investidor não tem
    for idx in all_indexes:
        if idx.id in active_index_ids:
            continue
        r30 = getattr(idx, "return_30d_pct", None) or 0.0
        r7  = getattr(idx, "return_7d_pct",  None) or 0.0
        btc = getattr(idx, "btc_benchmark_30d", None) or 0.0
        outperf = round(r30 - btc, 2)
        if outperf >= 3.0:
            insights.append({
                "type": "opportunity",
                "index_id":          idx.id,
                "index_slug":        idx.slug,
                "index_name":        idx.name,
                "return_30d_pct":    round(r30, 2),
                "return_7d_pct":     round(r7, 2),
                "btc_benchmark_30d": round(btc, 2),
                "outperformance_pct": outperf,
                "nav_usd":           idx.nav_usd,
                "message": (
                    f"{idx.name} superou o BTC em {outperf:.1f}% nos últimos 30 dias"
                    + (f" · 7d: {r7:+.1f}%" if r7 != 0 else "")
                ),
            })

    # 2. Concentração — HHI nas cestas onde o investidor está
    for idx_id in active_index_ids:
        idx = db.query(AlphaIndex).filter(AlphaIndex.id == idx_id).first()
        if not idx:
            continue
        constituents = db.query(IndexConstituent).filter(
            IndexConstituent.index_id == idx_id,
            IndexConstituent.network_mode == network_mode,
        ).all()
        basket = [c for c in constituents if getattr(c, "in_basket", True) and c.weight and c.weight > 0]
        if not basket:
            continue
        total_w = sum(c.weight for c in basket) or 1.0
        norm_w = [c.weight / total_w for c in basket]
        hhi = sum(w * w for w in norm_w)
        if hhi > 0.35:
            dominant = max(basket, key=lambda c: c.weight)
            insights.append({
                "type": "concentration",
                "index_id":       idx_id,
                "index_slug":     idx.slug,
                "index_name":     idx.name,
                "hhi":            round(hhi, 3),
                "effective_n":    round(1 / hhi, 1),
                "dominant_token": dominant.symbol,
                "max_weight_pct": round(dominant.weight, 1),
                "message": (
                    f"{dominant.symbol} representa {dominant.weight:.0f}% da cesta — "
                    f"alta concentração (HHI={hhi:.2f}, n_efetivo={1/hhi:.1f})"
                ),
            })

    return {"insights": insights, "total": len(insights)}


@router.get("/portfolio/{wallet_address}/history")
def get_portfolio_history(wallet_address: str, network_mode: str = "mainnet", days: int = 30, db: Session = Depends(get_db)):
    """Histórico de valor do portfolio para gráfico de evolução."""
    wallet_address = wallet_address.lower()
    subscriber = db.query(Subscriber).filter(
        Subscriber.wallet_address == wallet_address
    ).first()
    if not subscriber:
        return []

    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(days=days)

    result = []
    portfolios = [
        p for p in subscriber.portfolios
        if getattr(p, "network_mode", "mainnet") == network_mode
    ]
    for p in portfolios:
        index = db.query(AlphaIndex).filter(AlphaIndex.id == p.index_id).first()
        snapshots = db.query(PortfolioSnapshot).filter(
            PortfolioSnapshot.portfolio_id == p.id,
            PortfolioSnapshot.snapshot_at >= cutoff,
        ).order_by(PortfolioSnapshot.snapshot_at.asc()).all()

        # If no snapshots yet, return current value as single data point
        if not snapshots:
            points = [{
                "date": (p.first_invested_at or datetime.utcnow()).strftime("%Y-%m-%dT%H:%M:%S"),
                "value": p.current_value_usd,
                "deposited": p.deposited_usd,
                "nav": index.nav_usd if index else None,
            }]
        else:
            points = [{
                "date": s.snapshot_at.strftime("%Y-%m-%dT%H:%M:%S"),
                "value": s.value_usd,
                "deposited": s.deposited_usd or p.deposited_usd,
                "nav": s.nav_per_token,
            } for s in snapshots]

        result.append({
            "index_id": p.index_id,
            "index_name": index.name if index else p.index_id,
            "theme": index.theme if index else "",
            "current_value": p.current_value_usd,
            "deposited": p.deposited_usd,
            "return_7d_pct": round(index.return_7d_pct or 0, 2) if index else 0,
            "return_30d_pct": round(index.return_30d_pct or 0, 2) if index else 0,
            "btc_benchmark_30d": round(index.btc_benchmark_30d or 0, 2) if index else 0,
            "points": points,
        })
    return result


@router.get("/portfolio/{wallet_address}/transactions")
def get_portfolio_transactions(wallet_address: str, network_mode: str = "mainnet", db: Session = Depends(get_db)):
    """Histórico de depósitos e saques."""
    wallet_address = wallet_address.lower()

    subscriber = db.query(Subscriber).filter(
        Subscriber.wallet_address == wallet_address
    ).first()

    txs = []

    # Depósitos: usa DepositTransaction (tem network_mode — sem risco de misturar testnet/mainnet)
    if subscriber:
        deposits = db.query(DepositTransaction).filter(
            DepositTransaction.subscriber_id == subscriber.id,
            DepositTransaction.network_mode == network_mode,
        ).order_by(DepositTransaction.created_at.desc()).all()
        index_cache = {}
        for tx in deposits:
            if tx.index_id not in index_cache:
                idx = db.query(AlphaIndex).filter(AlphaIndex.id == tx.index_id).first()
                index_cache[tx.index_id] = idx.slug if idx else tx.index_id
            txs.append({
                "type": "deposit",
                "index_id": tx.index_id,
                "index_slug": index_cache[tx.index_id],
                "amount_usd": float(tx.amount_usd or 0),
                "tx_hash": tx.tx_hash or "",
                "timestamp": tx.created_at.strftime("%Y-%m-%dT%H:%M:%S") if tx.created_at else "",
                "status": "completed" if tx.buy_confirmed else "pending",
            })

    # Get withdrawals
    withdrawals = db.query(AgentActivityLog).filter(
        AgentActivityLog.agent == "withdraw",
        AgentActivityLog.action == "withdrawal_executed",
    ).order_by(AgentActivityLog.timestamp.desc()).limit(50).all()
    for a in withdrawals:
        data = a.data or {}
        if (data.get("to") or "").lower() != wallet_address:
            continue
        txs.append({
            "type": "withdrawal",
            "index_id": a.index_id,
            "amount_usd": data.get("withdrawal_usd") or data.get("amount_usd") or 0,
            "net_usd": data.get("net_usd"),
            "tx_hash": data.get("tx_hash", ""),
            "timestamp": a.timestamp.strftime("%Y-%m-%dT%H:%M:%S") if a.timestamp else "",
            "status": "completed",
        })

    txs.sort(key=lambda x: x["timestamp"], reverse=True)
    return txs


@router.get("/portfolio/{wallet_address}/lots")
def get_portfolio_lots(wallet_address: str, network_mode: str = "mainnet", db: Session = Depends(get_db)):
    """Retorna cada depósito confirmado como um lote independente com P&L calculado ao NAV atual.
    Regra: cada DepositTransaction = 1 lote com vida própria (NAV de entrada, P&L, dias)."""
    wallet_address = wallet_address.lower()
    subscriber = db.query(Subscriber).filter(Subscriber.wallet_address == wallet_address).first()
    if not subscriber:
        return []

    deposits = db.query(DepositTransaction).filter(
        DepositTransaction.subscriber_id == subscriber.id,
        DepositTransaction.network_mode == network_mode,
        DepositTransaction.buy_confirmed == True,
    ).order_by(DepositTransaction.created_at.asc()).all()

    index_cache: dict = {}
    # pré-calcula número de lotes por índice
    lot_counter: dict = {}
    lots = []

    for tx in deposits:
        if tx.index_id not in index_cache:
            idx = db.query(AlphaIndex).filter(AlphaIndex.id == tx.index_id).first()
            index_cache[tx.index_id] = idx
            lot_counter[tx.index_id] = 0
        idx = index_cache[tx.index_id]
        if not idx:
            continue

        lot_counter[tx.index_id] += 1
        current_nav = idx.nav_usd or 1.0
        current_value = float(tx.shares_issued or 0) * current_nav
        deposited = float(tx.amount_usd or 0)
        pnl_usd = current_value - deposited
        pnl_pct = (pnl_usd / deposited * 100) if deposited > 0 else 0
        days = (datetime.utcnow() - tx.created_at).days if tx.created_at else 0

        lots.append({
            "lot_id":           tx.id,
            "lot_number":       lot_counter[tx.index_id],
            "index_id":         tx.index_id,
            "index_name":       idx.name,
            "theme":            idx.theme,
            "deposited_usd":    round(deposited, 2),
            "shares_issued":    round(float(tx.shares_issued or 0), 8),
            "nav_at_purchase":  round(float(tx.nav_at_purchase or tx.cost_basis_per_share or 1.0), 6),
            "current_nav":      round(current_nav, 6),
            "current_value_usd": round(current_value, 2),
            "pnl_usd":          round(pnl_usd, 2),
            "pnl_pct":          round(pnl_pct, 2),
            "days_invested":    days,
            "invested_at":      tx.created_at.isoformat() if tx.created_at else "",
            "tx_hash":          tx.tx_hash or "",
            "return_7d_pct":    round(float(idx.return_7d_pct or 0), 2),
            "return_30d_pct":   round(float(idx.return_30d_pct or 0), 2),
            "min_deposit_usd":  round(float(idx.min_deposit_usd or 25), 2),
        })

    return lots


@router.get("/refunds/{wallet_address}")
def get_refunds(wallet_address: str, network_mode: str = "mainnet", db: Session = Depends(get_db)):
    """Retorna estornos recentes para o wallet (últimas 48h)."""
    from datetime import timedelta
    from models import AgentActivityLog
    wallet_address = wallet_address.lower()
    cutoff = datetime.utcnow() - timedelta(hours=48)
    logs = db.query(AgentActivityLog).filter(
        AgentActivityLog.action == "deposit_refunded",
        AgentActivityLog.timestamp >= cutoff,
    ).order_by(AgentActivityLog.timestamp.desc()).limit(10).all()

    refunds = []
    for log in logs:
        data = log.data or {}
        if data.get("from", "").lower() == wallet_address and data.get("network_mode") == network_mode:
            refunds.append({
                "amount_usd": data.get("amount_usd", 0),
                "minimum_usd": data.get("minimum_usd", 5),
                "tx_hash": data.get("tx_hash", ""),
                "timestamp": log.timestamp.isoformat() if log.timestamp else None,
                "description": log.description,
            })
    return {"refunds": refunds}


@router.get("/fee-statement/{wallet_address}")
def get_fee_statement(wallet_address: str, network_mode: str = "mainnet", db: Session = Depends(get_db)):
    """
    Extrato completo de taxas pagas pelo investidor durante o período do investimento.
    Inclui taxas de rebalanceamento (pagas implicitamente via NAV), com data, motivo e valor.
    """
    wallet = wallet_address.lower()

    # Busca portfolios do investidor
    sub = db.query(Subscriber).filter(
        func.lower(Subscriber.wallet_address) == wallet
    ).first()
    if not sub:
        return {"wallet": wallet_address, "portfolios": [], "total_fees_usd": 0.0, "trades": []}

    portfolios = db.query(SubscriberPortfolio).filter(
        SubscriberPortfolio.subscriber_id == sub.id,
        SubscriberPortfolio.network_mode == network_mode,
    ).all()

    portfolio_ids = [p.id for p in portfolios]
    index_ids = list({p.index_id for p in portfolios})

    # Taxas de rebalance: afetam todos os investidores do índice proporcionalmente
    # Buscamos todas as TradeExecution de rebalance dos índices do investidor
    rebalance_trades = db.query(TradeExecution).filter(
        TradeExecution.source == "rebalance",
        TradeExecution.network_mode == network_mode,
        TradeExecution.index_id.in_(index_ids),
    ).order_by(TradeExecution.executed_at.desc()).all()

    # Taxas de depósito: específicas do portfolio do investidor
    deposit_trades = db.query(TradeExecution).filter(
        TradeExecution.source == "deposit",
        TradeExecution.portfolio_id.in_(portfolio_ids),
        TradeExecution.network_mode == network_mode,
    ).order_by(TradeExecution.executed_at.desc()).all()

    all_trades = rebalance_trades + deposit_trades
    all_trades.sort(key=lambda x: x.executed_at or datetime.min, reverse=True)

    trades_out = []
    total_fees = 0.0
    for t in all_trades:
        fee = getattr(t, "fee_usd", 0) or 0
        total_fees += fee
        trades_out.append({
            "date":        t.executed_at.strftime("%Y-%m-%d %H:%M UTC") if t.executed_at else "",
            "source":      t.source,
            "index_id":    t.index_id,
            "symbol":      t.symbol,
            "side":        t.side.upper() if t.side else "",
            "notional_usd": round(t.notional_usd, 4),
            "fee_usd":     round(fee, 6),
            "fee_pct_display": f"{(getattr(t, 'fee_pct', 0.001) or 0.001) * 100:.2f}%",
            "reason":      getattr(t, "fee_reason", "") or t.source,
            "order_id":    t.order_id or "",
            "proposal_id": t.proposal_id,
        })

    return {
        "wallet":          wallet_address,
        "network_mode":    network_mode,
        "total_fees_usd":  round(total_fees, 6),
        "trade_count":     len(trades_out),
        "trades":          trades_out,
    }
