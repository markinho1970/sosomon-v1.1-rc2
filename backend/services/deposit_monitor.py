"""
Deposit Monitor — Base Mainnet (8453) + Base Sepolia Testnet (84532)

Roda dois monitores simultâneos — um por rede.
Cada depósito é creditado com network_mode correto no portfolio.
Mainnet e testnet nunca se misturam.
"""

import os
import uuid
import logging
from datetime import datetime, timezone, timedelta

import asyncio
import httpx

logger = logging.getLogger(__name__)

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
MIN_DEPOSIT_USD = 5.0

NETWORKS = {
    "mainnet": {
        "chain_id":      8453,
        "rpc":           os.getenv("BASE_RPC_URL", "https://mainnet.base.org"),
        "usdc":          "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "basescan_tx":   "https://basescan.org/tx/",
        "basescan_addr": "https://basescan.org/address/",
        "fund_wallet":   os.getenv("FUND_WALLET_ADDRESS", "").lower(),
        "label":         "base",
    },
    "testnet": {
        "chain_id":      84532,
        "rpc":           os.getenv("BASE_SEPOLIA_RPC_URL", "https://sepolia.base.org"),
        "usdc":          "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
        "basescan_tx":   "https://sepolia.basescan.org/tx/",
        "basescan_addr": "https://sepolia.basescan.org/address/",
        "fund_wallet":   os.getenv("TESTNET_FUND_WALLET_ADDRESS", os.getenv("FUND_WALLET_ADDRESS", "")).lower(),
        "label":         "base-sepolia",
    },
}

# Rastreia último bloco processado por rede — independentes
_last_block = {"mainnet": 0, "testnet": 0}

_RPC_FALLBACKS = {
    "mainnet": [
        os.getenv("BASE_RPC_URL", "https://base.drpc.org"),
        "https://base-rpc.publicnode.com",
        "https://1rpc.io/base",
        "https://mainnet.base.org",
    ],
    "testnet": [
        os.getenv("BASE_SEPOLIA_RPC_URL", "https://sepolia.base.org"),
        "https://base-sepolia-rpc.publicnode.com",
    ],
}

_rpc_status: dict = {
    "mainnet": {"healthy": True, "active_rpc": None, "last_error": None, "last_error_at": None, "fail_count": 0},
    "testnet": {"healthy": True, "active_rpc": None, "last_error": None, "last_error_at": None, "fail_count": 0},
}


def get_rpc_status() -> dict:
    """Retorna status dos RPCs para alertas do admin."""
    return {k: dict(v) for k, v in _rpc_status.items()}


def _configured(net: dict) -> bool:
    if not net["fund_wallet"]:
        return False
    return True


def _pad_address(address: str) -> str:
    return "0x" + address.lower().removeprefix("0x").zfill(64)


