"""
SoDEX API Service — Integração real com mainnet-gw.sodex.dev

Autenticação EIP-712:
  Headers: X-API-Key, X-API-Sign, X-API-Nonce
  chainId mainnet: 286623

Endpoints base:
  Spot:  https://mainnet-gw.sodex.dev/api/v1/spot
  Perps: https://mainnet-gw.sodex.dev/api/v1/perps
"""

import os
import json
import uuid
import asyncio
import httpx
from typing import Optional, Dict, Any, List
from loguru import logger

from utils.eip712 import get_public_headers, get_signed_headers, ACTION_BATCH_NEW_ORDER, ACTION_BATCH_CANCEL_ORDER
from utils.crypto import get_private_key

SODEX_API_KEY    = os.getenv("SODEX_API_KEY", "")
SODEX_WALLET_ADDR = os.getenv("SODEX_WALLET_ADDRESS", "")
SODEX_ACCOUNT_ID  = os.getenv("SODEX_ACCOUNT_ID", "0")
USE_TESTNET       = os.getenv("SODEX_USE_TESTNET", "false").lower() == "true"

_NET      = "testnet" if USE_TESTNET else "mainnet"
BASE_SPOT = f"https://{_NET}-gw.sodex.dev/api/v1/spot"  # leitura de preços (mesmo valor em ambas as redes)
TIMEOUT   = 15


def _spot_url(testnet: bool | None = None) -> str:
    """URL do gateway SoDEX para operações de conta/trading.
    testnet=None → usa env var SODEX_USE_TESTNET (padrão).
    testnet=True/False → força o gateway correto independente do env.
    """
    use_test = USE_TESTNET if testnet is None else testnet
    net = "testnet" if use_test else "mainnet"
    return f"https://{net}-gw.sodex.dev/api/v1/spot"

# Lê API key em runtime (não no import) para suportar scripts com load_dotenv tardio
def _api_key() -> str:
    return os.getenv("SODEX_API_KEY", "") or SODEX_API_KEY

_PUB = lambda: get_public_headers(_api_key())

def _sign(payload: dict, testnet: bool | None = None, action_type: str = ACTION_BATCH_NEW_ORDER) -> dict:
    """Assina payload com o chainId correto para a rede alvo.
    testnet=None usa SODEX_USE_TESTNET do env; True/False força a rede.
    """
    use_test = USE_TESTNET if testnet is None else testnet
    return get_signed_headers(_api_key(), payload, get_private_key(), use_test, action_type)


def _strip_decimal(s: str) -> str:
    """Remove trailing zeros de string decimal. '1800.000' -> '1800', '0.00300' -> '0.003'."""
    if "." in s:
        return s.rstrip("0").rstrip(".")
    return s


def _configured() -> bool:
    # Endpoints públicos funcionam sem key — usados apenas para account/trading
    return True


def _can_trade() -> bool:
    try:
        return bool(_api_key() and get_private_key() and (os.getenv("SODEX_WALLET_ADDRESS","") or SODEX_WALLET_ADDR))
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════════════════
#  MARKET DATA  (público)
# ═══════════════════════════════════════════════════════════════════════════════

async def get_markets(testnet: bool | None = None) -> List[Dict]:
    """Todos os símbolos spot com regras de trading. testnet=None usa env var."""
    if not _configured():
        return []
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.get(f"{_spot_url(testnet)}/markets/symbols", headers=_PUB())
            r.raise_for_status()
            data = r.json().get("data", [])
            return data if isinstance(data, list) else []
    except Exception as e:
        logger.error(f"SoDEX get_markets: {e}")
        return []


async def get_all_tickers(testnet: bool | None = None) -> Dict[str, Dict]:
    """
    Tickers de todos os mercados (24h stats).
    Retorna dict com múltiplos aliases por símbolo para facilitar lookup:
      "BTC-USDC", "BTC-USD", "vBTC_vUSDC" todos apontam para o mesmo ticker.
    Campos normalizados: lastPx -> lastPrice, volume -> volume24h
    testnet=None usa env var; True/False força o gateway correto.
    Retry automático: 3 tentativas com 5s de intervalo.
    """
    last_exc = None
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as c:
                r = await c.get(f"{_spot_url(testnet)}/markets/tickers", headers=_PUB())
                r.raise_for_status()
                items = r.json().get("data", [])
                if not isinstance(items, list):
                    return {}
            result = {}
            for item in items:
                raw_sym = item.get("symbol", "")  # ex: "vBTC_vUSDC"
                # Normaliza para campos conhecidos
                normalized = dict(item)
                normalized["lastPrice"] = item.get("lastPx") or item.get("lastPrice") or item.get("c") or "0"
                normalized["volume24h"] = item.get("quoteVolume") or item.get("volume24h") or item.get("v") or "0"
                normalized["change24h"] = item.get("changePct") or item.get("change24h") or 0
                # Remove prefixo "v" e converte underscore para dash: vBTC_vUSDC -> BTC-USDC
                clean = raw_sym.replace("_vUSDC","").replace("_USDC","").lstrip("v")
                # Trata casos especiais: vDEFIssi -> DEFIssi, WSOSO -> SOSO
                if clean.endswith("ssi"):
                    clean = clean  # mantém: DEFIssi, MAG7ssi, MEMEssi
                # Registra aliases
                result[raw_sym] = normalized           # vBTC_vUSDC
                result[clean + "-USDC"] = normalized   # BTC-USDC
                result[clean + "-USD"] = normalized    # BTC-USD
                result[clean] = normalized             # BTC
            return result
        except Exception as e:
            last_exc = e
            if attempt < 2:
                await asyncio.sleep(5)
    logger.error(f"SoDEX get_all_tickers: {last_exc}")
    return {}


