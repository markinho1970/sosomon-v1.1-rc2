from datetime import datetime, timezone
from typing import Optional, List
from sqlalchemy import (
    Column, String, Float, Integer, Boolean, DateTime, Text, ForeignKey, JSON, UniqueConstraint
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class AlphaIndex(Base):
    __tablename__ = "indexes"

    id = Column(String, primary_key=True)          # e.g. "ai-crypto-infrastructure"
    slug = Column(String, unique=True, nullable=False)
    name = Column(String, nullable=False)
    theme = Column(String, nullable=False)          # ai-crypto | rwa | depin | defi
    description = Column(Text)
    inception_date = Column(DateTime, default=datetime.utcnow)
    aum_usd = Column(Float, default=0.0)
    nav_usd = Column(Float, default=1.0)
    total_return_pct = Column(Float, default=0.0)
    return_30d_pct = Column(Float, default=0.0)
    return_7d_pct = Column(Float, default=0.0)
    btc_benchmark_30d = Column(Float, default=0.0)
    stablecoin_buffer_pct = Column(Float, default=0.0)
    subscriber_count = Column(Integer, default=0)
    management_fee_pct = Column(Float, default=0.75)
    last_rebalanced_at = Column(DateTime)
    rebalance_summary = Column(Text, default="")
    is_active = Column(Boolean, default=True)
    min_deposit_usd = Column(Float, default=50.0)
    target_constituents = Column(Integer, default=5)   # número alvo de tokens na cesta

    constituents = relationship("IndexConstituent", back_populates="index", cascade="all, delete-orphan")
    activities = relationship("AgentActivityLog", back_populates="index")


class IndexConstituent(Base):
    __tablename__ = "constituents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    index_id = Column(String, ForeignKey("indexes.id"), nullable=False)
    symbol = Column(String, nullable=False)
    name = Column(String, nullable=False)
    coingecko_id = Column(String)
    weight = Column(Float, nullable=False)          # percentage 0-100
    current_price_usd = Column(Float, default=0.0)
    price_at_nav_ref  = Column(Float, default=0.0)   # atualizado APENAS pelo nav_updater — nunca por scripts externos
    market_cap_usd = Column(Float, default=0.0)
    volume_24h_usd = Column(Float, default=0.0)
    price_change_7d = Column(Float, default=0.0)
    price_change_30d = Column(Float, default=0.0)
    ai_rationale = Column(Text, default="")
    added_at = Column(DateTime, default=datetime.utcnow)
    is_stablecoin = Column(Boolean, default=False)
    network_mode = Column(String, default="mainnet")   # "mainnet" | "testnet" — coluna adicionada por migrate_constituent_network_mode.py
    in_basket = Column(Boolean, default=True)          # True = token da cesta ativa; False = candidato do universo temático
    is_anchor = Column(Boolean, default=False)         # True = token âncora — Scout NUNCA pode remover (ex: vMAG7.ssi, WSOSO)

    index = relationship("AlphaIndex", back_populates="constituents")


class AgentActivityLog(Base):
    __tablename__ = "agent_activity"

    id = Column(String, primary_key=True)
    index_id = Column(String, ForeignKey("indexes.id"), nullable=True)
    agent = Column(String, nullable=False)          # scout | rebalancer | narrator
    action = Column(String, nullable=False)         # inclusion | exclusion | rebalance | ...
    token_symbol = Column(String, nullable=True)
    description = Column(Text, nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow)
    data = Column(JSON, nullable=True)

    index = relationship("AlphaIndex", back_populates="activities")


class Subscriber(Base):
    __tablename__ = "subscribers"

    id = Column(String, primary_key=True)
    wallet_address = Column(String, unique=True, nullable=True)
    email = Column(String, unique=True, nullable=True)
    is_pro = Column(Boolean, default=False)
    pro_since = Column(DateTime, nullable=True)
    stripe_customer_id = Column(String, nullable=True)
    referral_code = Column(String, unique=True)
    referred_by = Column(String, nullable=True)
    days_streak = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

    portfolios = relationship("SubscriberPortfolio", back_populates="subscriber")


class SubscriberPortfolio(Base):
    __tablename__ = "portfolios"

    id = Column(Integer, primary_key=True, autoincrement=True)
    subscriber_id = Column(String, ForeignKey("subscribers.id"), nullable=False)
    index_id = Column(String, ForeignKey("indexes.id"), nullable=False)
    deposited_usd = Column(Float, default=0.0)           # total depositado (custo base)
    current_value_usd = Column(Float, default=0.0)       # valor atual = cotas × NAV
    index_tokens_held = Column(Float, default=0.0)       # cotas (shares) atuais
    high_water_mark_usd = Column(Float, default=0.0)
    days_invested = Column(Integer, default=0)
    first_invested_at = Column(DateTime, default=datetime.utcnow)
    last_updated_at = Column(DateTime, default=datetime.utcnow)
    network_mode = Column(String, default="mainnet")     # "mainnet" | "testnet"
    # Campos de auditoria de cotas
    nav_at_first_deposit = Column(Float, default=1.0)    # NAV quando o 1º depósito ocorreu
    avg_cost_basis_per_share = Column(Float, default=1.0) # custo médio ponderado por cota
    total_shares_deposited = Column(Float, default=0.0)  # cotas acumuladas emitidas (histórico)
    total_shares_redeemed = Column(Float, default=0.0)   # cotas acumuladas queimadas (histórico)

    subscriber = relationship("Subscriber", back_populates="portfolios")


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    portfolio_id = Column(Integer, ForeignKey("portfolios.id"), nullable=False)
    index_id = Column(String, nullable=False)
    network_mode = Column(String, default="mainnet")
    snapshot_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    value_usd = Column(Float, nullable=False)
    deposited_usd = Column(Float, nullable=True)
    nav_per_token = Column(Float, nullable=True)


class ScoutReport(Base):
    __tablename__ = "scout_reports"

    id = Column(Integer, primary_key=True, autoincrement=True)
    index_id = Column(String, ForeignKey("indexes.id"), nullable=False)
    run_at = Column(DateTime, default=datetime.utcnow)
    tokens_screened = Column(Integer, default=0)
    tokens_qualified = Column(Integer, default=0)
    inclusions = Column(JSON, default=list)         # list of token dicts recommended for inclusion
    exclusions = Column(JSON, default=list)         # list of token dicts recommended for exclusion
    weight_changes = Column(JSON, default=list)
    raw_output = Column(Text)                        # full AI output for audit log


class InvestmentIntent(Base):
    """Registra a intenção de depósito: qual carteira vai depositar em qual índice.
    deposit_monitor consulta esta tabela para creditar o índice correto."""
    __tablename__ = "investment_intents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    wallet_address = Column(String, nullable=False, index=True)
    index_id = Column(String, ForeignKey("indexes.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)
    fulfilled = Column(Boolean, default=False)
    network_mode = Column(String, default="mainnet")  # "mainnet" | "testnet"


class InvestmentConsent(Base):
    """Registro auditável da concordância do usuário com o termo de risco."""
    __tablename__ = "investment_consents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    wallet_address = Column(String, nullable=False, index=True)
    index_id = Column(String, ForeignKey("indexes.id"), nullable=False)
    terms_version = Column(String, nullable=False, default="v1.0")
    signature = Column(Text, nullable=False)           # assinatura EIP-191 da carteira
    signed_message = Column(Text, nullable=False)      # mensagem exata que foi assinada
    signed_at = Column(DateTime, default=datetime.utcnow)
    ip_hint = Column(String, nullable=True)            # opcional, para auditoria


class RebalanceProposal(Base):
    __tablename__ = "rebalance_proposals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    index_id = Column(String, ForeignKey("indexes.id"), nullable=False)
    proposed_at = Column(DateTime, default=datetime.utcnow)
    approved_at = Column(DateTime, nullable=True)
    executed_at = Column(DateTime, nullable=True)
    status = Column(String, default="pending")      # pending | approved | rejected | executed | failed | dry_run
    trigger = Column(String)                        # "weekly" | "drift" | "risk_override" | "manual"
    changes = Column(JSON, default=list)            # list of {symbol, old_weight, new_weight, action}
    ai_rationale = Column(Text)
    execution_orders = Column(JSON, default=list)   # list of executed orders with IDs from SoDEX
    execution_error = Column(Text, nullable=True)   # error message if execution failed
    network_mode = Column(String, default="mainnet")  # "mainnet" | "testnet" — coluna adicionada por migrate_proposal_network_mode.py

class DepositTransaction(Base):
    """Registro permanente e auditável de cada depósito individual.
    Cada evento de depósito gera exatamente 1 linha nesta tabela.
    Permite rastrear: quanto cada cota custou, se a compra foi confirmada na SoDEX,
    e reconstruir o histórico completo de emissão de cotas por investidor."""
    __tablename__ = "deposit_transactions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    subscriber_id = Column(String, ForeignKey("subscribers.id"), nullable=False, index=True)
    portfolio_id = Column(Integer, ForeignKey("portfolios.id"), nullable=True)
    index_id = Column(String, ForeignKey("indexes.id"), nullable=False)
    tx_hash = Column(String, unique=True, nullable=False, index=True)  # hash on-chain
    amount_usd = Column(Float, nullable=False)             # valor bruto depositado
    nav_at_purchase = Column(Float, nullable=False)        # NAV no momento do processamento
    shares_issued = Column(Float, nullable=False)          # cotas emitidas = amount / nav
    cost_basis_per_share = Column(Float, nullable=False)   # = nav_at_purchase (preço de entrada)
    buy_confirmed = Column(Boolean, default=False)         # tokens comprados na SoDEX?
    buy_orders_placed = Column(Integer, default=0)         # nº de ordens colocadas
    buy_skipped_usd = Column(Float, default=0.0)           # USD não alocado (buffer)
    network_mode = Column(String, default="mainnet")
    created_at = Column(DateTime, default=datetime.utcnow)


class RedemptionTransaction(Base):
    """Registro permanente e auditável de cada saque/resgate.
    Cada execução bem-sucedida de saque gera exatamente 1 linha nesta tabela."""
    __tablename__ = "redemption_transactions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    subscriber_id = Column(String, ForeignKey("subscribers.id"), nullable=False, index=True)
    portfolio_id = Column(Integer, ForeignKey("portfolios.id"), nullable=True)
    index_id = Column(String, ForeignKey("indexes.id"), nullable=False)
    tx_hash = Column(String, nullable=True, index=True)    # hash on-chain (Base)
    amount_usd = Column(Float, nullable=False)             # valor bruto resgatado
    shares_burned = Column(Float, nullable=False)          # cotas queimadas
    nav_at_redemption = Column(Float, nullable=False)      # NAV no momento do resgate
    net_usd = Column(Float, nullable=False)                # valor líquido recebido
    management_fee_usd = Column(Float, default=0.0)
    performance_fee_usd = Column(Float, default=0.0)
    gas_fee_usd = Column(Float, default=0.0)
    pnl_usd = Column(Float, default=0.0)                   # lucro/prejuízo realizado
    pnl_pct = Column(Float, default=0.0)
    cost_basis_proportional = Column(Float, default=0.0)   # custo base proporcional
    is_simulated = Column(Boolean, default=False)
    network_mode = Column(String, default="mainnet")
    created_at = Column(DateTime, default=datetime.utcnow)


class FaucetClaim(Base):
    __tablename__ = "faucet_claims"

    id = Column(Integer, primary_key=True, autoincrement=True)
    wallet_address = Column(String, nullable=False, index=True)
    tx_hash = Column(String, nullable=False, unique=True)
    amount_eth = Column(Float, default=0.0001)
    claimed_at = Column(DateTime, default=datetime.utcnow)
    basescan = Column(String, nullable=True)


class SystemState(Base):
    """Estado persistido do sistema — chave/valor sobrevive a restarts."""
    __tablename__ = "system_state"
    key        = Column(String, primary_key=True)
    value      = Column(String, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow)


class IndexHolding(Base):
    """Quantidade real de cada token detido pelo fundo no SoDEX.
    Atualizada após cada compra executada com sucesso.
    Permite calcular NAV via quantidade×preço quando get_balances() está indisponível."""
    __tablename__ = "index_holdings"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    index_id     = Column(String, nullable=False)
    network_mode = Column(String, nullable=False, default="mainnet")
    symbol       = Column(String, nullable=False)   # símbolo limpo: DEFIssi, AAVE, LINK, UNI
    quantity     = Column(Float, nullable=False, default=0.0)
    updated_at   = Column(DateTime, nullable=False)

    __table_args__ = (UniqueConstraint("index_id", "network_mode", "symbol"),)


class TradeExecution(Base):
    """Histórico permanente de cada ordem individual executada (compra/venda) no SoDEX.
    Independente da retenção de histórico da SoDEX API — persiste para sempre no banco."""
    __tablename__ = "trade_executions"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    source         = Column(String, nullable=False)          # 'deposit' | 'rebalance' | 'withdrawal'
    deposit_tx_id  = Column(Integer, ForeignKey("deposit_transactions.id"), nullable=True)
    proposal_id    = Column(Integer, ForeignKey("rebalance_proposals.id"), nullable=True)
    portfolio_id   = Column(Integer, ForeignKey("portfolios.id"), nullable=True)
    index_id       = Column(String, nullable=False)
    network_mode   = Column(String, nullable=False, default="mainnet")
    symbol         = Column(String, nullable=False)          # ex: DEFIssi, AAVE, LINK
    side           = Column(String, nullable=False)          # 'buy' | 'sell'
    quantity       = Column(Float, nullable=False, default=0.0)
    price_usd      = Column(Float, nullable=False, default=0.0)
    notional_usd   = Column(Float, nullable=False, default=0.0)
    status         = Column(String, nullable=False, default="filled")  # 'filled' | 'skipped' | 'failed'
    skip_reason    = Column(String, nullable=True)
    order_id       = Column(String, nullable=True)           # ID da ordem no SoDEX
    fee_usd        = Column(Float, default=0.0)              # taxa cobrada pelo SoDEX em USD
    fee_pct        = Column(Float, default=0.001)            # % da taxa (0.001 = 0.1%)
    fee_reason     = Column(String, nullable=True)           # ex: "Rebalance #157: SELL UNI substituída por HYPE"
    executed_at    = Column(DateTime, nullable=False, default=datetime.utcnow)
    created_at     = Column(DateTime, default=datetime.utcnow)


class Announcement(Base):
    __tablename__ = "announcements"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    source           = Column(String, nullable=False, default="sodex")  # sodex | manual
    external_id      = Column(String, nullable=True)      # ID externo p/ dedup
    title            = Column(Text, nullable=False)
    body             = Column(Text, nullable=True)         # detalhes adicionados pelo admin
    labels           = Column(JSON, default=list)          # ["Delistings", "Updates"]
    severity         = Column(String, nullable=False, default="info")  # info | warning | critical
    affects_symbols  = Column(JSON, default=list)          # ["ADA", "ZEC"]
    action_deadline  = Column(DateTime, nullable=True)
    published_at     = Column(DateTime, nullable=True)
    is_active        = Column(Boolean, nullable=False, default=True)
    created_at       = Column(DateTime, default=datetime.utcnow)


class UniverseAsset(Base):
    """Snapshot diário de todos os ativos do universo SoSoValue+SoDEX."""
    __tablename__ = "universe_assets"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    symbol          = Column(String, nullable=False)
    name            = Column(String, nullable=True)
    theme           = Column(String, nullable=False)          # depin | ai-crypto | rwa
    currency_id     = Column(String, nullable=True)
    ssi_weight      = Column(Float, default=0.0)             # peso no índice SSI (%)
    marketcap       = Column(Float, default=0.0)
    mcap_rank       = Column(Integer, default=999)
    volume_24h      = Column(Float, default=0.0)
    vol_mcap_ratio  = Column(Float, default=0.0)
    roi_7d          = Column(Float, default=0.0)
    roi_30d         = Column(Float, default=0.0)
    roi_3m          = Column(Float, default=0.0)
    high_3m         = Column(Float, default=0.0)             # máxima dos últimos 91d
    dist_from_high  = Column(Float, default=0.0)             # % abaixo da máxima 3m
    beta_vs_ssi     = Column(Float, default=1.0)             # beta vs índice SSI do tema
    current_price   = Column(Float, default=0.0)
    in_basket       = Column(Boolean, default=False)
    is_anchor       = Column(Boolean, default=False)
    sodex_listed    = Column(Boolean, default=False)
    sodex_status    = Column(String, default="UNKNOWN")      # TRADING | HALT | UNKNOWN
    updated_at      = Column(DateTime, default=datetime.utcnow)