async def _rpc(rpc_url: str, method: str, params: list) -> dict:
    """Chamada RPC direta a uma URL. Sem retry — use _rpc_resilient para fallback chain."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(rpc_url, json=payload)
        r.raise_for_status()
        return r.json()


async def _rpc_resilient(network: str, method: str, params: list) -> dict:
    """Tenta cada RPC da lista de fallback em ordem. Atualiza _rpc_status. Lanca excecao se todos falharem."""
    urls = list(dict.fromkeys(u for u in _RPC_FALLBACKS.get(network, []) if u))
    last_exc: Exception = RuntimeError("nenhum RPC configurado")
    for url in urls:
        try:
            result = await _rpc(url, method, params)
            _rpc_status[network].update({"healthy": True, "active_rpc": url, "fail_count": 0})
            return result
        except Exception as e:
            last_exc = e
            logger.warning(
                f"deposit_monitor [{network}]: RPC {url} falhou "
                f"({type(e).__name__}: {e or '(sem msg)'}), tentando proximo..."
            )
    _rpc_status[network].update({
        "healthy": False,
        "last_error": f"{type(last_exc).__name__}: {last_exc or '(sem msg)'}",
        "last_error_at": datetime.now(timezone.utc).isoformat(),
        "fail_count": _rpc_status[network].get("fail_count", 0) + 1,
    })
    raise last_exc


async def _latest_block(network: str) -> int:
    res = await _rpc_resilient(network, "eth_blockNumber", [])
    return int(res["result"], 16)


async def _get_logs(network: str, usdc: str, fund_wallet: str, from_block: int, to_block: int) -> list:
    params = [{
        "address": usdc,
        "fromBlock": hex(from_block),
        "toBlock":   hex(to_block),
        "topics": [TRANSFER_TOPIC, None, _pad_address(fund_wallet)],
    }]
    res = await _rpc_resilient(network, "eth_getLogs", params)
    return res.get("result", [])


def _parse_amount(data_hex: str) -> float:
    raw = (data_hex or "0x0").removeprefix("0x") or "0"
    return int(raw, 16) / 1_000_000


async def _usdc_balance(network: str, usdc: str, address: str) -> float:
    data = "0x70a08231" + address.lower().removeprefix("0x").zfill(64)
    res = await _rpc_resilient(network, "eth_call", [{"to": usdc, "data": data}, "latest"])
    raw = res.get("result", "0x0")
    return int(raw or "0x0", 16) / 1_000_000


async def _eth_balance(network: str, address: str) -> float:
    res = await _rpc_resilient(network, "eth_getBalance", [address, "latest"])
    raw = res.get("result", "0x0")
    return int(raw or "0x0", 16) / 1e18


async def check_deposits(db, network: str = "mainnet"):
    """Detecta depósitos USDC na rede especificada e credita portfolios."""
    global _last_block

    net = NETWORKS.get(network, NETWORKS["mainnet"])

    if not _configured(net):
        logger.warning(f"deposit_monitor [{network}]: FUND_WALLET não configurado")
        return

    try:
        latest = await _latest_block(network)
    except Exception as e:
        logger.error(f"deposit_monitor [{network}]: erro ao buscar bloco: {type(e).__name__}: {e or '(sem mensagem)'}")
        return

    if _last_block[network] == 0:
        from models import SystemState
        try:
            _state = db.query(SystemState).filter_by(key=f"dm_last_block_{network}").first()
            if _state and int(_state.value) > 0:
                _last_block[network] = int(_state.value)
                logger.info(f"deposit_monitor [{network}]: retomando bloco {_last_block[network]} (DB)")
            else:
                _last_block[network] = latest - 30  # ~1 min na primeira execução (evita range largo)
                logger.info(f"deposit_monitor [{network}]: primeira execução, bloco {_last_block[network]}")
        except Exception as _e:
            _last_block[network] = latest - 30
            logger.warning(f"deposit_monitor [{network}]: erro ao ler last_block do DB: {_e}")

    if latest <= _last_block[network]:
        return

    # Limita o range por ciclo a 100 blocos — evita timeout em RPCs públicos.
    # Se houver acúmulo, atualiza em múltiplos ciclos de 2min.
    scan_to = min(latest, _last_block[network] + 100)

    try:
        logs = await _get_logs(network, net["usdc"], net["fund_wallet"],
                               _last_block[network] + 1, scan_to)
    except Exception as e:
        logger.error(f"deposit_monitor [{network}]: eth_getLogs falhou: {e}")
        return

    _last_block[network] = scan_to
    try:
        from models import SystemState
        _key = f"dm_last_block_{network}"
        _st = db.query(SystemState).filter_by(key=_key).first()
        if _st:
            _st.value = str(scan_to)
            _st.updated_at = datetime.utcnow()
        else:
            db.add(SystemState(key=_key, value=str(scan_to)))
        db.commit()
    except Exception as _e:
        logger.warning(f"deposit_monitor [{network}]: erro ao persistir last_block: {_e}")

    if not logs:
        return

    from models import Subscriber, SubscriberPortfolio, AgentActivityLog, AlphaIndex, InvestmentIntent

    default_index = db.query(AlphaIndex).filter(AlphaIndex.is_active == True).first()
    if not default_index:
        logger.warning(f"deposit_monitor [{network}]: nenhum index ativo encontrado")
        return

    for log in logs:
        tx_hash    = log.get("transactionHash", "")
        from_topic = (log.get("topics") or [None, None])[1]
        if not from_topic:
            continue

        from_address = ("0x" + from_topic[-40:]).lower()
        amount_usd   = _parse_amount(log.get("data", "0x0"))
        block_number = int(log.get("blockNumber", "0x0"), 16)

        if amount_usd <= 0:
            continue

        logger.info(f"deposit_monitor [{network}]: ${amount_usd:.2f} USDC de {from_address[:10]}... tx={tx_hash[:12]}...")

        # Deduplicação por tx_hash — protege contra duplo crédito em restart
        already = db.query(AgentActivityLog).filter(
            AgentActivityLog.agent == "deposit_monitor",
            AgentActivityLog.action.in_(["deposit_detected", "deposit_unattributed", "deposit_refunded"]),
            AgentActivityLog.data["tx_hash"].as_string() == tx_hash,
        ).first()
        if already:
            logger.debug(f"deposit_monitor [{network}]: tx {tx_hash[:12]}... já processada")
            continue

        # Depósito abaixo do mínimo — estornar automaticamente
        if amount_usd < MIN_DEPOSIT_USD:
            logger.warning(
                f"deposit_monitor [{network}]: depósito ${amount_usd:.2f} abaixo do mínimo "
                f"(${MIN_DEPOSIT_USD}). Iniciando estorno para {from_address[:10]}…"
            )
            from services.refund_executor import refund_deposit
            refund_result = await refund_deposit(from_address, amount_usd, net)
            refund_tx = refund_result.get("tx_hash", "") if refund_result.get("success") else ""
            refund_ok = refund_result.get("success", False)
            if refund_ok:
                logger.info(f"deposit_monitor [{network}]: estorno OK tx={refund_tx[:16]}…")
            else:
                logger.error(f"deposit_monitor [{network}]: falha no estorno — {refund_result.get('error')}")
            db.add(AgentActivityLog(
                id=str(uuid.uuid4()),
                index_id=default_index.id,
                agent="deposit_monitor",
                action="deposit_refunded",
                token_symbol="USDC",
                description=(
                    f"[{network.upper()}] Depósito de ${amount_usd:.2f} abaixo do mínimo de "
                    f"${MIN_DEPOSIT_USD:.0f}. Estorno {'enviado' if refund_ok else 'FALHOU'} para "
                    f"{from_address[:8]}…{from_address[-4:]}. "
                    f"Tx original: {tx_hash[:16]}… | Tx estorno: {refund_tx[:16]}…"
                ),
                timestamp=datetime.now(timezone.utc),
                data={
                    "tx_hash": tx_hash, "from": from_address,
                    "amount_usd": amount_usd, "reason": "below_minimum",
                    "network_mode": network, "minimum_usd": MIN_DEPOSIT_USD,
                    "refund_tx": refund_tx, "refund_ok": refund_ok,
                    "refund_basescan": refund_result.get("basescan", ""),
                },
            ))
            db.commit()
            continue

        # Só aceita depósitos de wallets com intent registrado na mesma rede (não expirado — 7 dias)
        intent_expiry = datetime.now(timezone.utc) - timedelta(days=7)
        intent = db.query(InvestmentIntent).filter(
            InvestmentIntent.wallet_address == from_address,
            InvestmentIntent.fulfilled == False,
            InvestmentIntent.network_mode == network,
            InvestmentIntent.created_at >= intent_expiry,
        ).order_by(InvestmentIntent.created_at.desc()).first()

        if not intent:
            logger.warning(
                f"deposit_monitor [{network}]: DEPÓSITO NÃO ATRIBUÍDO — "
                f"${amount_usd:.2f} USDC de {from_address[:10]}... sem intent {network}. "
                f"Retido como reserva admin. Tx: {net['basescan_tx']}{tx_hash}"
            )
            db.add(AgentActivityLog(
                id=str(uuid.uuid4()),
                index_id=default_index.id,
                agent="deposit_monitor",
                action="deposit_unattributed",
                token_symbol="USDC",
                description=(
                    f"[{network.upper()}] Depósito NÃO ATRIBUÍDO de ${amount_usd:,.2f} USDC de "
                    f"{from_address[:8]}...{from_address[-4:]} — sem intent registrado na rede {network}. "
                    f"Bloco {block_number} | Tx: {net['basescan_tx']}{tx_hash}"
                ),
                timestamp=datetime.now(timezone.utc),
                data={
                    "tx_hash": tx_hash, "basescan": f"{net['basescan_tx']}{tx_hash}",
                    "from": from_address, "amount_usd": amount_usd,
                    "block": block_number, "network": net["label"],
                    "chain_id": net["chain_id"], "network_mode": network, "status": "unattributed",
                },
            ))
            continue

        target_index = db.query(AlphaIndex).filter(AlphaIndex.id == intent.index_id).first()
        if not target_index:
            logger.error(f"deposit_monitor [{network}]: índice {intent.index_id} não encontrado")
            continue

        intent.fulfilled = True
        logger.info(f"deposit_monitor [{network}]: índice → {target_index.name}")

        # Cria subscriber se não existe
        subscriber = db.query(Subscriber).filter(Subscriber.wallet_address == from_address).first()
        if not subscriber:
            subscriber = Subscriber(
                id=str(uuid.uuid4()),
                wallet_address=from_address,
                referral_code=str(uuid.uuid4())[:8].upper(),
                created_at=datetime.now(timezone.utc),
            )
            db.add(subscriber)
            db.flush()

        nav    = max(target_index.nav_usd or 1.0, 0.0001)
        tokens = amount_usd / nav

        # Portfolio filtrado por network_mode — mainnet e testnet nunca se misturam
        portfolio = db.query(SubscriberPortfolio).filter(
            SubscriberPortfolio.subscriber_id == subscriber.id,
            SubscriberPortfolio.index_id == target_index.id,
            SubscriberPortfolio.network_mode == network,
        ).first()

        is_first_deposit = portfolio is None

        if portfolio:
            # Custo médio ponderado: (cotas_antigas × custo_antigo + novas_cotas × nav_atual) / total_cotas
            old_shares  = portfolio.index_tokens_held or 0.0
            old_avg     = portfolio.avg_cost_basis_per_share or nav
            new_total   = old_shares + tokens
            portfolio.avg_cost_basis_per_share = (
                (old_shares * old_avg + tokens * nav) / new_total if new_total > 0 else nav
            )
            portfolio.deposited_usd          += amount_usd
            portfolio.current_value_usd      += amount_usd
            portfolio.index_tokens_held      += tokens
            portfolio.total_shares_deposited  = (portfolio.total_shares_deposited or 0) + tokens
            portfolio.last_updated_at         = datetime.now(timezone.utc)
            if portfolio.current_value_usd > portfolio.high_water_mark_usd:
                portfolio.high_water_mark_usd = portfolio.current_value_usd
        else:
            portfolio = SubscriberPortfolio(
                subscriber_id=subscriber.id,
                index_id=target_index.id,
                deposited_usd=amount_usd,
                current_value_usd=amount_usd,
                index_tokens_held=tokens,
                high_water_mark_usd=amount_usd,
                network_mode=network,
                first_invested_at=datetime.now(timezone.utc),
                last_updated_at=datetime.now(timezone.utc),
                nav_at_first_deposit=nav,
                avg_cost_basis_per_share=nav,    # primeiro depósito: custo = NAV atual
                total_shares_deposited=tokens,
                total_shares_redeemed=0.0,
            )
            db.add(portfolio)
            target_index.subscriber_count = (target_index.subscriber_count or 0) + 1

        target_index.aum_usd = (target_index.aum_usd or 0) + amount_usd
        db.flush()  # garante portfolio.id disponível antes de criar DepositTransaction

        # Registro permanente e auditável desta emissão de cotas
        from models import DepositTransaction
        deposit_tx = DepositTransaction(
            subscriber_id=subscriber.id,
            portfolio_id=portfolio.id,
            index_id=target_index.id,
            tx_hash=tx_hash,
            amount_usd=amount_usd,
            nav_at_purchase=nav,
            shares_issued=round(tokens, 8),
            cost_basis_per_share=round(nav, 6),
            buy_confirmed=False,    # será atualizado após confirmação da compra
            network_mode=network,
            created_at=datetime.now(timezone.utc),
        )
        db.add(deposit_tx)

        db.add(AgentActivityLog(
            id=str(uuid.uuid4()),
            index_id=target_index.id,
            agent="deposit_monitor",
            action="deposit_detected",
            token_symbol="USDC",
            description=(
                f"[{network.upper()}] Depósito de ${amount_usd:,.2f} USDC de "
                f"{from_address[:8]}...{from_address[-4:]} "
                f"→ índice: {target_index.name} | bloco {block_number} | "
                f"Tx: {net['basescan_tx']}{tx_hash}"
            ),
            timestamp=datetime.now(timezone.utc),
            data={
                "tx_hash": tx_hash, "basescan": f"{net['basescan_tx']}{tx_hash}",
                "from": from_address, "amount_usd": amount_usd,
                "tokens": round(tokens, 6), "nav_at_deposit": nav,
                "block": block_number, "network": net["label"],
                "chain_id": net["chain_id"], "network_mode": network,
            },
        ))
        db.commit()

        # Compra tokens no SoDEX proporcional aos pesos da cesta
        try:
            from models import IndexConstituent
            from services.sodex import execute_buy_for_deposit
            is_testnet = (network == "testnet")
            basket = db.query(IndexConstituent).filter(
                IndexConstituent.index_id == target_index.id,
                IndexConstituent.in_basket == True,
                IndexConstituent.network_mode == network,
            ).all()

            # Mainnet: envia USDC do investor da fund wallet → SoDEX deposit address
            # antes de colocar ordens de compra (vUSDC precisa ser creditado primeiro)
            if not is_testnet:
                from services.sodex import deposit_usdc_to_sodex, SODEX_DEPOSIT_WAIT_SEC
                dep = await deposit_usdc_to_sodex(amount_usd)
                if not dep.get("success"):
                    raise RuntimeError(f"depósito SoDEX falhou: {dep.get('error')}")
                logger.info(
                    f"deposit_monitor [{network}]: ${amount_usd:.2f} USDC → SoDEX | "
                    f"tx={dep['tx_hash'][:20]}… | aguardando {SODEX_DEPOSIT_WAIT_SEC}s para crédito"
                )
                await asyncio.sleep(SODEX_DEPOSIT_WAIT_SEC)

            # Testnet: SoDEX é ecossistema fechado (só faucet, sem depósito externo)
            # → ordens simuladas (dry_run=True) para não bloquear o fluxo
            # Mainnet: ordens reais (dry_run=False)
            use_dry_run = is_testnet
            buy_result = await execute_buy_for_deposit(
                amount_usd=amount_usd,
                constituents=basket,
                dry_run=use_dry_run,
                testnet=is_testnet,
                index_id=target_index.id,
                network_mode=network,
            )
            # Em testnet: "placed" = dry_run orders (status="dry_run"); em mainnet: status="placed"
            placed   = [o for o in buy_result["orders"] if o.get("status") in ("placed", "dry_run")]
            skipped  = buy_result["skipped_usd"]
            mode_label = "SIMULADO" if use_dry_run else "REAL"
            logger.info(
                f"deposit_monitor [{network}]: compra {mode_label} — "
                f"{len(placed)} ordens | ${skipped:.2f} em stablecoin buffer"
            )
            # Marca compra como confirmada no registro auditável
            from models import DepositTransaction
            _dtx = db.query(DepositTransaction).filter_by(tx_hash=tx_hash).first()
            if _dtx:
                _dtx.buy_confirmed      = len(placed) > 0
                _dtx.buy_orders_placed  = len(placed)
                _dtx.buy_skipped_usd    = round(skipped, 4)
            # Atualiza stablecoin_buffer_pct se houve skipped — limitado a 100%
            if skipped > 0 and amount_usd > 0:
                extra_pct = round((skipped / amount_usd) * 100, 2)
                target_index.stablecoin_buffer_pct = min(100.0, (target_index.stablecoin_buffer_pct or 0) + extra_pct)
            buy_result["investor_wallet"] = from_address
            # Persiste cada ordem no histórico permanente de trades
            from models import TradeExecution
            _portfolio_id = None
            if _dtx and _dtx.portfolio_id:
                _portfolio_id = _dtx.portfolio_id
            _exec_at = datetime.now(timezone.utc)
            for o in buy_result.get("orders", []):
                _sym = (o.get("symbol_clean") or o.get("symbol") or "").replace("v","").replace(".","")
                _status_te = "filled" if o.get("status") in ("placed","dry_run") else "skipped"
                _skip_r = o.get("status") if _status_te == "skipped" else None
                db.add(TradeExecution(
                    source="deposit",
                    deposit_tx_id=_dtx.id if _dtx else None,
                    portfolio_id=_portfolio_id,
                    index_id=target_index.id,
                    network_mode=network,
                    symbol=_sym or o.get("symbol","?"),
                    side="buy",
                    quantity=float(o.get("quantity") or 0),
                    price_usd=float(o.get("price") or 0),
                    notional_usd=float(o.get("usd_value") or 0),
                    status=_status_te,
                    skip_reason=_skip_r,
                    order_id=o.get("clOrdID"),
                    executed_at=_exec_at,
                ))
            db.add(AgentActivityLog(
                id=str(uuid.uuid4()),
                index_id=target_index.id,
                agent="deposit_monitor",
                action="tokens_purchased",
                token_symbol="USDC",
                description=(
                    f"[{network.upper()}] Compra {mode_label} de tokens após depósito de ${amount_usd:.2f} — "
                    f"{len(placed)} ordens no SoDEX | buffer: ${skipped:.2f}"
                ),
                timestamp=datetime.now(timezone.utc),
                data=buy_result,
            ))
            db.commit()
        except Exception as e:
            logger.error(f"deposit_monitor [{network}]: erro na compra de tokens: {e}")

    db.commit()


async def check_deposits_mainnet(db):
    await check_deposits(db, "mainnet")


async def check_deposits_testnet(db):
    await check_deposits(db, "testnet")


async def get_fund_wallet_info(mode: str = "mainnet") -> dict:
    """Retorna endereço, saldo USDC e saldo ETH da fund wallet na rede especificada."""
    network = mode if mode in NETWORKS else "mainnet"
    net = NETWORKS[network]
    if not net["fund_wallet"]:
        return {"address": None, "usdc_balance": None, "eth_balance": None, "configured": False,
                "network": net["label"], "network_mode": mode}
    usdc_bal = None
    eth_bal = None
    try:
        usdc_bal = await _usdc_balance(network, net["usdc"], net["fund_wallet"])
    except Exception as e:
        logger.error(f"deposit_monitor [{mode}]: erro ao buscar saldo USDC: {e}")
    try:
        eth_bal = await _eth_balance(network, net["fund_wallet"])
    except Exception as e:
        logger.error(f"deposit_monitor [{mode}]: erro ao buscar saldo ETH: {e}")

    # Calcula gas price e transações possíveis (custo de 1 ERC-20 transfer = 100k gas)
    gas_price_gwei = None
    possible_txs = None
    gas_cost_usd = None
    GAS_LIMIT_TX = 100_000
    ETH_USD_ESTIMATE = 2500.0  # estimativa conservadora
    try:
        rpc_url = net.get("rpc", "https://mainnet.base.org")
        gp_res = await _rpc_resilient(network, "eth_gasPrice", [])
        gas_price_wei = int(gp_res["result"], 16)
        gas_price_gwei = round(gas_price_wei / 1e9, 6)
        cost_per_tx_eth = gas_price_wei * GAS_LIMIT_TX / 1e18
        gas_cost_usd = round(cost_per_tx_eth * ETH_USD_ESTIMATE, 6)
        if eth_bal is not None and cost_per_tx_eth > 0:
            possible_txs = int(eth_bal / cost_per_tx_eth)
    except Exception as e:
        logger.warning(f"deposit_monitor [{mode}]: erro ao buscar gas price: {e}")

    return {
        "address":         net["fund_wallet"],
        "usdc_balance":    usdc_bal,
        "eth_balance":     eth_bal,
        "configured":      True,
        "network":         net["label"],
        "chain_id":        net["chain_id"],
        "network_mode":    mode,
        "usdc_contract":   net["usdc"],
        "basescan_url":    f"{net['basescan_addr']}{net['fund_wallet']}",
        "gas_price_gwei":  gas_price_gwei,
        "possible_txs":    possible_txs,
        "gas_cost_usd":    gas_cost_usd,
    }