async def get_ticker(symbol: str, testnet: bool | None = None) -> Optional[Dict]:
    """Ticker de um símbolo específico (ex: BTC-USDC). testnet=None usa env var."""
    if not _configured():
        return None
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.get(f"{_spot_url(testnet)}/markets/tickers",
                            headers=_PUB(), params={"symbol": symbol})
            r.raise_for_status()
            data = r.json().get("data", [])
            return data[0] if isinstance(data, list) and data else data
    except Exception as e:
        logger.error(f"SoDEX get_ticker({symbol}): {e}")
        return None


async def get_orderbook(symbol: str, depth: int = 20, testnet: bool | None = None) -> Optional[Dict]:
    """Orderbook de um mercado. testnet=None usa env var."""
    if not _configured():
        return None
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.get(f"{_spot_url(testnet)}/markets/{symbol}/orderbook",
                            headers=_PUB(), params={"limit": depth})
            r.raise_for_status()
            return r.json().get("data")
    except Exception as e:
        logger.error(f"SoDEX get_orderbook({symbol}): {e}")
        return None


async def get_candles(symbol: str, interval: str = "1D", limit: int = 30, testnet: bool | None = None) -> List[Dict]:
    """Candles OHLCV. Intervals: 1m, 5m, 15m, 1h, 4h, 1D."""
    if not _configured():
        return []
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.get(f"{_spot_url(testnet)}/markets/{symbol}/klines",
                            headers=_PUB(),
                            params={"interval": interval, "limit": limit})
            r.raise_for_status()
            data = r.json().get("data", [])
            return data if isinstance(data, list) else []
    except Exception as e:
        logger.error(f"SoDEX get_candles({symbol}): {e}")
        return []


# ═══════════════════════════════════════════════════════════════════════════════
#  ACCOUNT  (autenticado — GET com X-API-Key + assinatura)
# ═══════════════════════════════════════════════════════════════════════════════

async def get_balances(testnet: bool | None = None) -> Dict[str, Any]:
    """Saldos da fund wallet. Endpoint público — não requer API key.
    testnet=None usa env var; True/False força o gateway correto."""
    wallet = os.getenv("SODEX_WALLET_ADDRESS", "") or SODEX_WALLET_ADDR
    if not wallet:
        logger.warning("SoDEX get_balances: SODEX_WALLET_ADDRESS não configurado")
        return {}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.get(f"{_spot_url(testnet)}/accounts/{wallet}/balances")
            r.raise_for_status()
            data = r.json().get("data", {})
            # SoDEX retorna {"blockTime":..., "blockHeight":..., "balances":[...]}
            if isinstance(data, dict) and "balances" in data:
                balances_list = data["balances"]
            elif isinstance(data, list):
                balances_list = data
            else:
                balances_list = []
            return {b.get("coin", b.get("id", "")): b for b in balances_list if isinstance(b, dict)}
    except Exception as e:
        logger.error(f"SoDEX get_balances: {e}")
        return {}


async def get_open_orders(symbol: Optional[str] = None, testnet: bool | None = None) -> List[Dict]:
    """Ordens abertas da fund wallet. testnet=None usa env var."""
    if not _can_trade():
        return []
    try:
        params = {}
        if symbol:
            params["symbol"] = symbol
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.get(
                f"{_spot_url(testnet)}/accounts/{SODEX_WALLET_ADDR}/orders",
                headers=_PUB(),
                params=params,
            )
            r.raise_for_status()
            data = r.json().get("data", [])
            return data if isinstance(data, list) else []
    except Exception as e:
        logger.error(f"SoDEX get_open_orders: {e}")
        return []


async def get_trade_history(symbol: Optional[str] = None, limit: int = 50, testnet: bool | None = None) -> List[Dict]:
    """Histórico de trades executados. testnet=None usa env var."""
    if not _can_trade():
        return []
    try:
        params = {"limit": limit}
        if symbol:
            params["symbol"] = symbol
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.get(
                f"{_spot_url(testnet)}/accounts/{SODEX_WALLET_ADDR}/trades",
                headers=_PUB(),
                params=params,
            )
            r.raise_for_status()
            data = r.json().get("data", [])
            return data if isinstance(data, list) else []
    except Exception as e:
        logger.error(f"SoDEX get_trade_history: {e}")
        return []


# ═══════════════════════════════════════════════════════════════════════════════
#  TRADING  (autenticado — POST com assinatura EIP-712)
# ═══════════════════════════════════════════════════════════════════════════════

async def place_order(
    symbol: str,
    symbol_id: int,
    side: str,        # "BUY" | "SELL"
    order_type: str,  # "LIMIT" | "MARKET"
    quantity: str,
    price: str = None,
    client_order_id: str = None,
    time_in_force: str = "GTC",
    testnet: bool | None = None,
) -> Optional[Dict]:
    """
    Coloca uma ordem no SoDEX via batch endpoint.

    Args:
        symbol: ex "BTC-USDC"
        symbol_id: ID numérico do símbolo (obtido via get_markets)
        side: "BUY" ou "SELL"
        order_type: "LIMIT" ou "MARKET"
        quantity: string decimal ex "0.001"
        price: string decimal, obrigatório para LIMIT
        client_order_id: ID único do cliente (gerado automaticamente se omitido)
    """
    if not _can_trade():
        logger.error("SoDEX: não é possível colocar ordens sem credenciais")
        return None

    # SoDEX espera inteiros para side/type/timeInForce
    _side_map = {"BUY": 1, "SELL": 2}
    _type_map = {"LIMIT": 1, "MARKET": 2}
    _tif_map  = {"GTC": 1, "IOC": 2, "FOK": 3}

    clord_id = client_order_id or str(uuid.uuid4())[:36]

    # Ordem dos campos deve ser identica ao Go struct BatchNewOrderItem:
    # symbolID, clOrdID, side, type, timeInForce, price (omitempty), quantity (omitempty)
    order: Dict[str, Any] = {
        "symbolID":    symbol_id,
        "clOrdID":     clord_id,
        "side":        _side_map.get(side.upper(), 1),
        "type":        _type_map.get(order_type.upper(), 1),
        "timeInForce": _tif_map.get(time_in_force.upper(), 1),
    }
    if price and order_type.upper() == "LIMIT":
        order["price"] = _strip_decimal(price)  # price antes de quantity (Go struct order)
    order["quantity"] = _strip_decimal(quantity)

    payload = {
        "accountID": int(SODEX_ACCOUNT_ID),
        "orders": [order],
    }

    _body = json.dumps(payload, separators=(",", ":"))
    _hdrs = _sign(payload, testnet, ACTION_BATCH_NEW_ORDER)
    logger.info("[SODEX] place_order " + str(symbol) + " " + str(side) + " qty=" + str(quantity) + " price=" + str(price))
    logger.info("[SODEX] body: " + _body)
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.post(
                f"{_spot_url(testnet)}/trade/orders/batch",
                headers=_hdrs,
                content=_body,
            )
            _raw = r.text
            logger.info("[SODEX] HTTP " + str(r.status_code) + " resp: " + _raw[:800])
            import time as _tm
            _ts = _tm.strftime("%H:%M:%S")
            _sep = chr(10) + "=== " + _ts + " " + str(symbol) + " " + str(side) + " ===" + chr(10)
            with open("/tmp/sodex_orders.log", "a") as _lf:
                _lf.write(_sep)
                _lf.write("BODY: " + _body + chr(10))
                _lf.write("HTTP " + str(r.status_code) + ": " + _raw + chr(10))
            r.raise_for_status()
            result = r.json()
            if result.get("code", 0) != 0:
                err = result.get("error") or result.get("msg") or str(result)
                _code_val = str(result.get("code"))
                logger.error("[SODEX] place_order API error code=" + _code_val + " " + str(err) + " | FULL: " + str(result))
                return None
            logger.success("SoDEX order placed: " + str(side) + " " + str(quantity) + " " + str(symbol) + " @ " + str(price or 'market'))
            return result
    except Exception as e:
        logger.error("[SODEX] place_order exception " + str(symbol) + ": " + str(e))
        import time as _tm2
        with open("/tmp/sodex_orders.log", "a") as _lf:
            _lf.write(chr(10) + "EXCEPTION " + _tm2.strftime("%H:%M:%S") + " " + str(symbol) + ": " + str(e) + chr(10))
        return None


async def cancel_orders(symbol: str, symbol_id: int, order_ids: List[str], testnet: bool | None = None) -> bool:
    """Cancela ordens pelo ID do cliente. testnet=None usa env var."""
    if not _can_trade():
        return False
    try:
        payload = {
            "accountID": int(SODEX_ACCOUNT_ID),
            "orders": [{"symbolID": symbol_id, "clOrdID": oid} for oid in order_ids],
        }
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.request(
                "DELETE",
                f"{_spot_url(testnet)}/trade/orders/batch",
                headers=_sign(payload, testnet, ACTION_BATCH_CANCEL_ORDER),
                content=json.dumps(payload, separators=(",", ":")),
            )
            r.raise_for_status()
            logger.info(f"SoDEX: {len(order_ids)} ordens canceladas em {symbol}")
            return True
    except Exception as e:
        logger.error(f"SoDEX cancel_orders({symbol}): {e}")
        return False


# ═══════════════════════════════════════════════════════════════════════════════
#  HELPERS para os Agents
# ═══════════════════════════════════════════════════════════════════════════════

async def get_portfolio_snapshot(testnet: bool | None = None) -> Dict[str, Any]:
    """
    Snapshot do portfolio: saldos + valor USD de cada posição.
    Usado pelo Rebalancer para calcular pesos atuais.
    testnet=None usa env var; True/False força o gateway correto.
    """
    balances = await get_balances(testnet=testnet)
    tickers  = await get_all_tickers(testnet=testnet)

    positions = []
    total_usd = 0.0

    STABLE = {"USDC", "USDT", "USD", "vUSDC", "vUSDT"}
    for coin, bal in balances.items():
        available = float(bal.get("total", 0)) - float(bal.get("locked", 0))
        if coin in STABLE:
            usd_value = available
        else:
            # coin pode ser "vETH", "vDEFI.ssi" etc.
            # Normaliza: remove prefixo "v" e remove ponto (vDEFI.ssi → DEFIssi)
            base      = coin.lstrip("v")
            base_norm = base.replace(".", "")
            price = 0.0
            for sym in (f"{coin}-USDC", f"{coin}-vUSDC", f"{base}-USDC",
                        f"{base_norm}-USDC", f"{base_norm}-USD", base_norm):
                ticker = tickers.get(sym, {})
                price  = float(ticker.get("lastPrice") or ticker.get("lastPx") or ticker.get("c") or 0)
                if price:
                    break
            usd_value = available * price

        positions.append({
            "asset":     coin,
            "amount":    available,
            "usd_value": usd_value,
        })
        total_usd += usd_value

    for p in positions:
        p["weight_pct"] = round((p["usd_value"] / total_usd * 100) if total_usd > 0 else 0, 2)

    return {
        "positions":          positions,
        "total_usd":          total_usd,
        "network":            "testnet" if (USE_TESTNET if testnet is None else testnet) else "mainnet",
        "wallet":             SODEX_WALLET_ADDR,
        "configured":         len(balances) > 0,   # True quando recebemos dados reais do SoDEX
    }


async def get_symbol_id(symbol: str, testnet: bool | None = None) -> Optional[int]:
    """Busca o symbolID numérico necessário para ordens.
    Tenta múltiplos formatos: 'ETH-USDC' → 'vETH_vUSDC', 'WSOSO_vUSDC', etc.
    """
    markets = await get_markets(testnet=testnet)
    base = symbol.replace("-USDC", "").replace("-USD", "")
    for m in markets:
        name    = m.get("name", "")
        display = m.get("displayName", "")
        if (name == symbol
                or name == f"v{base}_vUSDC"
                or name == f"{base}_vUSDC"
                or display == symbol.replace("-", "/")
                or display == f"{base}/USDC"):
            return m.get("id")
    return None


async def execute_rebalance_trades(
    target_weights: Dict[str, float],
    dry_run: bool = True,
    testnet: bool | None = None,
) -> List[Dict]:
    """
    Executa rebalanceamento para atingir pesos alvo via SoDEX.
    dry_run=True apenas loga sem executar.
    testnet=None usa env var; True/False força o gateway correto.
    """
    import math as _math
    snapshot  = await get_portfolio_snapshot(testnet=testnet)
    total_usd = snapshot["total_usd"]
    current: dict = {}
    for _p in snapshot["positions"]:
        _raw = _p.get("asset", "")
        current[_raw] = _p
        _stripped = _raw.lstrip("v").lstrip("V")
        if _stripped and _stripped != _raw: current[_stripped] = _p
        _nodot = _stripped.replace(".", "")
        if _nodot and _nodot != _stripped: current[_nodot] = _p
    tickers   = await get_all_tickers()
    markets   = await get_markets(testnet=testnet)
    mkt_info_rb: Dict[str, dict] = {}
    for _m in markets:
        _base = _m.get("baseCoin", "")
        _clean = _base.lstrip("v").lstrip("V").replace(".", "").upper()
        mkt_info_rb[_clean] = _m

    orders = []

    # SELLs antes de BUYs para garantir liquidez USDC
    sorted_targets = sorted(
        target_weights.items(),
        key=lambda x: 0 if (total_usd * (x[1] / 100) - current.get(x[0], {}).get("usd_value", 0)) < 0 else 1
    )

    for asset, target_pct in sorted_targets:
        if asset in ("USDC", "USDT", "USD"):
            continue

        target_usd  = total_usd * (target_pct / 100)
        current_usd = current.get(asset, {}).get("usd_value", 0)
        diff_usd    = target_usd - current_usd

        if abs(diff_usd) < 0.50:
            continue

        sym    = f"{asset}-USDC"
        ticker = tickers.get(sym, {})
        price  = float(ticker.get("lastPrice") or ticker.get("lastPx") or ticker.get("c") or 0)

        if price <= 0:
            logger.warning(f"SoDEX rebalance: sem preço para {sym}")
            continue

        _mkt_rb     = mkt_info_rb.get(asset.upper()) or {}
        _qty_prec   = int(_mkt_rb.get("quantityPrecision") or 8)
        _px_prec    = int(_mkt_rb.get("pricePrecision") or 2)
        _step_rb    = float(_mkt_rb.get("stepSize") or 0)
        _min_not_rb = float(_mkt_rb.get("minNotional") or 0)

        raw_size = abs(diff_usd) / price
        if _step_rb > 0:
            size = _math.floor(raw_size / _step_rb) * _step_rb
            if _min_not_rb > 0 and size * price < _min_not_rb:
                size += _step_rb
        else:
            size = _math.floor(raw_size * 10**_qty_prec) / 10**_qty_prec
            if _min_not_rb > 0 and size * price < _min_not_rb:
                size += 10**(-_qty_prec)

        side      = "BUY" if diff_usd > 0 else "SELL"
        qty_str   = f"{size:.{_qty_prec}f}"
        price_str = f"{price:.{_px_prec}f}"

        order_info = {
            "symbol":    sym,
            "side":      side,
            "quantity":  qty_str,
            "price":     price_str,
            "usd_value": abs(diff_usd),
            "reason":    f"{asset} {current.get(asset, {}).get('weight_pct', 0):.1f}% → {target_pct:.1f}%",
        }

        if dry_run:
            logger.info(f"[DRY RUN] {side} {qty_str} {sym} @ ${price_str}")
            order_info["status"] = "dry_run"
        else:
            sym_id = await get_symbol_id(sym)
            if sym_id:
                result = await place_order(sym, sym_id, side, "LIMIT", qty_str, price_str, testnet=testnet)
                order_info["status"] = "placed" if result else "failed"
            else:
                logger.warning(f"SoDEX: symbolID não encontrado para {sym}")
                order_info["status"] = "no_symbol"

        orders.append(order_info)

    return orders


SODEX_DEPOSIT_ADDRESS = "0x47D3CC0Ceacc2f5c69CF91A0592C4A90d9B541cA"
USDC_BASE_CONTRACT    = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
BASE_RPC_URL          = os.getenv("BASE_RPC_URL", "https://mainnet.base.org")
CHAIN_ID_BASE         = 8453
GAS_LIMIT_TRANSFER    = 100_000

# Crédito demora ~2min na Base após confirmação on-chain
SODEX_DEPOSIT_WAIT_SEC = 150


async def deposit_usdc_to_sodex(amount_usd: float, simulate: bool = False) -> dict:
    """
    Transfere USDC da fund wallet (on-chain Base) para o endereço de depósito do SoDEX.
    SoDEX credita como vUSDC em ~2 minutos após confirmação.

    simulate=True: assina a tx mas NÃO transmite (para testes).
    """
    from eth_account import Account
    from utils.crypto import get_fund_private_key

    fund_wallet = os.getenv("FUND_WALLET_ADDRESS", "").lower()
    amount_units = int(round(amount_usd * 1_000_000))  # USDC 6 decimais

    async def _rpc(method, params):
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(BASE_RPC_URL, json=payload)
            return r.json()

    def _encode_transfer(to: str, units: int) -> str:
        selector  = "a9059cbb"
        to_padded = to.lower().removeprefix("0x").zfill(64)
        amt_pad   = hex(units)[2:].zfill(64)
        return "0x" + selector + to_padded + amt_pad

    # Verifica saldo USDC on-chain
    data_call = "0x70a08231" + fund_wallet.removeprefix("0x").zfill(64)
    res = await _rpc("eth_call", [{"to": USDC_BASE_CONTRACT, "data": data_call}, "latest"])
    usdc_bal = int(res.get("result", "0x0"), 16) / 1_000_000

    if usdc_bal < amount_usd and not simulate:
        return {"success": False, "error": f"Saldo USDC insuficiente: ${usdc_bal:.2f} disponível, ${amount_usd:.2f} necessário"}

    nonce_res = await _rpc("eth_getTransactionCount", [fund_wallet, "pending"])
    nonce = int(nonce_res["result"], 16)
    gas_res = await _rpc("eth_gasPrice", [])
    gas_price = int(gas_res["result"], 16)

    tx = {
        "to":       USDC_BASE_CONTRACT,
        "data":     _encode_transfer(SODEX_DEPOSIT_ADDRESS, amount_units),
        "nonce":    nonce,
        "gasPrice": gas_price,
        "gas":      GAS_LIMIT_TRANSFER,
        "chainId":  CHAIN_ID_BASE,
        "value":    0,
    }

    private_key = get_fund_private_key()
    signed = Account.sign_transaction(tx, private_key)
    raw_tx = "0x" + signed.raw_transaction.hex()

    if simulate:
        return {"success": True, "simulate": True, "tx_hash": "(simulação)", "amount_usd": amount_usd}

    result = await _rpc("eth_sendRawTransaction", [raw_tx])
    if "error" in result:
        return {"success": False, "error": str(result["error"])}

    tx_hash = result.get("result", "")
    logger.info(f"deposit_usdc_to_sodex: ${amount_usd:.2f} USDC → SoDEX | tx={tx_hash[:16]}…")
    return {"success": True, "tx_hash": tx_hash, "amount_usd": amount_usd,
            "basescan": f"https://basescan.org/tx/{tx_hash}",
            "wait_sec": SODEX_DEPOSIT_WAIT_SEC}


async def _verify_order_executed(clord_id: str, testnet: bool | None = None) -> bool:
    """Verifica via histórico de trades se uma ordem com clOrdID foi executada."""
    wallet = os.getenv("SODEX_WALLET_ADDRESS", "") or SODEX_WALLET_ADDR
    if not wallet:
        return False
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.get(f"{_spot_url(testnet)}/accounts/{wallet}/trades?limit=50")
            r.raise_for_status()
            trades = r.json().get("data", [])
            for trade in trades:
                if trade.get("clOrdID") == clord_id:
                    return True
    except Exception as e:
        logger.warning(f"_verify_order_executed: {e}")
    return False


async def execute_buy_for_deposit(
    amount_usd: float,
    constituents: list,          # lista de IndexConstituent (in_basket=True)
    dry_run: bool = True,
    testnet: bool | None = None,
    index_id: str = "",
    network_mode: str = "mainnet",
) -> Dict[str, Any]:
    """
    Compra tokens no SoDEX proporcional aos pesos da cesta após um depósito.

    - Cada constituinte recebe amount_usd * weight/100 de USDC.
    - Tokens abaixo de $5 ou sem preço são pulados (viram stablecoin buffer).
    - testnet=True força gateway testnet; testnet=False força mainnet;
      testnet=None usa SODEX_USE_TESTNET do env.
    - dry_run=True loga sem colocar ordens reais.

    Retorna dict com ordens e total alocado.
    """
    MIN_ORDER_USD = 5.0

    tickers = await get_all_tickers(testnet=testnet)
    markets = await get_markets(testnet=testnet)

    # Índice de precisão por símbolo limpo (ex: AAVE, UNI, DEFIssi)
    mkt_info: Dict[str, dict] = {}
    for m in markets:
        base = m.get("baseCoin", "")
        clean = base.lstrip("v").lstrip("V").replace(".", "").upper()
        mkt_info[clean] = m

    orders        = []
    skipped_usd   = 0.0
    allocated_usd = 0.0

    for c in constituents:
        symbol     = c.symbol
        weight_pct = c.weight or 0.0
        alloc_usd  = amount_usd * (weight_pct / 100.0)

        if alloc_usd < MIN_ORDER_USD:
            logger.info(f"execute_buy_for_deposit: {symbol} pulado — ${alloc_usd:.2f} < mínimo ${MIN_ORDER_USD}")
            skipped_usd += alloc_usd
            orders.append({"symbol": symbol, "status": "skipped_below_minimum", "usd_value": alloc_usd})
            continue

        # SoDEX retorna tickers sem prefixo "v" e sem ponto (vETH→ETH, vMAG7.ssi→MAG7ssi)
        clean_sym = symbol.lstrip("v").replace(".", "")
        sym_key = f"{clean_sym}-USDC"
        ticker  = (tickers.get(sym_key)
                   or tickers.get(f"{symbol}-USDC")
                   or tickers.get(f"v{symbol}_vUSDC")
                   or {})
        price   = float(ticker.get("lastPrice") or ticker.get("lastPx") or ticker.get("c") or 0)

        if price <= 0:
            logger.warning(f"execute_buy_for_deposit: {symbol} sem preço no SoDEX — pulando")
            skipped_usd += alloc_usd
            orders.append({"symbol": symbol, "status": "skipped_no_price", "usd_value": alloc_usd})
            continue

        # Aplica precisão do mercado (quantityPrecision e pricePrecision do SoDEX)
        mkt = mkt_info.get(clean_sym.upper()) or mkt_info.get(symbol.lstrip("v").replace(".", "").upper()) or {}
        qty_prec  = int(mkt.get("quantityPrecision") or 8)
        px_prec   = int(mkt.get("pricePrecision") or 6)
        step_size = float(mkt.get("stepSize") or 0)
        min_qty   = float(mkt.get("minQuantity") or 0)

        import math
        raw_qty = alloc_usd / price
        min_notional = float(mkt.get("minNotional") or 0)

        if step_size > 0:
            qty = math.floor(raw_qty / step_size) * step_size
            # Se o notional ficar abaixo do mínimo por causa do floor, sobe um step
            if min_notional > 0 and qty * price < min_notional:
                qty += step_size
        else:
            qty = math.floor(raw_qty * 10**qty_prec) / 10**qty_prec
            if min_notional > 0 and qty * price < min_notional:
                qty += 10**(-qty_prec)

        qty_str = f"{qty:.{qty_prec}f}"
        px_str  = f"{price:.{px_prec}f}"

        if min_qty > 0 and qty < min_qty:
            logger.warning(f"execute_buy_for_deposit: {symbol} qty {qty_str} < minQuantity {min_qty} — pulando")
            skipped_usd += alloc_usd
            orders.append({"symbol": symbol, "status": "skipped_below_min_qty", "usd_value": alloc_usd})
            continue

        order_info = {
            "symbol":       symbol,
            "symbol_clean": clean_sym,   # símbolo sem prefixo v e sem ponto: DEFIssi, AAVE, LINK
            "side":         "BUY",
            "quantity":     qty_str,
            "price":        px_str,
            "usd_value":    round(alloc_usd, 4),
            "status":       None,
        }

        if dry_run:
            logger.info(f"[DRY RUN] BUY {qty_str} {sym_key} @ ${px_str} (${alloc_usd:.2f})")
            order_info["status"] = "dry_run"
        else:
            sym_id = await get_symbol_id(sym_key, testnet=testnet)
            if not sym_id:
                sym_id = await get_symbol_id(f"{symbol}-USDC", testnet=testnet)
            if sym_id:
                clord_id = str(uuid.uuid4())
                result = await place_order(sym_key, sym_id, "BUY", "LIMIT", qty_str, px_str,
                                           client_order_id=clord_id, testnet=testnet)
                if result:
                    order_info["status"] = "placed"
                    order_info["raw"]    = result
                else:
                    # Resposta nula não significa que a ordem falhou — pode ter sido timeout de resposta
                    # após o servidor processar. Verificar via histórico de trades.
                    await asyncio.sleep(5)
                    executed = await _verify_order_executed(clord_id, testnet=testnet)
                    if executed:
                        logger.info(f"execute_buy_for_deposit: {symbol} confirmado via histórico — resposta perdida mas ordem executou")
                        order_info["status"] = "placed"
                        order_info["raw"]    = {"confirmed_via_trade_history": True, "clOrdID": clord_id}
                    else:
                        logger.error(f"execute_buy_for_deposit: {symbol} falhou — não encontrado no histórico de trades")
                        order_info["status"] = "failed"
                        order_info["raw"]    = None
                order_info["clOrdID"] = clord_id
            else:
                logger.warning(f"execute_buy_for_deposit: symbolID não encontrado para {sym_key}")
                order_info["status"] = "no_symbol"
                skipped_usd += alloc_usd

        allocated_usd += alloc_usd
        orders.append(order_info)

    # Atualiza index_holdings com as quantidades compradas (apenas ordens reais colocadas)
    if not dry_run and index_id:
        try:
            from database import SessionLocal
            from models import IndexHolding
            from datetime import datetime, timezone
            db_h = SessionLocal()
            try:
                for order_result in orders:
                    if order_result.get("status") != "placed":
                        continue
                    sym_h = order_result.get("symbol_clean")
                    qty   = float(order_result.get("quantity", 0) or 0)
                    if not sym_h or qty <= 0:
                        continue
                    existing = db_h.query(IndexHolding).filter(
                        IndexHolding.index_id     == index_id,
                        IndexHolding.network_mode == network_mode,
                        IndexHolding.symbol       == sym_h,
                    ).first()
                    if existing:
                        existing.quantity   += qty
                        existing.updated_at  = datetime.now(timezone.utc)
                    else:
                        db_h.add(IndexHolding(
                            index_id=index_id,
                            network_mode=network_mode,
                            symbol=sym_h,
                            quantity=qty,
                            updated_at=datetime.now(timezone.utc),
                        ))
                db_h.commit()
                placed_count = sum(1 for o in orders if o.get("status") == "placed")
                logger.info(f"index_holdings atualizado: {placed_count} token(s) para {index_id}/{network_mode}")
            finally:
                db_h.close()
        except Exception as e:
            logger.warning(f"index_holdings update failed: {e}")

    return {
        "orders":        orders,
        "allocated_usd": round(allocated_usd, 4),
        "skipped_usd":   round(skipped_usd, 4),
        "dry_run":       dry_run,
        "testnet":       USE_TESTNET if testnet is None else testnet,
    }


async def execute_sell_for_withdrawal(
    amount_usd: float,
    constituents: list,          # lista de IndexConstituent (in_basket=True)
    dry_run: bool = True,
    testnet: bool | None = None,
) -> Dict[str, Any]:
    """
    Vende tokens no SoDEX proporcionalmente ao valor sacado.

    Usa os pesos da cesta para calcular quanto vender de cada token:
      sell_usd[token] = amount_usd * (weight / 100)
      sell_qty[token] = sell_usd[token] / current_price

    Coloca ordens LIMIT ao preço de mercado atual para execução imediata.
    dry_run=True loga sem colocar ordens reais.
    """
    import math as _math
    MIN_SELL_USD = 1.0

    tickers  = await get_all_tickers(testnet=testnet)
    markets  = await get_markets(testnet=testnet)
    mkt_info_sell: Dict[str, dict] = {}
    for _m in markets:
        _base  = _m.get("baseCoin", "")
        _clean = _base.lstrip("v").lstrip("V").replace(".", "").upper()
        mkt_info_sell[_clean] = _m

    orders        = []
    recovered_usd = 0.0
    skipped_usd   = 0.0

    for c in constituents:
        symbol     = c.symbol
        weight_pct = c.weight or 0.0
        sell_usd   = amount_usd * (weight_pct / 100.0)

        if sell_usd < MIN_SELL_USD:
            logger.info(f"execute_sell_for_withdrawal: {symbol} pulado — ${sell_usd:.2f} < mínimo ${MIN_SELL_USD}")
            skipped_usd += sell_usd
            orders.append({"symbol": symbol, "status": "skipped_below_minimum", "usd_value": sell_usd})
            continue

        clean_sym = symbol.lstrip("v").replace(".", "")
        sym_key   = f"{clean_sym}-USDC"
        ticker    = (tickers.get(sym_key)
                     or tickers.get(f"{symbol}-USDC")
                     or tickers.get(f"v{symbol}_vUSDC")
                     or {})
        price = float(ticker.get("lastPrice") or ticker.get("lastPx") or ticker.get("c") or 0)

        if price <= 0:
            logger.warning(f"execute_sell_for_withdrawal: {symbol} sem preço no SoDEX — pulando")
            skipped_usd += sell_usd
            orders.append({"symbol": symbol, "status": "skipped_no_price", "usd_value": sell_usd})
            continue

        _mkt_sell   = mkt_info_sell.get(clean_sym.upper()) or {}
        _qty_prec_s = int(_mkt_sell.get("quantityPrecision") or 8)
        _px_prec_s  = int(_mkt_sell.get("pricePrecision") or 6)
        _step_s     = float(_mkt_sell.get("stepSize") or 0)

        raw_qty = sell_usd / price
        if _step_s > 0:
            qty = _math.floor(raw_qty / _step_s) * _step_s
        else:
            qty = _math.floor(raw_qty * 10**_qty_prec_s) / 10**_qty_prec_s

        qty_str = f"{qty:.{_qty_prec_s}f}"
        px_str  = f"{price:.{_px_prec_s}f}"

        order_info = {
            "symbol":    symbol,
            "side":      "SELL",
            "quantity":  qty_str,
            "price":     px_str,
            "usd_value": round(sell_usd, 4),
            "status":    None,
        }

        if dry_run:
            logger.info(f"[DRY RUN] SELL {qty_str} {sym_key} @ ${px_str} (${sell_usd:.2f})")
            order_info["status"] = "dry_run"
        else:
            sym_id = await get_symbol_id(sym_key, testnet=testnet)
            if not sym_id:
                sym_id = await get_symbol_id(f"{symbol}-USDC", testnet=testnet)

            if sym_id:
                result = await place_order(sym_key, sym_id, "SELL", "LIMIT", qty_str, px_str, testnet=testnet)
                order_info["status"] = "placed" if result else "failed"
                order_info["raw"]    = result
            else:
                logger.warning(f"execute_sell_for_withdrawal: symbolID não encontrado para {sym_key}")
                order_info["status"] = "no_symbol"
                skipped_usd += sell_usd

        recovered_usd += sell_usd
        orders.append(order_info)

    return {
        "orders":        orders,
        "recovered_usd": round(recovered_usd, 4),
        "skipped_usd":   round(skipped_usd, 4),
        "dry_run":       dry_run,
        "testnet":       USE_TESTNET if testnet is None else testnet,
    }
