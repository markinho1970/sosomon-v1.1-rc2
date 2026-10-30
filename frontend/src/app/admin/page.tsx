"use client";

import React, { useState, useEffect, useLayoutEffect, useCallback, useRef } from "react";
import { useAccount, useSignMessage, useDisconnect, useChainId, useSwitchChain } from "wagmi";
import { useConnectModal } from "@rainbow-me/rainbowkit";
import {
  CheckCircle2, XCircle, RefreshCw, Clock, BarChart3, Users,
  DollarSign, AlertTriangle, ShieldCheck, Play, Zap, Wallet, ArrowRightLeft,
  Fuel, ExternalLink, ChevronDown, ChevronRight, Copy, Download,
  Home, Bell, Layers, Cpu, Menu, Activity, Server,
  TrendingUp, TrendingDown, Scale, Megaphone, Plus, Eye, EyeOff,
} from "lucide-react";
import { QRCodeSVG } from "qrcode.react";
import { adminApi, investApi, adminAnnouncementApi, feesApi, type SystemAlert } from "@/lib/api";
import type { Announcement } from "@/types";
import { useLang } from "@/lib/LanguageContext";
import { LANGUAGES, type Lang } from "@/lib/i18n/translations";

type AdminTab = "overview" | "proposals" | "indexes" | "treasury" | "investors" | "trades" | "agents" | "announcements" | "fees";

interface Proposal {
  id: number;
  index_id: string;
  index_name: string;
  status: "pending" | "approved" | "rejected" | "executed" | "failed" | "dry_run";
  trigger: string;
  proposed_at: string;
  approved_at: string | null;
  executed_at: string | null;
  changes: Array<{ symbol: string; old_weight: number; new_weight: number; action: string; rationale?: string }>;
  ai_rationale: string;
}

interface AdminStats {
  total_subscribers: number;
  pro_subscribers: number;
  total_aum_usd: number;
  pending_proposals: number;
  network_mode: string;
  indexes: Array<{ id: string; name: string; aum_usd: number; subscriber_count: number; return_30d_pct: number }>;
}

interface InvestorBasketItem {
  symbol: string;
  weight: number;
  price: number;
  est_usd: number;
  est_qty: number;
  change_7d: number;
}

interface InvestorPortfolioItem {
  id: string;            // DepositTransaction id — único por investimento
  portfolio_id: string;
  wallet_address: string;
  index_id: string;
  index_name: string;
  deposited_usd: number;
  current_value_usd: number;
  pnl_usd: number;
  pnl_pct: number;
  shares: number;
  pool_share_pct: number;
  is_pro: boolean;
  nav_at_buy: number;    // NAV na data do depósito
  high_water_mark: number;
  deposit_date: string | null;
  buy_confirmed: boolean;
  tx_hash: string | null;
  basket: InvestorBasketItem[];
}

interface InvestorsData {
  portfolios: InvestorPortfolioItem[];
  total_deposited_usd: number;
  total_current_usd: number;
  total_pnl_usd: number;
  total_pnl_pct: number;
  count: number;
  page: number;
  per_page: number;
  total_pages: number;
}

interface PortfolioPosition {
  asset: string;
  amount: number;
  usd_value: number;
  weight_pct: number;
}

interface Portfolio {
  positions: PortfolioPosition[];
  total_usd: number;
  network: string;
  wallet: string;
  configured: boolean;
}

interface Trade {
  id?: string;
  symbol?: string;
  side?: string;
  quantity?: string;
  price?: string;
  status?: string;
  created_at?: string;
  [key: string]: unknown;
}

interface Movement {
  id: string;
  type: "deposit" | "refund" | "withdrawal" | "manual" | "other";
  action: string;
  amount_usd: number;
  wallet: string;
  index_id: string;
  tx_hash: string;
  refund_tx: string;
  refund_ok: boolean | null;
  refund_basescan: string;
  basescan: string;
  manual_credit: boolean;
  reason: string;
  description: string;
  timestamp: string;
}

interface AuthSession {
  address: string;
  message: string;
  signature: string;
}

interface FundWalletInfo {
  address: string | null;
  usdc_balance: number | null;
  eth_balance: number | null;
  configured: boolean;
  network: string;
  chain_id: number;
  network_mode: string;
  usdc_contract: string;
  basescan_url: string;
  gas_price_gwei: number | null;
  possible_txs: number | null;
  gas_cost_usd: number | null;
}

const CHAIN_BASE_MAINNET = 8453;
const CHAIN_BASE_SEPOLIA = 84532;
const ADMIN_SESSION_KEY = "sosomon_admin_session";
const ADMIN_SESSION_MAX_AGE_S = 3600;

function fmtUSD(v: number) {
  if (v >= 1_000_000) return `$${(v / 1_000_000).toFixed(2)}M`;
  if (v >= 1_000) return `$${(v / 1_000).toFixed(1)}K`;
  return `$${v.toFixed(2)}`;
}

function timeAgo(iso: string) {
  const diff = Date.now() - new Date(iso).getTime();
  const h = Math.floor(diff / 3_600_000);
  if (h < 1) return `${Math.floor(diff / 60000)}m ago`;
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

const STATUS_BADGE: Record<string, string> = {
  pending:  "bg-amber-500/10 text-amber-400 border-amber-500/20",
  approved: "bg-green-500/10 text-green-400 border-green-500/20",
  rejected: "bg-red-500/10 text-red-400 border-red-500/20",
  executed: "bg-brand-blue/10 text-brand-blue border-brand-blue/20",
  failed:   "bg-red-600/10 text-red-400 border-red-600/20",
  dry_run:  "bg-purple-500/10 text-purple-400 border-purple-500/20",
};

export default function AdminPage() {
  const { address, isConnected, status: wagmiStatus } = useAccount();
  const { signMessageAsync } = useSignMessage();
  const { disconnect } = useDisconnect();
  const { openConnectModal } = useConnectModal();
  const { t, lang, setLang } = useLang();
  const chainId = useChainId();
  const { switchChain } = useSwitchChain();

  const [sessionChecked, setSessionChecked] = useState(false);
  const [session, setSession] = useState<AuthSession | null>(null);
  const [authError, setAuthError] = useState("");
  const [accessDenied, setAccessDenied] = useState(false);
  const [signing, setSigning] = useState(false);

  const [networkMode, setNetworkMode] = useState<"mainnet" | "testnet">("mainnet");
  const [activeTab, setActiveTab] = useState<AdminTab>("overview");
  const [sidebarOpen, setSidebarOpen] = useState(false);

  const [langOpen, setLangOpen] = useState(false);
  const langRef = useRef<HTMLDivElement>(null);
  const loadKeyRef = useRef("");

  // Data state
  const [proposals, setProposals] = useState<Proposal[]>([]);
  const [stats, setStats] = useState<AdminStats | null>(null);
  const [portfolio, setPortfolio] = useState<Portfolio | null>(null);
  const [trades, setTrades] = useState<Trade[]>([]);
  const [movements, setMovements] = useState<Movement[]>([]);
  const [fundWallet, setFundWallet] = useState<FundWalletInfo | null>(null);
  const [founderPortfolio, setFounderPortfolio] = useState<Record<string, unknown>[] | null>(null);
  const [investors, setInvestors] = useState<InvestorsData | null>(null);
  const [loadingInvestors, setLoadingInvestors] = useState(false);
  const [alerts, setAlerts] = useState<SystemAlert[]>([]);
  const [alertsCheckedAt, setAlertsCheckedAt] = useState("");
  const [liveUpdatedAt, setLiveUpdatedAt] = useState<Date | null>(null);
  const [liveSecs, setLiveSecs] = useState(0);
  const [report, setReport] = useState<Record<string, unknown> | null>(null);
  const [showReport, setShowReport] = useState(false);

  // Announcements state
  const [announcements, setAnnouncements] = useState<Announcement[]>([]);
  const [annLoading, setAnnLoading] = useState(false);
  const [annSyncing, setAnnSyncing] = useState(false);
  const [annEditId, setAnnEditId] = useState<number | null>(null);
  const [annEditBody, setAnnEditBody] = useState("");
  const [annEditSymbols, setAnnEditSymbols] = useState("");
  const [annEditDeadline, setAnnEditDeadline] = useState("");
  const [annCreateMode, setAnnCreateMode] = useState(false);
  const [annNewTitle, setAnnNewTitle] = useState("");
  const [annNewBody, setAnnNewBody] = useState("");
  const [annNewSeverity, setAnnNewSeverity] = useState("info");
  const [annNewSymbols, setAnnNewSymbols] = useState("");
  const [annNewDeadline, setAnnNewDeadline] = useState("");

  // Loading state
  const [loading, setLoading] = useState(false);
  const [loadingPortfolio, setLoadingPortfolio] = useState(false);
  const [loadingTrades, setLoadingTrades] = useState(false);
  const [feesSummary, setFeesSummary] = useState<any>(null);
  const [feesLoading, setFeesLoading] = useState(false);
  const [loadingFundWallet, setLoadingFundWallet] = useState(false);
  const [loadingMovements, setLoadingMovements] = useState(false);
  const [loadingReport, setLoadingReport] = useState(false);

  // Action state
  const [actionId, setActionId] = useState<number | null>(null);
  const [executingId, setExecutingId] = useState<number | null>(null);
  const [executeResult, setExecuteResult] = useState<Record<number, string>>({});
  const [expanded, setExpanded] = useState<number | null>(null);
  const [expandedMovement, setExpandedMovement] = useState<string | null>(null);
  const [copiedAddr, setCopiedAddr] = useState(false);
  const [investorPage, setInvestorPage] = useState(1);
  const [investorPerPage, setInvestorPerPage] = useState(25);
  const [selectedInvestor, setSelectedInvestor] = useState<InvestorPortfolioItem | null>(null);

  // Agent run state
  const [runningRebalancer, setRunningRebalancer] = useState(false);
  const [rebalancerMsg, setRebalancerMsg] = useState("");
  const [runningScout, setRunningScout] = useState(false);
  const [scoutMsg, setScoutMsg] = useState("");
  const [runningNavUpdate, setRunningNavUpdate] = useState(false);
  const [navUpdateMsg, setNavUpdateMsg] = useState("");

  // Filters
  const [movFilter, setMovFilter] = useState<"all" | "deposit" | "refund" | "withdrawal">("all");
  const [propFilter, setPropFilter] = useState<"all" | "pending" | "approved" | "executed" | "rejected">("pending");

  // ── Session restore ──────────────────────────────────────────────────────────
  useLayoutEffect(() => {
    const savedNet = localStorage.getItem("sosomon_admin_network");
    if (savedNet === "testnet" || savedNet === "mainnet") setNetworkMode(savedNet);
    try {
      const raw = localStorage.getItem(ADMIN_SESSION_KEY);
      if (raw) {
        const saved = JSON.parse(raw) as AuthSession;
        const ts = parseInt(saved.message.split("ts:").pop()?.trim() ?? "0", 10);
        if (!isNaN(ts) && Date.now() / 1000 - ts < ADMIN_SESSION_MAX_AGE_S) {
          setSession(saved);
          setSessionChecked(true);
          return;
        }
        localStorage.removeItem(ADMIN_SESSION_KEY);
      }
    } catch { /**/ }
    setSessionChecked(true);
  }, []);

  useEffect(() => {
    if (!address || !session) return;
    if (session.address.toLowerCase() !== address.toLowerCase()) expireSession();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [address]);

  useEffect(() => {
    if (wagmiStatus === "disconnected" && !localStorage.getItem(ADMIN_SESSION_KEY)) {
      setSession(null);
      setSessionChecked(true);
    }
  }, [wagmiStatus]);

  useEffect(() => {
    if (isConnected && !session && chainId !== CHAIN_BASE_MAINNET) {
      const hasStored = !!localStorage.getItem(ADMIN_SESSION_KEY);
      if (!hasStored) switchChain({ chainId: CHAIN_BASE_MAINNET });
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isConnected, chainId]);

  useEffect(() => {
    if (!session) return;
    const requiredChain = networkMode === "mainnet" ? CHAIN_BASE_MAINNET : CHAIN_BASE_SEPOLIA;
    if (chainId !== requiredChain) switchChain({ chainId: requiredChain });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [networkMode, session]);

  useEffect(() => {
    function handler(e: MouseEvent) {
      if (langRef.current && !langRef.current.contains(e.target as Node)) setLangOpen(false);
    }
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  function changeNetwork(mode: "mainnet" | "testnet") {
    loadKeyRef.current = "";
    setNetworkMode(mode);
    localStorage.setItem("sosomon_admin_network", mode);
    setReport(null);
    setShowReport(false);
    // Limpa dados stale da rede anterior imediatamente
    setTrades([]);
    setPortfolio(null);
    setFundWallet(null);
    setMovements([]);
    setFounderPortfolio(null);
  }

  function expireSession() {
    setSession(null);
    setAuthError("");
    setAccessDenied(false);
    localStorage.removeItem(ADMIN_SESSION_KEY);
  }

  async function handleSign() {
    if (!address) return;
    setSigning(true);
    setAuthError("");
    setAccessDenied(false);
    try {
      const ts = Math.floor(Date.now() / 1000);
      const message = `SoSoMon Admin Access | addr:${address} | ts:${ts}`;
      const signature = await signMessageAsync({ message });
      await adminApi.verifySignature(address, message, signature);
      const newSession = { address, message, signature };
      setSession(newSession);
      localStorage.setItem(ADMIN_SESSION_KEY, JSON.stringify(newSession));
    } catch (e: unknown) {
      const status = (e as { response?: { status?: number } })?.response?.status;
      if (status === 403) setAccessDenied(true);
      else if (status === 401) setAuthError(t("admin_sig_expired"));
      else if ((e as { name?: string })?.name === "UserRejectedRequestError") setAuthError(t("admin_sig_cancelled"));
      else setAuthError(t("admin_auth_error"));
    } finally { setSigning(false); }
  }

  // ── Data loading ─────────────────────────────────────────────────────────────
  const loadAll = useCallback(async (net: "mainnet" | "testnet") => {
    if (!session) return;
    setLoading(true);
    setLoadingPortfolio(true);
    setLoadingTrades(true);
    setLoadingFundWallet(true);
    try {
      const [p, s, fw] = await Promise.all([
        adminApi.getProposals(session.address, session.message, session.signature),
        adminApi.getStats(session.address, session.message, session.signature, net),
        adminApi.getFundWallet(session.address, session.message, session.signature, net),
      ]);
      setProposals(p ?? []);
      setStats(s ?? null);
      setFundWallet(fw ?? null);
    } catch (e: unknown) {
      const status = (e as { response?: { status?: number } })?.response?.status;
      if (status === 401) { expireSession(); return; }
    } finally {
      setLoading(false);
      setLoadingFundWallet(false);
    }
    try {
      setPortfolio(await adminApi.getPortfolio(session.address, session.message, session.signature, net));
    } catch { /**/ } finally { setLoadingPortfolio(false); }
    setLoadingInvestors(true);
    try {
      const inv = await adminApi.getInvestors(session.address, session.message, session.signature, net, 1, 25);
      setInvestors(inv ?? null);
      setInvestorPage(1);
    } catch { /**/ } finally { setLoadingInvestors(false); }
    // Carrega portfolio do founder como investidor
    try {
      const fp = await investApi.getPortfolio(session.address, net);
      setFounderPortfolio(Array.isArray(fp) ? fp as Record<string, unknown>[] : null);
    } catch { /**/ }
    try {
      const tr = await adminApi.getTrades(session.address, session.message, session.signature, 20, net);
      setTrades(Array.isArray(tr) ? tr : []);
    } catch { /**/ } finally { setLoadingTrades(false); }
    setLoadingMovements(true);
    try {
      const mv = await adminApi.getMovements(session.address, session.message, session.signature, net);
      setMovements((mv?.movements ?? []) as Movement[]);
    } catch { /**/ } finally { setLoadingMovements(false); }
    setLiveUpdatedAt(new Date());
    setLiveSecs(0);
  }, [session]);

  useEffect(() => {
    if (!session) { loadKeyRef.current = ""; return; }
    const key = `${session.address}:${networkMode}`;
    if (loadKeyRef.current === key) return;
    loadKeyRef.current = key;
    loadAll(networkMode);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session, networkMode]);

  const fetchAlerts = useCallback(async () => {
    if (!session) return;
    try {
      const data = await adminApi.alerts(session.address, session.message, session.signature);
      setAlerts(data.alerts ?? []);
      setAlertsCheckedAt(new Date(data.checked_at).toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" }));
    } catch { /**/ }
  }, [session]);

  useEffect(() => {
    if (!session) return;
    fetchAlerts();
    const id = setInterval(fetchAlerts, 60_000);
    return () => clearInterval(id);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session]);

  // Auto-refresh dados financeiros a cada 60s
  const loadLive = useCallback(async (net: "mainnet" | "testnet") => {
    if (!session || loading) return;
    try {
      const [s, fw, p] = await Promise.all([
        adminApi.getStats(session.address, session.message, session.signature, net),
        adminApi.getFundWallet(session.address, session.message, session.signature, net),
        adminApi.getPortfolio(session.address, session.message, session.signature, net),
      ]);
      if (s)  setStats(s);
      if (fw) setFundWallet(fw);
      if (p)  setPortfolio(p);
    } catch { /**/ }
    try {
      const inv = await adminApi.getInvestors(session.address, session.message, session.signature, net, investorPage, investorPerPage);
      if (inv) setInvestors(inv);
    } catch { /**/ }
    setLiveUpdatedAt(new Date());
    setLiveSecs(0);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session, loading, investorPage, investorPerPage]);

  useEffect(() => {
    if (!session) return;
    const id = setInterval(() => loadLive(networkMode), 60_000);
    return () => clearInterval(id);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session, networkMode]);

  useEffect(() => {
    const id = setInterval(() => setLiveSecs(s => s + 1), 1_000);
    return () => clearInterval(id);
  }, []);

  // Load announcements when tab becomes active
  useEffect(() => {
    if (activeTab !== "announcements" || !address || !signMessageAsync || annLoading) return;
    let cancelled = false;
    setAnnLoading(true);
    (async () => {
      try {
        const msg = `SoSoMon Admin\nTimestamp: ${new Date().toISOString()}`;
        const sig = await signMessageAsync({ message: msg });
        if (cancelled) return;
        const rows = await adminAnnouncementApi.getAll(address, msg, sig);
        if (!cancelled) setAnnouncements(rows);
      } catch (e) { console.error(e); }
      finally { if (!cancelled) setAnnLoading(false); }
    })();
    return () => { cancelled = true; };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab, address]);

  // ── Action handlers ──────────────────────────────────────────────────────────
  async function handleApprove(id: number) {
    if (!session) return;
    setActionId(id);
    try { await adminApi.approve(id, session.address, session.message, session.signature); await loadAll(networkMode); }
    finally { setActionId(null); }
  }

  async function handleReject(id: number) {
    if (!session) return;
    setActionId(id);
    try { await adminApi.reject(id, session.address, session.message, session.signature); await loadAll(networkMode); }
    finally { setActionId(null); }
  }

  async function handleExecute(id: number, dryRun: boolean) {
    if (!session) return;
    setExecutingId(id);
    setExecuteResult(prev => ({ ...prev, [id]: "" }));
    try {
      const res = await adminApi.executeProposal(id, session.address, session.message, session.signature, dryRun);
      setExecuteResult(prev => ({ ...prev, [id]: dryRun ? `Dry run: ${res.orders_count ?? 0} orders simulated` : `Executed: ${res.orders_count ?? 0} orders placed` }));
      await loadAll(networkMode);
    } catch (e: unknown) {
      const msg = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Execution failed";
      setExecuteResult(prev => ({ ...prev, [id]: `Error: ${msg}` }));
    } finally { setExecutingId(null); }
  }

  async function handleReport() {
    if (!session) return;
    setLoadingReport(true);
    try { const r = await adminApi.getReport(session.address, session.message, session.signature, networkMode); setReport(r); setShowReport(true); }
    catch { /**/ } finally { setLoadingReport(false); }
  }

  async function handleRunRebalancer() {
    if (!session) return;
    setRunningRebalancer(true);
    setRebalancerMsg("");
    try {
      const res = await adminApi.runRebalancer(session.address, session.message, session.signature, false);
      setRebalancerMsg(`Rebalancer: ${res.pending_proposals ?? 0} proposals pending.`);
      await loadAll(networkMode);
    } catch (e: unknown) {
      const msg = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed";
      setRebalancerMsg(`Erro: ${msg}`);
    } finally { setRunningRebalancer(false); }
  }

  async function handleRunScout() {
    if (!session) return;
    setRunningScout(true);
    setScoutMsg("");
    try {
      const res = await adminApi.runScout(session.address, session.message, session.signature);
      setScoutMsg(res.message || "Scout concluído.");
      await loadAll(networkMode);
    } catch (e: unknown) {
      const msg = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed";
      setScoutMsg(`Erro: ${msg}`);
    } finally { setRunningScout(false); }
  }

  async function handleRunNavUpdate() {
    if (!session) return;
    setRunningNavUpdate(true);
    setNavUpdateMsg("");
    try {
      const res = await adminApi.runNavUpdate(session.address, session.message, session.signature);
      setNavUpdateMsg(res.message || "NAV atualizado.");
    } catch (e: unknown) {
      const msg = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed";
      setNavUpdateMsg(`Erro: ${msg}`);
    } finally { setRunningNavUpdate(false); }
  }

  function copyFundAddress() {
    if (!fundWallet?.address) return;
    navigator.clipboard.writeText(fundWallet.address);
    setCopiedAddr(true);
    setTimeout(() => setCopiedAddr(false), 2000);
  }

  // ── Lang picker (EN + PT only for admin) ────────────────────────────────────
  const ADMIN_LANGS = LANGUAGES.filter(l => l.code === "en" || l.code === "pt");
  const currentLang = ADMIN_LANGS.find(l => l.code === lang) ?? ADMIN_LANGS[0]; // default EN

  function LangPicker() {
    return (
      <div ref={langRef} className="relative z-20">
        <button onClick={() => setLangOpen(v => !v)} className="flex items-center gap-1 px-2 py-1.5 rounded-lg border border-white/10 bg-white/5 text-white/50 hover:text-white text-xs transition-all">
          <span>{currentLang.flag}</span>
          <span>{currentLang.code.toUpperCase()}</span>
          <ChevronDown size={10} className={`transition-transform ${langOpen ? "rotate-180" : ""}`} />
        </button>
        {langOpen && (
          <div className="absolute right-0 top-full mt-1 w-36 bg-[#0d1117] border border-white/10 rounded-xl shadow-2xl py-1 z-50">
            {ADMIN_LANGS.map(l => (
              <button key={l.code} onClick={() => { setLang(l.code as Lang); setLangOpen(false); }}
                className={`w-full text-left flex items-center gap-2 px-3 py-2 text-xs transition-all ${lang === l.code ? "text-white bg-white/8" : "text-white/50 hover:text-white hover:bg-white/5"}`}>
                <span>{l.flag}</span><span>{l.label}</span>
                {lang === l.code && <span className="ml-auto w-1.5 h-1.5 rounded-full bg-amber-400" />}
              </button>
            ))}
          </div>
        )}
      </div>
    );
  }

  // ── Guards ───────────────────────────────────────────────────────────────────
  if (!sessionChecked) return <div className="min-h-screen bg-brand-dark" />;

  if (!isConnected) {
    return (
      <div className="min-h-screen bg-brand-dark flex items-center justify-center px-4">
        <div className="relative w-full max-w-sm card text-center">
          <LangPicker />
          <ShieldCheck size={36} className="text-brand-blue mx-auto mb-4" />
          <h1 className="text-xl font-bold text-white mb-1">{t("admin_title")}</h1>
          <p className="text-white/40 text-sm mb-6">{t("admin_connect_wallet")}</p>
          <button onClick={() => openConnectModal?.()} className="btn-primary w-full">{t("admin_connect_wallet")}</button>
        </div>
      </div>
    );
  }

  if (!session) {
    return (
      <div className="min-h-screen bg-brand-dark flex items-center justify-center px-4">
        <div className="relative w-full max-w-sm card text-center">
          <LangPicker />
          {accessDenied ? (
            <>
              <div className="w-14 h-14 rounded-full bg-red-500/10 border border-red-500/30 flex items-center justify-center mx-auto mb-4">
                <XCircle size={32} className="text-red-400" />
              </div>
              <h1 className="text-xl font-bold text-red-400 mb-2">{t("admin_not_authorized")}</h1>
              <div className="bg-red-500/10 border border-red-500/20 rounded-xl p-4 mb-5">
                <p className="text-red-300/80 text-sm">{t("admin_not_authorized_desc")}</p>
                <p className="text-white/30 font-mono text-xs mt-2">{address?.slice(0, 6)}…{address?.slice(-4)}</p>
              </div>
              <button onClick={() => { expireSession(); disconnect(); }} className="text-white/40 hover:text-white text-sm">{t("admin_disconnect")}</button>
            </>
          ) : (
            <>
              <ShieldCheck size={36} className="text-brand-blue mx-auto mb-4" />
              <h1 className="text-xl font-bold text-white mb-1">{t("admin_title")}</h1>
              <p className="text-white/40 text-sm mb-2">{t("admin_subtitle")}</p>
              <p className="text-xs text-white/30 font-mono mb-6">{address?.slice(0, 6)}…{address?.slice(-4)}</p>
              <p className="text-white/50 text-sm mb-4">{t("admin_sign_desc")}</p>
              {authError && <p className="text-red-400 text-xs mb-3">{authError}</p>}
              <button onClick={handleSign} disabled={signing} className="btn-primary w-full disabled:opacity-50">
                {signing ? t("admin_signing") : t("admin_sign_to_auth")}
              </button>
              <div className="mt-3">
                <button onClick={() => disconnect()} className="text-white/30 hover:text-white/60 text-xs">{t("admin_disconnect")}</button>
              </div>
            </>
          )}
        </div>
      </div>
    );
  }

  // ── Main admin UI ─────────────────────────────────────────────────────────────
  const pendingCount = proposals.filter(p => p.status === "pending").length;
  // stats.pending_proposals vem do backend (DB) e é mais confiável quando proposals
  // ainda não carregou ou falhou silenciosamente.
  const displayPendingCount = stats?.pending_proposals ?? pendingCount;
  const criticalAlerts = alerts.filter(a => a.severity === "critical");
  const isMainnet = networkMode === "mainnet";
  const ethBal = fundWallet?.eth_balance ?? 0;
  const possibleTxs = fundWallet?.possible_txs ?? null;
  const ethColor = possibleTxs !== null
    ? (possibleTxs >= 200 ? "text-green-400" : possibleTxs >= 50 ? "text-amber-400" : "text-red-400")
    : (ethBal >= 0.05 ? "text-green-400" : ethBal >= 0.01 ? "text-amber-400" : "text-red-400");
  const showBanner = displayPendingCount > 0 && activeTab !== "proposals";
  const ethLowAlert = fundWallet && possibleTxs !== null && possibleTxs < 200;

  const NAV_ITEMS: { id: AdminTab; icon: React.ElementType; label: string; badge: number }[] = [
    { id: "overview",   icon: Home,            label: t("admin_tab_overview"),      badge: criticalAlerts.length },
    { id: "proposals",  icon: Bell,            label: t("admin_tab_proposals"),     badge: displayPendingCount },
    { id: "indexes",    icon: Layers,          label: t("admin_tab_indexes"),       badge: 0 },
    { id: "treasury",   icon: Wallet,          label: t("admin_tab_treasury"),      badge: ethLowAlert ? 1 : 0 },
    { id: "investors",  icon: Users,           label: t("admin_tab_investors_tab"), badge: 0 },
    { id: "trades",     icon: ArrowRightLeft,  label: t("admin_tab_trades"),        badge: 0 },
    { id: "agents",     icon: Cpu,             label: t("admin_tab_agents"),        badge: 0 },
    { id: "fees",       icon: DollarSign,      label: "Taxas",                      badge: 0 },
    { id: "announcements", icon: Megaphone,    label: "Anúncios",                   badge: 0 },
  ];

  function goTab(tab: AdminTab) { setActiveTab(tab); setSidebarOpen(false); }

  return (
    <div className="min-h-screen bg-brand-dark">

      {/* ── Fixed top header ─────────────────────────────────────────────── */}
      <header className="fixed top-0 left-0 right-0 z-40 h-12 border-b border-white/5 bg-black/90 backdrop-blur-sm flex items-center px-4 gap-3">
        <button onClick={() => setSidebarOpen(v => !v)} className="lg:hidden text-white/40 hover:text-white mr-1 transition-colors">
          <Menu size={18} />
        </button>
        <span className="font-bold text-white text-sm">SoSoMon</span>
        <span className="text-white/15">·</span>
        <span className="text-white/40 text-xs hidden sm:block">{t("admin_founder_console")}</span>
        <span className="text-white/20 font-mono text-xs hidden sm:block">{session.address.slice(0, 6)}…{session.address.slice(-4)}</span>

        <div className="ml-auto flex items-center gap-2">
          <div className="flex rounded-lg border border-white/10 overflow-hidden text-xs">
            <button onClick={() => changeNetwork("mainnet")} className={`px-3 py-1.5 font-semibold transition-all ${isMainnet ? "bg-green-500/20 text-green-400" : "text-white/30 hover:text-white/60"}`}>
              🟢 {t("admin_network_mainnet")}
            </button>
            <button onClick={() => changeNetwork("testnet")} className={`px-3 py-1.5 font-semibold transition-all ${!isMainnet ? "bg-yellow-500/20 text-yellow-400" : "text-white/30 hover:text-white/60"}`}>
              🟡 {t("admin_network_testnet")}
            </button>
          </div>
          <button
            onClick={() => { loadAll(networkMode); setLiveUpdatedAt(null); }}
            title={t("admin_refresh_all")}
            className="flex items-center gap-1.5 text-white/30 hover:text-white transition-colors text-xs"
          >
            {loading ? (
              <RefreshCw size={14} className="animate-spin" />
            ) : (
              <span className="relative flex h-2 w-2">
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-green-400 opacity-75" />
                <span className="relative inline-flex rounded-full h-2 w-2 bg-green-500" />
              </span>
            )}
            {!loading && liveUpdatedAt && (
              <span className="text-white/20 hidden sm:inline">
                {liveSecs < 60 ? `${liveSecs}s` : `${Math.floor(liveSecs / 60)}m`}
              </span>
            )}
          </button>
          <LangPicker />
        </div>
      </header>

      {/* ── Pending proposals banner ─────────────────────────────────────── */}
      {showBanner && (
        <div
          onClick={() => goTab("proposals")}
          className="fixed top-12 left-0 right-0 z-30 bg-amber-500/10 border-b border-amber-500/20 px-4 lg:pl-[216px] py-1.5 flex items-center gap-2 cursor-pointer hover:bg-amber-500/15 transition-colors"
        >
          <Bell size={11} className="text-amber-400 shrink-0" />
          <p className="text-amber-300 text-xs font-medium">
            {displayPendingCount === 1 ? t("admin_pending_banner_one") : t("admin_pending_banner_many").replace("{n}", String(displayPendingCount))}
          </p>
          <span className="ml-auto text-amber-400/50 text-xs shrink-0">{t("admin_pending_banner_link")}</span>
        </div>
      )}

      {/* ── Mobile sidebar overlay ───────────────────────────────────────── */}
      {sidebarOpen && (
        <div onClick={() => setSidebarOpen(false)} className="fixed inset-0 bg-black/60 z-30 lg:hidden" />
      )}

      {/* ── Sidebar ──────────────────────────────────────────────────────── */}
      <aside className={`fixed left-0 top-12 bottom-0 w-52 bg-[#07090d] border-r border-white/5 z-30 flex flex-col transition-transform duration-200 ${sidebarOpen ? "translate-x-0" : "-translate-x-full lg:translate-x-0"}`}>
        <nav className="flex-1 py-3 px-2 space-y-0.5 overflow-y-auto">
          {NAV_ITEMS.map(item => {
            const active = activeTab === item.id;
            return (
              <button
                key={item.id}
                onClick={() => goTab(item.id)}
                className={`w-full flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-all ${active ? "bg-white/8 text-white" : "text-white/40 hover:text-white/70 hover:bg-white/4"}`}
              >
                <item.icon size={15} className={active ? "text-brand-blue" : "text-white/25"} />
                <span className="flex-1 text-left">{item.label}</span>
                {item.badge > 0 && (
                  <span className={`min-w-[18px] h-[18px] flex items-center justify-center rounded-full text-[10px] font-bold px-1 ${item.id === "proposals" ? "bg-amber-500 text-black" : "bg-red-500 text-white"}`}>
                    {item.badge}
                  </span>
                )}
              </button>
            );
          })}
        </nav>
        <div className="p-3 border-t border-white/5 space-y-1">
          <div className="px-3 py-1.5">
            <p className="text-white/20 text-[10px] font-mono truncate">{session.address}</p>
          </div>
          <button onClick={() => { expireSession(); disconnect(); }} className="w-full text-left text-white/25 hover:text-white/60 text-xs px-3 py-2 rounded-lg hover:bg-white/4 transition-colors">
            {t("admin_disconnect")}
          </button>
        </div>
      </aside>

      {/* ── Content area ─────────────────────────────────────────────────── */}
      <main className={`lg:ml-52 min-h-screen ${showBanner ? "pt-20" : "pt-12"}`}>
        <div className="max-w-4xl mx-auto px-4 py-6 space-y-5">

          {/* ══════════════════════ OVERVIEW ══════════════════════════════ */}
          {activeTab === "overview" && (
            <>
              {stats && (
                <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                  <div className="stat-card">
                    <div className="flex items-center gap-1.5 mb-1"><DollarSign size={13} className="text-white/30" /><p className="stat-label">{t("admin_total_aum")}</p></div>
                    <p className="stat-value">{fmtUSD(stats.total_aum_usd)}</p>
                  </div>
                  <div className="stat-card">
                    <div className="flex items-center gap-1.5 mb-1"><Users size={13} className="text-white/30" /><p className="stat-label">{t("admin_subscribers")}</p></div>
                    <p className="stat-value">{stats.total_subscribers}</p>
                    <p className="text-xs text-white/30">{t("admin_pro_subscribers").replace("{n}", String(stats.pro_subscribers))}</p>
                  </div>
                  <div className="stat-card">
                    <div className="flex items-center gap-1.5 mb-1"><BarChart3 size={13} className="text-white/30" /><p className="stat-label">{t("admin_indexes")}</p></div>
                    <p className="stat-value">{stats.indexes.length}</p>
                  </div>
                  <div className={`stat-card cursor-pointer transition-colors ${displayPendingCount > 0 ? "hover:border-amber-500/30" : ""}`} onClick={() => displayPendingCount > 0 && goTab("proposals")}>
                    <div className="flex items-center gap-1.5 mb-1"><AlertTriangle size={13} className="text-amber-400" /><p className="stat-label">{t("admin_pending")}</p></div>
                    <p className={`stat-value ${displayPendingCount > 0 ? "text-amber-400" : "text-white"}`}>{displayPendingCount}</p>
                    <p className="text-xs text-white/30">{t("admin_proposals_label")}</p>
                  </div>
                </div>
              )}

              <div className="space-y-2">
                {alerts.length === 0 ? (
                  <div className="flex items-center gap-2 px-4 py-2.5 rounded-xl border border-green-500/20 bg-green-500/5">
                    <CheckCircle2 size={13} className="text-green-400 shrink-0" />
                    <p className="text-green-400/70 text-xs">{t("admin_all_systems_ok")}</p>
                    {alertsCheckedAt && <span className="ml-auto text-white/20 text-xs">{alertsCheckedAt}</span>}
                  </div>
                ) : alerts.map((alert) => {
                  const cls: Record<string, string> = {
                    critical: "border-red-500/40 bg-red-500/5 text-red-300",
                    warning:  "border-amber-500/40 bg-amber-500/5 text-amber-300",
                    info:     "border-blue-500/40 bg-blue-500/5 text-blue-300",
                  };
                  const icons: Record<string, React.ReactNode> = {
                    critical: <AlertTriangle size={13} className="text-red-400 shrink-0 mt-0.5" />,
                    warning:  <AlertTriangle size={13} className="text-amber-400 shrink-0 mt-0.5" />,
                    info:     <Clock size={13} className="text-blue-400 shrink-0 mt-0.5" />,
                  };
                  return (
                    <div key={alert.id} className={`flex items-start gap-3 px-4 py-3 rounded-xl border ${cls[alert.severity] ?? "border-white/10 bg-white/5 text-white/60"}`}>
                      {icons[alert.severity]}
                      <div className="flex-1 min-w-0">
                        <p className="text-sm font-medium">{alert.title}</p>
                        <p className="text-xs opacity-70 mt-0.5">{alert.message}</p>
                      </div>
                    </div>
                  );
                })}
              </div>

              <div className="card">
                <h2 className="font-semibold text-white mb-3 flex items-center gap-2 text-sm">
                  <Server size={14} className="text-brand-blue" /> {t("admin_agent_health")}
                </h2>
                <div className="grid grid-cols-2 gap-2">
                  {[
                    { name: "Scout",           freq: t("admin_scout_schedule") },
                    { name: "Rebalancer",      freq: t("admin_rebal_schedule") },
                    { name: "NAV Updater",     freq: t("admin_nav_schedule") },
                    { name: "Deposit Monitor", freq: t("admin_deposit_schedule") },
                  ].map(agent => (
                    <div key={agent.name} className="flex items-center gap-3 p-3 rounded-lg bg-white/3 border border-white/5">
                      <div className="w-2 h-2 rounded-full bg-green-400 shrink-0" />
                      <div>
                        <p className="text-white text-xs font-medium">{agent.name}</p>
                        <p className="text-white/30 text-[10px]">{agent.freq}</p>
                      </div>
                    </div>
                  ))}
                </div>
                <button onClick={() => goTab("agents")} className="mt-3 text-brand-blue text-xs hover:underline">
                  {t("admin_manage_agents_link")}
                </button>
              </div>

              {stats && stats.indexes.length > 0 && (
                <div className="card">
                  <h2 className="font-semibold text-white mb-3 flex items-center gap-2 text-sm">
                    <Layers size={14} className="text-brand-blue" /> {t("admin_tab_indexes")}
                  </h2>
                  <div className="space-y-2">
                    {stats.indexes.map(idx => (
                      <div key={idx.id} className="flex items-center justify-between p-3 rounded-lg bg-white/3">
                        <div>
                          <p className="text-sm text-white font-medium">{idx.name}</p>
                          <p className="text-xs text-white/30">{t("admin_investors_count").replace("{n}", String(idx.subscriber_count))}</p>
                        </div>
                        <div className="text-right">
                          <p className="text-sm text-white font-medium">{fmtUSD(idx.aum_usd)}</p>
                          <p className={`text-xs ${idx.return_30d_pct > 0 ? "text-green-400" : idx.return_30d_pct < 0 ? "text-red-400" : "text-white/50"}`}>
                            {idx.return_30d_pct > 0 ? "+" : ""}{idx.return_30d_pct.toFixed(1)}% 30d
                          </p>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {movements.length > 0 && (
                <div className="card">
                  <h2 className="font-semibold text-white mb-3 flex items-center gap-2 text-sm">
                    <Activity size={14} className="text-brand-blue" /> {t("admin_recent_activity")}
                  </h2>
                  <div className="space-y-1">
                    {movements.slice(0, 6).map(m => {
                      const typeColor: Record<string, string> = { deposit: "text-green-400", refund: "text-orange-400", withdrawal: "text-red-400", manual: "text-blue-400", other: "text-white/40" };
                      return (
                        <div key={m.id} className="flex items-center gap-3 py-2 border-b border-white/5 last:border-0">
                          <span className={`text-xs font-medium w-16 shrink-0 ${typeColor[m.type] ?? "text-white/40"}`}>{m.type}</span>
                          <span className="text-white text-xs">${m.amount_usd.toFixed(2)}</span>
                          <span className="text-white/30 text-xs font-mono truncate flex-1">{m.wallet.slice(0, 8)}…{m.wallet.slice(-4)}</span>
                          <span className="text-white/20 text-xs shrink-0">{new Date(m.timestamp).toLocaleDateString("en-US", { month: "short", day: "numeric" })}</span>
                        </div>
                      );
                    })}
                  </div>
                  {movements.length > 6 && (
                    <button onClick={() => goTab("investors")} className="mt-2 text-brand-blue text-xs hover:underline">
                      {t("admin_view_all_link")}
                    </button>
                  )}
                </div>
              )}
            </>
          )}

          {/* ══════════════════════ PROPOSALS ═════════════════════════════ */}
          {activeTab === "proposals" && (
            <>
              <div className="flex items-center justify-between flex-wrap gap-3">
                <h2 className="font-semibold text-white flex items-center gap-2 text-sm">
                  <Bell size={14} className="text-brand-blue" /> {t("admin_tab_proposals")}
                  {pendingCount > 0 && (
                    <span className="px-2 py-0.5 rounded-full bg-amber-500/10 text-amber-400 text-xs border border-amber-500/20">
                      {t("admin_pending_count").replace("{n}", String(pendingCount))}
                    </span>
                  )}
                </h2>
                <div className="flex items-center gap-1">
                  {([
                    { key: "pending",  label: t("admin_prop_filter_pending") },
                    { key: "approved", label: t("admin_prop_filter_approved") },
                    { key: "executed", label: t("admin_prop_filter_executed") },
                    { key: "rejected", label: t("admin_prop_filter_rejected") },
                    { key: "all",      label: t("admin_prop_filter_all") },
                  ] as const).map(f => (
                    <button key={f.key} onClick={() => setPropFilter(f.key)} className={`px-2.5 py-1 rounded-lg text-xs transition-all ${propFilter === f.key ? "bg-white/10 text-white" : "text-white/30 hover:text-white/60"}`}>
                      {f.label}
                    </button>
                  ))}
                </div>
              </div>

              <div className="flex items-center gap-2">
                <button onClick={handleRunRebalancer} disabled={runningRebalancer} className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-brand-blue/10 border border-brand-blue/20 text-brand-blue hover:bg-brand-blue/20 text-xs transition-all disabled:opacity-40">
                  <Play size={12} className={runningRebalancer ? "animate-pulse" : ""} />
                  {runningRebalancer ? t("admin_running_rebalancer") : t("admin_run_rebalancer_btn")}
                </button>
                {rebalancerMsg && <p className="text-brand-blue text-xs">{rebalancerMsg}</p>}
              </div>

              {(() => {
                const filtered = proposals.filter(p => propFilter === "all" || p.status === propFilter);
                if (filtered.length === 0) return (
                  <div className="card text-center py-10">
                    <p className="text-white/30 text-sm">{t("admin_no_proposals_filter")} &ldquo;{propFilter}&rdquo;</p>
                    <p className="text-white/20 text-xs mt-1">{t("admin_scout_daily_note")}</p>
                  </div>
                );
                return (
                  <div className="space-y-3">
                    {filtered.map(p => (
                      <div key={p.id} className={`card ${p.status === "pending" ? "border border-amber-500/20" : ""}`}>
                        <div className="flex flex-col md:flex-row md:items-start gap-4">
                          <div className="flex-1">
                            <div className="flex items-center gap-2 flex-wrap mb-1.5">
                              <span className={`text-xs px-2 py-0.5 rounded-full border ${STATUS_BADGE[p.status] ?? STATUS_BADGE.pending}`}>{p.status}</span>
                              <span className="text-xs text-white/40 font-mono">#{p.id}</span>
                              <span className="text-xs text-white/60 font-medium">{p.index_name}</span>
                              <span className="text-xs text-white/25">{t("admin_trigger_label")}: {p.trigger}</span>
                              <span className="text-xs text-white/25 flex items-center gap-1"><Clock size={10} /> {timeAgo(p.proposed_at)}</span>
                            </div>
                            <p className="text-sm text-white/60 leading-relaxed">{p.ai_rationale}</p>
                            {executeResult[p.id] && (
                              <p className={`text-xs mt-2 ${executeResult[p.id].startsWith("Error") ? "text-red-400" : "text-green-400"}`}>
                                {executeResult[p.id]}
                              </p>
                            )}
                            {p.changes && p.changes.length > 0 && (
                              <div className="mt-3">
                                <button onClick={() => setExpanded(expanded === p.id ? null : p.id)} className="text-xs text-brand-blue hover:underline">
                                  {expanded === p.id
                                    ? t("admin_hide_changes")
                                    : t("admin_show_changes").replace("{n}", String(p.changes.length))}
                                </button>
                                {expanded === p.id && (
                                  <div className="mt-2 grid grid-cols-2 md:grid-cols-3 gap-2">
                                    {p.changes.map((c, i) => (
                                      <div key={i} className="bg-white/3 rounded-lg p-2 text-xs">
                                        <span className="text-white font-mono">{c.symbol}</span>
                                        <span className="text-white/30 ml-2">{c.action}</span>
                                        {c.old_weight !== undefined && <div className="text-white/30 mt-0.5">{c.old_weight}% → <span className="text-white">{c.new_weight}%</span></div>}
                                        {c.rationale && <p className="text-white/20 mt-1 leading-tight">{c.rationale}</p>}
                                      </div>
                                    ))}
                                  </div>
                                )}
                              </div>
                            )}
                          </div>
                          <div className="flex flex-col gap-2 shrink-0">
                            {p.status === "pending" && (
                              <div className="flex gap-2">
                                <button onClick={() => handleReject(p.id)} disabled={actionId === p.id} className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-red-500/20 text-red-400 hover:bg-red-500/10 text-xs transition-all disabled:opacity-40">
                                  <XCircle size={13} /> {t("admin_reject")}
                                </button>
                                <button onClick={() => handleApprove(p.id)} disabled={actionId === p.id} className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-green-500/10 border border-green-500/20 text-green-400 hover:bg-green-500/20 text-xs transition-all disabled:opacity-40">
                                  <CheckCircle2 size={13} /> {t("admin_approve")}
                                </button>
                              </div>
                            )}
                            {(p.status === "approved" || p.status === "pending") && (
                              <div className="flex gap-2">
                                <button onClick={() => handleExecute(p.id, true)} disabled={executingId === p.id} className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-purple-500/20 text-purple-400 hover:bg-purple-500/10 text-xs transition-all disabled:opacity-40">
                                  <Zap size={12} /> {t("admin_dry_run")}
                                </button>
                                <button onClick={() => handleExecute(p.id, false)} disabled={executingId === p.id} className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-brand-blue/10 border border-brand-blue/20 text-brand-blue hover:bg-brand-blue/20 text-xs transition-all disabled:opacity-40">
                                  <Play size={12} className={executingId === p.id ? "animate-pulse" : ""} />
                                  {executingId === p.id ? t("admin_executing") : t("admin_execute")}
                                </button>
                              </div>
                            )}
                            {p.status !== "pending" && p.status !== "approved" && (
                              <div className="text-xs text-white/20 space-y-0.5">
                                {p.approved_at && <p>{t("admin_approved_time").replace("{t}", timeAgo(p.approved_at))}</p>}
                                {p.executed_at && <p>{t("admin_executed_time").replace("{t}", timeAgo(p.executed_at))}</p>}
                              </div>
                            )}
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                );
              })()}
            </>
          )}

          {/* ══════════════════════ INDEXES ═══════════════════════════════ */}
          {activeTab === "indexes" && (
            <>
              <h2 className="font-semibold text-white flex items-center gap-2 text-sm">
                <Layers size={14} className="text-brand-blue" /> {t("admin_indexes_net").replace("{net}", isMainnet ? t("admin_network_mainnet") : t("admin_network_testnet"))}
              </h2>
              {!stats ? (
                <p className="text-white/30 text-sm">{t("admin_loading")}</p>
              ) : (
                <div className="space-y-4">
                  {stats.indexes.map(idx => (
                    <div key={idx.id} className="card">
                      <div className="flex items-start justify-between mb-4">
                        <div>
                          <h3 className="text-white font-semibold">{idx.name}</h3>
                          <p className="text-white/25 text-xs font-mono mt-0.5">{idx.id}</p>
                        </div>
                        <div className="text-right">
                          <p className="text-white font-bold text-lg">{fmtUSD(idx.aum_usd)}</p>
                          <p className={`text-xs ${idx.return_30d_pct > 0 ? "text-green-400" : idx.return_30d_pct < 0 ? "text-red-400" : "text-white/50"}`}>
                            {idx.return_30d_pct > 0 ? "+" : ""}{idx.return_30d_pct.toFixed(2)}% 30d
                          </p>
                        </div>
                      </div>
                      <div className="grid grid-cols-3 gap-3 text-xs">
                        <div className="bg-white/3 rounded-lg p-2.5">
                          <p className="text-white/30 mb-0.5">{t("admin_tab_investors_tab")}</p>
                          <p className="text-white font-medium">{idx.subscriber_count}</p>
                        </div>
                        <div className="bg-white/3 rounded-lg p-2.5">
                          <p className="text-white/30 mb-0.5">{t("admin_total_aum")}</p>
                          <p className="text-white font-medium">{fmtUSD(idx.aum_usd)}</p>
                        </div>
                        <div className="bg-white/3 rounded-lg p-2.5">
                          <p className="text-white/30 mb-0.5">30d</p>
                          <p className={`font-medium ${idx.return_30d_pct > 0 ? "text-green-400" : idx.return_30d_pct < 0 ? "text-red-400" : "text-white"}`}>
                            {idx.return_30d_pct > 0 ? "+" : ""}{idx.return_30d_pct.toFixed(2)}%
                          </p>
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </>
          )}

          {/* ══════════════════════ TREASURY ══════════════════════════════ */}
          {activeTab === "treasury" && (
            <>
              <h2 className="font-semibold text-white flex items-center gap-2 text-sm">
                <Wallet size={14} className="text-brand-blue" /> {t("admin_treasury_title")}
                <span className={`ml-auto text-xs px-2 py-0.5 rounded-full border font-medium ${isMainnet ? "text-green-400 bg-green-500/10 border-green-500/20" : "text-yellow-400 bg-yellow-500/10 border-yellow-500/20"}`}>
                  {isMainnet ? `🟢 ${t("admin_network_mainnet")}` : `🟡 ${t("admin_network_testnet")}`}
                </span>
              </h2>

              {/* Aviso testnet */}
              {!isMainnet && (
                <div className="flex items-start gap-3 p-3 rounded-xl bg-yellow-500/8 border border-yellow-500/20">
                  <span className="text-yellow-400 text-base mt-0.5">⚠️</span>
                  <div>
                    <p className="text-yellow-300 text-sm font-semibold">{t("admin_testnet_warn_title")}</p>
                    <p className="text-yellow-300/60 text-xs mt-0.5">{t("admin_testnet_warn_body")}</p>
                  </div>
                </div>
              )}

              {/* ── FUND WALLET (sticky) ─────────────────────────────────────── */}
              <div className="sticky top-0 z-20 -mx-4 sm:-mx-6 px-4 sm:px-6 pb-3 bg-[#0a0a0f]/97 backdrop-blur-md border-b border-white/5">
              <div className="card mt-0">
                <div className="flex items-center justify-between mb-4">
                  <div className="flex items-center gap-2">
                    <Fuel size={15} className="text-amber-400" />
                    <h3 className="font-semibold text-white text-sm">{t("admin_fund_wallet_label")}</h3>
                  </div>
                  <button onClick={() => { setFundWallet(null); loadAll(networkMode); }} className="text-white/30 hover:text-white transition-colors">
                    <RefreshCw size={13} className={loadingFundWallet ? "animate-spin" : ""} />
                  </button>
                </div>
                {!fundWallet && loadingFundWallet && <p className="text-white/30 text-sm text-center py-4">{t("admin_loading")}</p>}
                {!fundWallet && !loadingFundWallet && (
                  <div className="bg-white/3 border border-white/8 rounded-lg p-4 text-center">
                    <p className="text-white/40 text-sm">{t("admin_click_refresh")}</p>
                  </div>
                )}
                {fundWallet && (
                  <div className="space-y-3">
                    <div className={`p-3 rounded-xl border ${possibleTxs !== null && possibleTxs < 50 ? "bg-red-500/8 border-red-500/25" : possibleTxs !== null && possibleTxs < 200 ? "bg-amber-500/8 border-amber-500/20" : "bg-white/3 border-white/5"}`}>
                      <div className="flex items-center justify-between">
                        <div className="flex items-center gap-2">
                          <Fuel size={14} className={ethColor} />
                          <span className="text-white/60 text-sm">{t("admin_eth_gas")}</span>
                        </div>
                        <div className="text-right">
                          <span className={`font-bold text-sm ${ethColor}`}>{fundWallet.eth_balance !== null ? fundWallet.eth_balance.toFixed(6) : "—"} ETH</span>
                        </div>
                      </div>
                      <div className="mt-2 flex items-center justify-between">
                        <span className="text-white/35 text-xs">{t("admin_possible_txs")}</span>
                        <span className={`font-mono font-bold text-xs ${ethColor}`}>
                          {possibleTxs !== null ? `~${possibleTxs.toLocaleString("pt-BR")}` : "—"}
                        </span>
                      </div>
                      {fundWallet.gas_price_gwei !== null && (
                        <div className="flex items-center justify-between mt-1">
                          <span className="text-white/25 text-xs">{t("admin_gas_price_now")}</span>
                          <span className="text-white/35 font-mono text-xs">{fundWallet.gas_price_gwei?.toFixed(4)} gwei</span>
                        </div>
                      )}
                      {possibleTxs !== null && possibleTxs < 50 && (
                        <p className="text-red-400 text-xs mt-2 font-medium">{t("admin_eth_critical")}</p>
                      )}
                      {possibleTxs !== null && possibleTxs >= 50 && possibleTxs < 200 && (
                        <p className="text-amber-400 text-xs mt-2">{t("admin_eth_low")}</p>
                      )}
                    </div>
                  </div>
                )}
              </div>

              {/* ── Reserva Admin (vUSDC no SoDEX = sempre do admin) ── */}
              {portfolio && portfolio.configured && (
                <div className="card mt-3 py-2.5">
                  {(() => {
                    const usdcPos = portfolio.positions.find((p: PortfolioPosition) =>
                      ["USDC", "vUSDC", "usdc"].includes(p.asset)
                    );
                    const adminUsdc = usdcPos?.usd_value ?? 0;
                    const adminAmt  = usdcPos?.amount ?? 0;
                    return (
                      <div className="flex items-center justify-between">
                        <div className="flex items-center gap-2">
                          <DollarSign size={13} className="text-amber-400" />
                          <div>
                            <p className="text-xs font-semibold text-white/60 uppercase tracking-wider">{t("admin_reserve_title")}</p>
                            <p className="text-white/25 text-xs font-mono">{adminAmt.toFixed(4)} USDC · SoDEX</p>
                          </div>
                        </div>
                        <span className={`font-mono font-bold text-sm ${adminUsdc > 0 ? "text-amber-300" : "text-white/30"}`}>
                          ${adminUsdc.toFixed(2)}
                        </span>
                      </div>
                    );
                  })()}
                </div>
              )}

              {/* ── Deposit ETH for gas (fora do card, dentro do sticky) ── */}
              {fundWallet && (
                <div className={`rounded-xl border p-3 mt-3 ${isMainnet ? "border-green-500/20 bg-green-500/5" : "border-yellow-500/20 bg-yellow-500/5"}`}>
                  <div className="flex items-center gap-1.5 mb-3">
                    <Download size={12} className={isMainnet ? "text-green-400" : "text-yellow-400"} />
                    <span className={`text-xs font-semibold ${isMainnet ? "text-green-400" : "text-yellow-400"}`}>
                      {t("admin_deposit_eth_gas")} ({isMainnet ? "Base" : "Base Sepolia"})
                    </span>
                  </div>
                  <div className="flex gap-4 items-center">
                    <div className="shrink-0 rounded-lg overflow-hidden border border-white/10 bg-white p-1.5">
                      <QRCodeSVG value={`ethereum:${fundWallet.address}@${isMainnet ? CHAIN_BASE_MAINNET : CHAIN_BASE_SEPOLIA}`} size={80} bgColor="#ffffff" fgColor="#000000" level="M" />
                    </div>
                    <div className="flex-1 min-w-0 space-y-2">
                      <p className="text-white/50 font-mono text-xs truncate">{fundWallet.address}</p>
                      <div className="flex gap-2 flex-wrap">
                        <button onClick={copyFundAddress} className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-white/5 border border-white/10 text-white/60 hover:text-white text-xs transition-all">
                          <Copy size={11} /> {copiedAddr ? t("admin_copied") : t("admin_copy")}
                        </button>
                        {fundWallet.basescan_url && (
                          <a href={fundWallet.basescan_url} target="_blank" rel="noopener noreferrer" className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-white/5 border border-white/10 text-white/60 hover:text-white text-xs transition-all">
                            <ExternalLink size={11} /> Basescan
                          </a>
                        )}
                      </div>
                    </div>
                  </div>
                </div>
              )}
              </div>{/* /sticky */}

              {/* ── PORTFÓLIO DE INVESTIDORES ─────────────────────────────────── */}
              {/* ─── tabela paginada ─── */}
              <div className="card mt-4">
                <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
                  <div>
                    <div className="flex items-center gap-2">
                      <Users size={15} className="text-brand-blue" />
                      <h3 className="font-semibold text-white text-sm">{t("admin_investor_portfolio")}</h3>
                      {investors && <span className="text-xs px-1.5 py-0.5 rounded bg-white/5 text-white/40">{investors.count}</span>}
                    </div>
                    {investors && <p className="text-xs text-white/30 mt-0.5 ml-5.5">{t("admin_aum_prefix")}{investors.total_current_usd.toFixed(2)} · P&L: <span className={investors.total_pnl_pct >= 0 ? "text-green-400" : "text-red-400"}>{investors.total_pnl_pct >= 0 ? "+" : ""}{investors.total_pnl_pct.toFixed(2)}%</span></p>}
                  </div>
                  <div className="flex items-center gap-2">
                    <select
                      value={investorPerPage}
                      onChange={e => { const v = Number(e.target.value); setInvestorPerPage(v); setInvestorPage(1); setLoadingInvestors(true); adminApi.getInvestors(session!.address, session!.message, session!.signature, networkMode, 1, v).then(d => { setInvestors(d ?? null); setLoadingInvestors(false); }).catch(() => setLoadingInvestors(false)); }}
                      className="text-xs bg-white/5 border border-white/10 rounded-lg px-2 py-1.5 text-white/60 cursor-pointer"
                    >
                      <option value={25}>25 {t("admin_per_page")}</option>
                      <option value={50}>50 {t("admin_per_page")}</option>
                      <option value={100}>100 {t("admin_per_page")}</option>
                    </select>
                    <button onClick={() => { setLoadingInvestors(true); adminApi.getInvestors(session!.address, session!.message, session!.signature, networkMode, investorPage, investorPerPage).then(d => { setInvestors(d ?? null); setLoadingInvestors(false); }).catch(() => setLoadingInvestors(false)); }} className="text-white/30 hover:text-white transition-colors">
                      <RefreshCw size={13} className={loadingInvestors ? "animate-spin" : ""} />
                    </button>
                  </div>
                </div>

                {loadingInvestors && <p className="text-white/30 text-sm text-center py-8">{t("admin_loading")}</p>}
                {!loadingInvestors && investors && investors.count === 0 && (
                  <p className="text-white/30 text-sm text-center py-8">{t("admin_no_portfolios")}</p>
                )}
                {!loadingInvestors && investors && investors.count > 0 && (
                  <div className="overflow-x-auto">
                    {/* Header */}
                    <div className="grid grid-cols-[1.4fr_1fr_72px_80px_80px_96px_72px_60px_24px] gap-1 px-2 mb-1 min-w-[680px]">
                      {[t("admin_th_wallet"),t("admin_th_index"),t("admin_th_entry"),t("admin_th_deposited"),t("admin_th_current"),"P&L",t("admin_th_shares"),"% Pool",""].map(h => (
                        <span key={h} className="text-xs text-white/25 uppercase tracking-wider text-right first:text-left [&:nth-child(2)]:text-left">{h}</span>
                      ))}
                    </div>
                    {/* Rows */}
                    <div className="space-y-1 min-w-[680px]">
                      {investors.portfolios.map((inv) => {
                        const pPos = inv.pnl_pct > 0, pNeg = inv.pnl_pct < 0;
                        const isFounder = inv.wallet_address.toLowerCase() === session?.address.toLowerCase();
                        const entryDate = inv.deposit_date ? new Date(inv.deposit_date).toLocaleDateString("pt-BR", { day: "2-digit", month: "2-digit", year: "2-digit" }) : "—";
                        return (
                          <div
                            key={inv.id}
                            onClick={() => setSelectedInvestor(inv)}
                            className={`grid grid-cols-[1.4fr_1fr_72px_80px_80px_96px_72px_60px_24px] gap-1 items-center p-2.5 rounded-lg cursor-pointer transition-colors ${isFounder ? "bg-purple-500/8 border border-purple-500/15 hover:bg-purple-500/12" : "bg-white/3 border border-white/5 hover:bg-white/6"}`}
                          >
                            <div className="flex items-center gap-1.5 min-w-0">
                              <span className="text-white/60 font-mono text-xs truncate">{inv.wallet_address.slice(0,8)}…{inv.wallet_address.slice(-4)}</span>
                              {isFounder && <span className="shrink-0 text-xs px-1 py-px rounded bg-purple-500/20 text-purple-300 border border-purple-500/20">founder</span>}
                              {inv.is_pro && !isFounder && <span className="shrink-0 text-xs px-1 py-px rounded bg-brand-blue/20 text-brand-blue border border-brand-blue/20">pro</span>}
                            </div>
                            <span className="text-white/60 text-xs truncate">{inv.index_name}</span>
                            <span className="text-white/40 text-xs text-right font-mono">{entryDate}</span>
                            <span className="text-white/50 text-xs text-right font-mono">${inv.deposited_usd.toFixed(2)}</span>
                            <span className="text-white text-xs text-right font-mono font-semibold">${inv.current_value_usd.toFixed(2)}</span>
                            <div className="text-right">
                              <div className={`text-xs font-medium flex items-center justify-end gap-0.5 ${pPos ? "text-green-400" : pNeg ? "text-red-400" : "text-white/40"}`}>
                                {pPos ? <TrendingUp size={10} /> : pNeg ? <TrendingDown size={10} /> : null}
                                {pPos ? "+" : ""}{inv.pnl_pct.toFixed(2)}%
                              </div>
                              <div className={`text-xs font-mono ${pPos ? "text-green-400/60" : pNeg ? "text-red-400/60" : "text-white/25"}`}>{pPos ? "+" : ""}{inv.pnl_usd >= 0 ? "" : "-"}${Math.abs(inv.pnl_usd).toFixed(2)}</div>
                            </div>
                            <span className="text-white/35 text-xs text-right font-mono">{inv.shares.toFixed(2)}</span>
                            <span className="text-brand-blue/80 text-xs text-right font-mono font-semibold">{inv.pool_share_pct.toFixed(1)}%</span>
                            <ChevronRight size={13} className="text-white/20 justify-self-end" />
                          </div>
                        );
                      })}
                    </div>

                    {/* Totais */}
                    <div className="grid grid-cols-[1.4fr_1fr_72px_80px_80px_96px_72px_60px_24px] gap-1 items-center px-2.5 py-2 mt-2 border-t border-white/10 min-w-[680px]">
                      <span className="text-white/50 text-xs font-semibold col-span-3">Total ({investors.count} {investors.count === 1 ? t("admin_investments_one") : t("admin_investments_many")})</span>
                      <span className="text-white/50 text-xs text-right font-mono">${investors.total_deposited_usd.toFixed(2)}</span>
                      <span className="text-white font-bold text-xs text-right font-mono">${investors.total_current_usd.toFixed(2)}</span>
                      <span className={`text-xs font-bold text-right ${investors.total_pnl_pct > 0 ? "text-green-400" : investors.total_pnl_pct < 0 ? "text-red-400" : "text-white/40"}`}>
                        {investors.total_pnl_pct > 0 ? "+" : ""}{investors.total_pnl_pct.toFixed(2)}%
                      </span>
                      <span className="col-span-3" />
                    </div>

                    {/* Paginação */}
                    {investors.total_pages > 1 && (
                      <div className="flex items-center justify-between mt-4 pt-3 border-t border-white/8">
                        <span className="text-white/30 text-xs">
                          {((investors.page - 1) * investors.per_page) + 1}–{Math.min(investors.page * investors.per_page, investors.count)} {t("admin_of")} {investors.count}
                        </span>
                        <div className="flex items-center gap-1">
                          <button
                            disabled={investors.page <= 1}
                            onClick={() => { const p = investorPage - 1; setInvestorPage(p); setLoadingInvestors(true); adminApi.getInvestors(session!.address, session!.message, session!.signature, networkMode, p, investorPerPage).then(d => { setInvestors(d ?? null); setLoadingInvestors(false); }).catch(() => setLoadingInvestors(false)); }}
                            className="px-2 py-1 rounded text-xs bg-white/5 border border-white/10 text-white/50 hover:text-white disabled:opacity-30 disabled:cursor-not-allowed"
                          >←</button>
                          <span className="text-white/40 text-xs px-2">{investors.page} / {investors.total_pages}</span>
                          <button
                            disabled={investors.page >= investors.total_pages}
                            onClick={() => { const p = investorPage + 1; setInvestorPage(p); setLoadingInvestors(true); adminApi.getInvestors(session!.address, session!.message, session!.signature, networkMode, p, investorPerPage).then(d => { setInvestors(d ?? null); setLoadingInvestors(false); }).catch(() => setLoadingInvestors(false)); }}
                            className="px-2 py-1 rounded text-xs bg-white/5 border border-white/10 text-white/50 hover:text-white disabled:opacity-30 disabled:cursor-not-allowed"
                          >→</button>
                        </div>
                      </div>
                    )}
                  </div>
                )}
              </div>

              {/* ─── POPUP DETALHE DO INVESTIDOR ─── */}
              {selectedInvestor && (
                <div className="fixed inset-0 z-50 flex items-end sm:items-center justify-center p-0 sm:p-4 bg-black/70 backdrop-blur-sm" onClick={() => setSelectedInvestor(null)}>
                  <div className="w-full sm:max-w-lg max-h-[90vh] overflow-y-auto rounded-t-2xl sm:rounded-2xl bg-[#111118] border border-white/10 shadow-2xl" onClick={e => e.stopPropagation()}>
                    {/* Header popup */}
                    <div className="flex items-start justify-between p-5 border-b border-white/8">
                      <div>
                        <div className="flex items-center gap-2 flex-wrap">
                          <span className="text-white font-mono text-sm font-semibold">
                            {selectedInvestor.wallet_address.slice(0,10)}…{selectedInvestor.wallet_address.slice(-6)}
                          </span>
                          {selectedInvestor.wallet_address.toLowerCase() === session?.address.toLowerCase() && (
                            <span className="text-xs px-1.5 py-0.5 rounded bg-purple-500/20 text-purple-300 border border-purple-500/20">founder</span>
                          )}
                          {selectedInvestor.is_pro && <span className="text-xs px-1.5 py-0.5 rounded bg-brand-blue/20 text-brand-blue border border-brand-blue/20">pro</span>}
                          <span className={`text-xs px-1.5 py-0.5 rounded border font-medium ${isMainnet ? "text-green-400 bg-green-500/10 border-green-500/20" : "text-yellow-400 bg-yellow-500/10 border-yellow-500/20"}`}>{isMainnet ? "Mainnet" : "Testnet"}</span>
                        </div>
                        <p className="text-white/50 text-xs mt-1">
                          {selectedInvestor.index_name} · {selectedInvestor.deposit_date ? new Date(selectedInvestor.deposit_date).toLocaleDateString("pt-BR", { day: "2-digit", month: "2-digit", year: "2-digit", hour: "2-digit", minute: "2-digit" }) : "—"}
                        </p>
                      </div>
                      <button onClick={() => setSelectedInvestor(null)} className="text-white/30 hover:text-white text-xl leading-none ml-4">×</button>
                    </div>

                    <div className="p-5 space-y-5">
                      {/* Resumo financeiro + status */}
                      <div className="grid grid-cols-3 gap-3">
                        {[
                          { label: t("admin_th_deposited"), value: `$${selectedInvestor.deposited_usd.toFixed(2)}`, cls: "text-white" },
                          { label: t("admin_investor_current"), value: `$${selectedInvestor.current_value_usd.toFixed(2)}`, cls: "text-white font-bold" },
                          { label: "P&L", value: `${selectedInvestor.pnl_pct >= 0 ? "+" : ""}${selectedInvestor.pnl_pct.toFixed(2)}%`, cls: selectedInvestor.pnl_pct > 0 ? "text-green-400 font-bold" : selectedInvestor.pnl_pct < 0 ? "text-red-400 font-bold" : "text-white/40" },
                        ].map(i => (
                          <div key={i.label} className="bg-white/4 rounded-xl p-3 text-center">
                            <p className="text-white/40 text-xs mb-1">{i.label}</p>
                            <p className={`text-sm font-mono ${i.cls}`}>{i.value}</p>
                          </div>
                        ))}
                      </div>

                      {/* Info do depósito: TX, status, NAV */}
                      <div className="flex items-center justify-between p-3 rounded-xl bg-white/3 border border-white/8">
                        <div className="space-y-1">
                          <div className="flex items-center gap-2">
                            <span className={`text-xs px-1.5 py-0.5 rounded border ${selectedInvestor.buy_confirmed ? "text-green-400 bg-green-500/10 border-green-500/20" : "text-amber-400 bg-amber-500/10 border-amber-500/20"}`}>
                              {selectedInvestor.buy_confirmed ? t("admin_buys_ok") : t("admin_buys_pending")}
                            </span>
                            <span className="text-white/30 text-xs font-mono">{t("admin_entry_nav")}{selectedInvestor.nav_at_buy.toFixed(6)}</span>
                          </div>
                          {selectedInvestor.tx_hash && (
                            <a href={`${isMainnet ? "https://basescan.org" : "https://sepolia.basescan.org"}/tx/${selectedInvestor.tx_hash}`} target="_blank" rel="noopener noreferrer" className="text-white/25 hover:text-white/60 text-xs font-mono transition-colors">
                              TX: {selectedInvestor.tx_hash.slice(0, 14)}… ↗
                            </a>
                          )}
                        </div>
                        <div className="text-right">
                          <p className="text-white/40 text-xs">HWM</p>
                          <p className="text-white/60 font-mono text-xs">${selectedInvestor.high_water_mark.toFixed(2)}</p>
                        </div>
                      </div>

                      {/* Cotas e participação no pool */}
                      <div className="bg-brand-blue/8 border border-brand-blue/20 rounded-xl p-4">
                        <p className="text-brand-blue/70 text-xs uppercase tracking-wider mb-2 font-semibold">{t("admin_pool_participation")}</p>
                        <div className="grid grid-cols-2 gap-3">
                          <div>
                            <p className="text-white/40 text-xs">{t("admin_shares_this")}</p>
                            <p className="text-white font-mono font-semibold">{selectedInvestor.shares.toFixed(4)}</p>
                          </div>
                          <div>
                            <p className="text-white/40 text-xs">{t("admin_pool_pct")}</p>
                            <p className="text-brand-blue font-mono font-bold text-lg">{selectedInvestor.pool_share_pct.toFixed(2)}%</p>
                          </div>
                        </div>
                      </div>

                      {/* Composição da cesta com % de cada token no pool SoDEX */}
                      {selectedInvestor.basket.length > 0 && (
                        <div>
                          <p className="text-white/50 text-xs uppercase tracking-wider mb-2 font-semibold">{t("admin_basket_comp")}</p>
                          <div className="space-y-2">
                            {selectedInvestor.basket.map(token => {
                              const ch7 = token.change_7d;
                              // % deste investimento no total de cada token no SoDEX
                              const sodexPos = portfolio?.positions.find((p: PortfolioPosition) =>
                                p.asset.replace(/\./g, "").toLowerCase() === token.symbol.replace(/\./g, "").toLowerCase()
                              );
                              const tokenPoolPct = sodexPos && sodexPos.amount > 0
                                ? (token.est_qty / sodexPos.amount * 100)
                                : null;
                              return (
                                <div key={token.symbol} className="flex items-center gap-3 p-2.5 rounded-lg bg-white/4">
                                  <div className="w-14 shrink-0">
                                    <div className="w-full bg-white/5 rounded-full h-1 mb-1">
                                      <div className="bg-brand-blue h-1 rounded-full" style={{ width: `${token.weight}%` }} />
                                    </div>
                                    <span className="text-white/30 text-xs">{token.weight}%</span>
                                  </div>
                                  <div className="flex-1 min-w-0">
                                    <span className="text-white text-xs font-mono font-semibold">{token.symbol}</span>
                                    <p className="text-white/30 text-xs font-mono">{token.est_qty.toFixed(4)} @ ${token.price < 1 ? token.price.toFixed(4) : token.price.toFixed(2)}</p>
                                    {tokenPoolPct !== null && (
                                      <p className="text-brand-blue/60 text-xs font-mono">{tokenPoolPct.toFixed(1)}% {t("admin_sodex_pool_pct")}</p>
                                    )}
                                  </div>
                                  <div className="text-right shrink-0">
                                    <p className="text-white text-xs font-mono font-semibold">${token.est_usd.toFixed(2)}</p>
                                    {ch7 !== 0 && <p className={`text-xs font-mono ${ch7 > 0 ? "text-green-400/70" : "text-red-400/70"}`}>{ch7 > 0 ? "+" : ""}{ch7.toFixed(1)}% 7d</p>}
                                  </div>
                                </div>
                              );
                            })}
                          </div>
                        </div>
                      )}
                    </div>
                  </div>
                </div>
              )}

            </>
          )}

          {/* ══════════════════════ INVESTORS ═════════════════════════════ */}
          {activeTab === "investors" && (
            <>
              <div className="flex items-center justify-between flex-wrap gap-3">
                <h2 className="font-semibold text-white flex items-center gap-2 text-sm">
                  <Users size={14} className="text-brand-blue" /> {t("admin_movements_title")}
                </h2>
                <div className="flex items-center gap-1">
                  {([
                    { key: "all",        label: t("admin_filter_all") },
                    { key: "deposit",    label: t("admin_filter_deposit") },
                    { key: "refund",     label: t("admin_filter_refund") },
                    { key: "withdrawal", label: t("admin_filter_withdrawal") },
                  ] as const).map(f => (
                    <button key={f.key} onClick={() => setMovFilter(f.key)} className={`px-2.5 py-1 rounded-lg text-xs transition-all ${movFilter === f.key ? "bg-white/10 text-white" : "text-white/30 hover:text-white/60"}`}>
                      {f.label}
                    </button>
                  ))}
                  <button onClick={() => { setLoadingMovements(true); adminApi.getMovements(session.address, session.message, session.signature, networkMode).then(d => setMovements((d?.movements ?? []) as Movement[])).catch(() => {}).finally(() => setLoadingMovements(false)); }} className="text-white/30 hover:text-white ml-1 transition-colors">
                    <RefreshCw size={12} className={loadingMovements ? "animate-spin" : ""} />
                  </button>
                </div>
              </div>

              {(() => {
                const filtered = movements.filter(m => movFilter === "all" || m.type === movFilter);
                if (!loadingMovements && filtered.length === 0) return (
                  <div className="card text-center py-10">
                    <p className="text-white/30 text-sm">{t("admin_no_movements_filter")} &ldquo;{movFilter}&rdquo;</p>
                  </div>
                );
                return (
                  <div className="space-y-2">
                    {filtered.map(m => {
                      const typeColor: Record<string, string> = { deposit: "text-green-400 bg-green-500/10 border-green-500/20", refund: "text-orange-400 bg-orange-500/10 border-orange-500/20", withdrawal: "text-red-400 bg-red-500/10 border-red-500/20", manual: "text-blue-400 bg-blue-500/10 border-blue-500/20", other: "text-white/40 bg-white/5 border-white/10" };
                      const isExp = expandedMovement === m.id;
                      return (
                        <div key={m.id} className="rounded-lg bg-white/3 overflow-hidden border border-white/5">
                          <button onClick={() => setExpandedMovement(isExp ? null : m.id)} className="w-full flex items-center gap-3 p-3 hover:bg-white/5 transition-colors text-left">
                            <span className={`text-xs px-2 py-0.5 rounded border font-medium shrink-0 ${typeColor[m.type] ?? "text-white/40 bg-white/5 border-white/10"}`}>{m.type}</span>
                            <span className="text-white font-medium text-sm">${m.amount_usd.toFixed(2)}</span>
                            <span className="text-xs text-white/40 font-mono truncate flex-1">{m.wallet.slice(0, 8)}…{m.wallet.slice(-4)}</span>
                            {m.wallet.toLowerCase() === session.address.toLowerCase() && (
                              <span className="text-xs px-1.5 py-0.5 rounded border font-medium shrink-0 text-purple-300 bg-purple-500/10 border-purple-500/20">Founder</span>
                            )}
                            {m.type === "refund" && (
                              <span className={`text-xs shrink-0 ${m.refund_ok === true ? "text-green-400" : m.refund_ok === false ? "text-red-400" : "text-yellow-400"}`}>
                                {m.refund_ok === true ? t("admin_refund_status_ok") : m.refund_ok === false ? t("admin_refund_status_failed") : t("admin_refund_status_pending")}
                              </span>
                            )}
                            <span className="text-xs text-white/25 shrink-0">{new Date(m.timestamp).toLocaleDateString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}</span>
                          </button>
                          {isExp && (
                            <div className="px-4 pb-3 space-y-1.5 border-t border-white/5 pt-2">
                              {m.index_id && <p className="text-xs text-white/40"><span className="text-white/25">{t("admin_index_overview")}: </span>{m.index_id}</p>}
                              {m.tx_hash && (
                                <p className="text-xs text-white/40 flex items-center gap-1.5">
                                  <span className="text-white/25">Tx: </span>
                                  <span className="font-mono truncate">{m.tx_hash.slice(0, 20)}…</span>
                                  {m.basescan && <a href={m.basescan} target="_blank" rel="noopener noreferrer" className="text-brand-blue hover:text-blue-300 shrink-0">↗</a>}
                                </p>
                              )}
                              {m.refund_tx && (
                                <p className="text-xs text-white/40 flex items-center gap-1.5">
                                  <span className="text-white/25">Refund Tx: </span>
                                  <span className="font-mono truncate">{m.refund_tx.slice(0, 20)}…</span>
                                  {m.refund_basescan && <a href={m.refund_basescan} target="_blank" rel="noopener noreferrer" className="text-brand-blue hover:text-blue-300 shrink-0">↗</a>}
                                </p>
                              )}
                              {m.description && <p className="text-xs text-white/30 leading-relaxed">{m.description}</p>}
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </div>
                );
              })()}
            </>
          )}

          {/* ══════════════════════ TRADES ════════════════════════════════ */}
          {activeTab === "trades" && (
            <>
              <div className="flex items-center justify-between">
                <h2 className="font-semibold text-white flex items-center gap-2 text-sm">
                  <ArrowRightLeft size={14} className="text-brand-blue" /> {t("admin_trades_sodex_title")}
                  <span className={`text-xs px-2 py-0.5 rounded font-mono ${networkMode === "testnet" ? "bg-amber-500/20 text-amber-400" : "bg-blue-500/20 text-blue-400"}`}>
                    {networkMode === "testnet" ? "testnet-gw" : "mainnet-gw"}
                  </span>
                </h2>
                <button onClick={() => { setLoadingTrades(true); setTrades([]); adminApi.getTrades(session.address, session.message, session.signature, 50, networkMode).then(d => { setTrades(Array.isArray(d) ? d : []); setLoadingTrades(false); }).catch(() => setLoadingTrades(false)); }} className="text-white/30 hover:text-white transition-colors">
                  <RefreshCw size={13} className={loadingTrades ? "animate-spin" : ""} />
                </button>
              </div>
              {loadingTrades && trades.length === 0 && <p className="text-white/30 text-sm text-center py-6">{t("admin_loading")}</p>}
              {!loadingTrades && trades.length === 0 && (
                <div className="card text-center py-10">
                  <p className="text-white/30 text-sm">{t("admin_no_trades")}</p>
                  <p className="text-white/20 text-xs mt-1">{t("admin_no_trades_sub2")}</p>
                </div>
              )}
              {trades.length > 0 && (
                <div className="card overflow-x-auto">
                  <table className="w-full text-xs">
                    <thead>
                      <tr className="text-white/20 border-b border-white/5">
                        <th className="text-left pb-2 font-normal pr-4">{t("admin_investor_col")}</th>
                        <th className="text-left pb-2 font-normal">{t("admin_trade_symbol")}</th>
                        <th className="text-left pb-2 font-normal">{t("admin_trade_side")}</th>
                        <th className="text-right pb-2 font-normal">{t("admin_trade_qty")}</th>
                        <th className="text-right pb-2 font-normal">{t("admin_trade_price")}</th>
                        <th className="text-right pb-2 font-normal">{t("admin_usd_col")}</th>
                        <th className="text-right pb-2 font-normal">{t("admin_trade_status")}</th>
                        <th className="text-right pb-2 font-normal">{t("admin_trade_time")}</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-white/3">
                      {trades.map((tr, i) => {
                        const wallet = String(tr.investor_wallet ?? "");
                        const shortWallet = wallet.length >= 10 ? `${wallet.slice(0, 6)}…${wallet.slice(-4)}` : wallet;
                        const isFounder = wallet.toLowerCase() === "0x1a3ade798b60bd6e99ff3d84367cc7913115031c";
                        return (
                          <tr key={tr.id ?? i}>
                            <td className="py-2 pr-4">
                              <div className="flex items-center gap-1.5" title={wallet || undefined}>
                                <span className="font-mono text-white/50 cursor-default">{shortWallet || "—"}</span>
                                {isFounder && (
                                  <span className="text-xs px-1 py-0.5 rounded border font-medium text-purple-300 bg-purple-500/10 border-purple-500/20">Founder</span>
                                )}
                              </div>
                            </td>
                            <td className="py-2 font-mono text-white">{String(tr.symbol ?? tr.s ?? "—")}</td>
                            <td className={`py-2 font-medium ${String(tr.side).toUpperCase() === "BUY" ? "text-green-400" : "text-red-400"}`}>{String(tr.side ?? "—").toUpperCase()}</td>
                            <td className="py-2 text-right text-white/60">{Number(tr.quantity ?? tr.qty ?? 0).toFixed(6)}</td>
                            <td className="py-2 text-right text-white/60">{tr.price ? `$${Number(tr.price).toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 4})}` : "—"}</td>
                            <td className="py-2 text-right text-white font-medium">{tr.usd_value ? `$${Number(tr.usd_value).toFixed(2)}` : "—"}</td>
                            <td className="py-2 text-right">
                              {tr.is_simulated ? (
                                <span className={`px-1.5 py-0.5 rounded text-xs border ${String(tr.status) === "skipped" ? "text-white/30 bg-white/5 border-white/10" : "text-amber-400 bg-amber-500/10 border-amber-500/20"}`}>
                                  {String(tr.status)}
                                </span>
                              ) : (
                                <span className={`px-1.5 py-0.5 rounded text-xs ${String(tr.status) === "placed" || String(tr.status) === "filled" ? "text-green-400" : "text-white/40"}`}>{String(tr.status ?? "—")}</span>
                              )}
                            </td>
                            <td className="py-2 text-right text-white/30">{(tr.timestamp || tr.created_at) ? timeAgo(String(tr.timestamp ?? tr.created_at)) : "—"}</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              )}
            </>
          )}

          {/* ══════════════════════ AGENTS ════════════════════════════════ */}
          {activeTab === "agents" && (
            <>
              <h2 className="font-semibold text-white flex items-center gap-2 text-sm">
                <Cpu size={14} className="text-brand-blue" /> {t("admin_agents_controls_title")}
              </h2>

              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div className="card">
                  <div className="flex items-center gap-2 mb-1">
                    <div className="w-2 h-2 rounded-full bg-green-400 shrink-0" />
                    <h3 className="font-semibold text-white text-sm">Scout</h3>
                    <span className="text-white/25 text-xs ml-auto">{t("admin_scout_schedule")}</span>
                  </div>
                  <p className="text-white/35 text-xs mb-4 leading-relaxed">{t("admin_scout_agent_desc")}</p>
                  <button onClick={handleRunScout} disabled={runningScout} className="w-full flex items-center justify-center gap-2 px-4 py-2 rounded-lg bg-white/5 border border-white/10 text-white/70 hover:text-white hover:bg-white/8 text-sm transition-all disabled:opacity-40">
                    <Play size={13} className={runningScout ? "animate-pulse text-green-400" : ""} />
                    {runningScout ? t("admin_running_scout") : t("admin_run_scout")}
                  </button>
                  {scoutMsg && <p className={`text-xs mt-2 ${scoutMsg.startsWith("Erro") ? "text-red-400" : "text-green-400"}`}>{scoutMsg}</p>}
                </div>

                <div className="card">
                  <div className="flex items-center gap-2 mb-1">
                    <div className="w-2 h-2 rounded-full bg-green-400 shrink-0" />
                    <h3 className="font-semibold text-white text-sm">Rebalancer</h3>
                    <span className="text-white/25 text-xs ml-auto">{t("admin_rebal_schedule")}</span>
                  </div>
                  <p className="text-white/35 text-xs mb-4 leading-relaxed">{t("admin_rebalancer_agent_desc")}</p>
                  <button onClick={handleRunRebalancer} disabled={runningRebalancer} className="w-full flex items-center justify-center gap-2 px-4 py-2 rounded-lg bg-white/5 border border-white/10 text-white/70 hover:text-white hover:bg-white/8 text-sm transition-all disabled:opacity-40">
                    <Play size={13} className={runningRebalancer ? "animate-pulse text-brand-blue" : ""} />
                    {runningRebalancer ? t("admin_running_rebalancer") : t("admin_run_rebalancer_now")}
                  </button>
                  {rebalancerMsg && <p className={`text-xs mt-2 ${rebalancerMsg.startsWith("Erro") ? "text-red-400" : "text-brand-blue"}`}>{rebalancerMsg}</p>}
                </div>

                <div className="card">
                  <div className="flex items-center gap-2 mb-1">
                    <div className="w-2 h-2 rounded-full bg-green-400 shrink-0" />
                    <h3 className="font-semibold text-white text-sm">NAV Updater</h3>
                    <span className="text-white/25 text-xs ml-auto">{t("admin_nav_schedule")}</span>
                  </div>
                  <p className="text-white/35 text-xs mb-4 leading-relaxed">{t("admin_nav_updater_agent_desc")}</p>
                  <button onClick={handleRunNavUpdate} disabled={runningNavUpdate} className="w-full flex items-center justify-center gap-2 px-4 py-2 rounded-lg bg-white/5 border border-white/10 text-white/70 hover:text-white hover:bg-white/8 text-sm transition-all disabled:opacity-40">
                    <RefreshCw size={13} className={runningNavUpdate ? "animate-spin text-amber-400" : ""} />
                    {runningNavUpdate ? t("admin_running_nav") : t("admin_run_nav_now")}
                  </button>
                  {navUpdateMsg && <p className={`text-xs mt-2 ${navUpdateMsg.startsWith("Erro") ? "text-red-400" : "text-green-400"}`}>{navUpdateMsg}</p>}
                </div>

                <div className="card">
                  <div className="flex items-center gap-2 mb-1">
                    <div className="w-2 h-2 rounded-full bg-blue-400 shrink-0" />
                    <h3 className="font-semibold text-white text-sm">{t("admin_report_title")}</h3>
                    <span className="text-white/25 text-xs ml-auto">{t("admin_on_demand")}</span>
                  </div>
                  <p className="text-white/35 text-xs mb-4 leading-relaxed">{t("admin_report_agent_desc")}</p>
                  <button onClick={handleReport} disabled={loadingReport} className="w-full flex items-center justify-center gap-2 px-4 py-2 rounded-lg bg-white/5 border border-white/10 text-white/70 hover:text-white hover:bg-white/8 text-sm transition-all disabled:opacity-40">
                    <BarChart3 size={13} className={loadingReport ? "animate-pulse text-blue-400" : ""} />
                    {loadingReport ? t("admin_generating") : t("admin_generate_report")}
                  </button>
                </div>
              </div>

              {showReport && report && (() => {
                const p = report.platform as Record<string, unknown>;
                const indexes = report.indexes as Array<Record<string, unknown>>;
                const rProps = report.proposals as Record<string, unknown>;
                const byStatus = rProps.by_status as Record<string, number> ?? {};
                return (
                  <div className="card">
                    <div className="flex items-center justify-between mb-4">
                      <div className="flex items-center gap-2">
                        <BarChart3 size={15} className="text-brand-blue" />
                        <h3 className="font-semibold text-white text-sm">{t("admin_report_title")}</h3>
                        <span className="text-white/20 text-xs">{String(report.generated_at ?? "").replace("T", " ").slice(0, 19)} UTC</span>
                      </div>
                      <button onClick={() => setShowReport(false)} className="text-white/30 hover:text-white/60 text-xs border border-white/10 rounded px-2 py-1">{t("admin_report_close")}</button>
                    </div>
                    <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-5">
                      {[
                        { label: t("admin_total_aum"),             value: fmtUSD(Number(p.total_aum_usd ?? 0)) },
                        { label: t("admin_subscribers"),            value: String(p.total_investors ?? 0) },
                        { label: t("admin_pro_label"),              value: String(p.pro_investors ?? 0) },
                        { label: t("admin_report_proposals_total"), value: String(rProps.total ?? 0) },
                      ].map(s => (
                        <div key={s.label} className="bg-white/3 rounded-xl p-3 border border-white/5">
                          <p className="text-white/40 text-xs mb-1">{s.label}</p>
                          <p className="text-white font-bold">{s.value}</p>
                        </div>
                      ))}
                    </div>
                    <div className="mb-4">
                      <p className="text-white/30 text-xs mb-2 uppercase tracking-wide">{t("admin_report_proposals_status")}</p>
                      <div className="flex gap-2 flex-wrap">
                        {Object.entries(byStatus).map(([k, v]) => (
                          <span key={k} className={`text-xs px-2 py-1 rounded border ${STATUS_BADGE[k] ?? "border-white/10 text-white/40"}`}>{k}: {v}</span>
                        ))}
                        {Object.keys(byStatus).length === 0 && <p className="text-white/30 text-xs">{t("admin_report_no_proposals")}</p>}
                      </div>
                    </div>
                    <div>
                      <p className="text-white/30 text-xs mb-2 uppercase tracking-wide">{t("admin_report_indexes")}</p>
                      <div className="space-y-2">
                        {indexes.map(idx => (
                          <div key={String(idx.id)} className="flex items-center justify-between p-2.5 rounded-lg bg-white/3">
                            <div>
                              <p className="text-sm text-white font-medium">{String(idx.name)}</p>
                              <p className="text-xs text-white/30">{String(idx.subscriber_count ?? 0)} · NAV ${Number(idx.nav_usd ?? 1).toFixed(4)}</p>
                            </div>
                            <div className="text-right">
                              <p className="text-sm text-white">{fmtUSD(Number(idx.aum_usd ?? 0))}</p>
                              <p className={`text-xs ${Number(idx.return_30d_pct) > 0 ? "text-green-400" : Number(idx.return_30d_pct) < 0 ? "text-red-400" : "text-white/50"}`}>
                                {Number(idx.return_30d_pct) > 0 ? "+" : ""}{Number(idx.return_30d_pct).toFixed(1)}% 30d
                              </p>
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>
                  </div>
                );
              })()}

              <div className="card">
                <h3 className="font-semibold text-white mb-3 flex items-center gap-2 text-sm">
                  <Clock size={14} className="text-white/40" /> {t("admin_scheduler_title")}
                </h3>
                <div className="space-y-2">
                  {[
                    ["Scout",            t("admin_scout_schedule"),   t("admin_scout_agent_desc").split(".")[0]],
                    ["Rebalancer",       t("admin_rebal_schedule"),   t("admin_rebalancer_agent_desc").split(".")[0]],
                    ["NAV Updater",      t("admin_nav_schedule"),     t("admin_nav_updater_agent_desc").split(".")[0]],
                    ["Deposit Monitor",  t("admin_deposit_schedule"), "USDC on Base"],
                    ["Fee Manager",      "1st/month 08:00 UTC",       "2%/yr mgmt + 20% perf HWM"],
                  ].map(([name, freq, desc]) => (
                    <div key={name} className="flex items-start gap-3 py-2 border-b border-white/5 last:border-0">
                      <div className="w-2 h-2 rounded-full bg-green-400 mt-1.5 shrink-0" />
                      <div className="flex-1">
                        <div className="flex items-center gap-2">
                          <span className="text-white text-xs font-medium">{name}</span>
                          <span className="text-white/30 text-xs">{freq}</span>
                        </div>
                        <p className="text-white/20 text-xs mt-0.5">{desc}</p>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            </>
          )}

          {/* ── FEES TAB ──────────────────────────────────────────────────── */}
          {activeTab === "fees" && (
            <div className="space-y-4">
              <div className="flex items-center justify-between">
                <h2 className="font-semibold text-white flex items-center gap-2 text-sm">
                  <DollarSign size={14} className="text-brand-blue" /> Gestão de Taxas
                </h2>
                <button
                  onClick={async () => {
                    if (!session) return;
                    setFeesLoading(true);
                    try {
                      const d = await feesApi.getSummary(session.address, session.message, session.signature);
                      setFeesSummary(d);
                    } catch(e) {}
                    setFeesLoading(false);
                  }}
                  className="text-white/30 hover:text-white transition-colors flex items-center gap-1 text-xs"
                >
                  <RefreshCw size={13} className={feesLoading ? "animate-spin" : ""} />
                  {feesSummary ? "Atualizar" : "Carregar"}
                </button>
              </div>

              {!feesSummary && !feesLoading && (
                <div className="text-white/30 text-sm text-center py-8">Clique em Carregar para ver o resumo de taxas.</div>
              )}
              {feesLoading && (
                <div className="text-white/30 text-sm text-center py-8">Carregando...</div>
              )}

              {feesSummary && (
                <>
                  <div className="grid grid-cols-3 gap-3">
                    <div className="bg-white/5 rounded-lg p-4 border border-white/10">
                      <div className="text-white/40 text-xs mb-1">Total Coletado</div>
                      <div className="text-white font-mono text-lg">${feesSummary.collected.grand_total_usd.toFixed(4)}</div>
                      <div className="text-white/30 text-xs mt-1">{feesSummary.collected.count} cobranças</div>
                    </div>
                    <div className="bg-white/5 rounded-lg p-4 border border-white/10">
                      <div className="text-white/40 text-xs mb-1">Management Fees</div>
                      <div className="text-green-400 font-mono text-lg">${feesSummary.collected.management_total_usd.toFixed(4)}</div>
                      <div className="text-white/30 text-xs mt-1">{feesSummary.config.management_fee_annual_pct}%/ano</div>
                    </div>
                    <div className="bg-white/5 rounded-lg p-4 border border-white/10">
                      <div className="text-white/40 text-xs mb-1">Performance Fees</div>
                      <div className="text-purple-400 font-mono text-lg">${feesSummary.collected.performance_total_usd.toFixed(4)}</div>
                      <div className="text-white/30 text-xs mt-1">{feesSummary.config.performance_fee_pct}% acima do HWM</div>
                    </div>
                  </div>

                  <div className="bg-white/5 rounded-lg p-4 border border-white/10">
                    <div className="text-white/60 text-xs font-medium mb-3 uppercase tracking-wide">Pendente (próxima cobrança)</div>
                    <div className="space-y-2">
                      {feesSummary.pending.management.map((m: any) => (
                        <div key={m.index_id} className="flex justify-between text-sm">
                          <span className="text-white/50">{m.index_name} — management</span>
                          <span className="text-green-400 font-mono">${m.fee_amount_usd.toFixed(4)}</span>
                        </div>
                      ))}
                      {feesSummary.pending.performance.map((p: any) => (
                        <div key={p.portfolio_id} className="flex justify-between text-sm">
                          <span className="text-white/50">{p.index_name} — performance</span>
                          <span className="text-purple-400 font-mono">${p.fee_amount_usd.toFixed(4)}</span>
                        </div>
                      ))}
                      {feesSummary.pending.management.length === 0 && feesSummary.pending.performance.length === 0 && (
                        <div className="text-white/30 text-sm">Nenhuma taxa pendente agora.</div>
                      )}
                    </div>
                    <div className="mt-3 flex items-center justify-between">
                      <span className="text-white/30 text-xs">{feesSummary.config.collection_schedule}</span>
                      <div className="flex gap-2">
                        <button
                          onClick={async () => {
                            if (!session) return;
                            const r = await feesApi.collect(session.address, session.message, session.signature, true);
                            alert("Simulação: management $" + r.management_fees.total_usd.toFixed(4) + " + performance $" + r.performance_fees.total_usd.toFixed(4));
                          }}
                          className="text-xs px-3 py-1.5 rounded border border-white/20 text-white/50 hover:text-white hover:border-white/40 transition-colors"
                        >
                          Simular
                        </button>
                        <button
                          onClick={async () => {
                            if (!session) return;
                            if (!confirm("Cobrar taxas agora?")) return;
                            const r = await feesApi.collect(session.address, session.message, session.signature, false);
                            alert("Cobrado: management $" + r.management_fees.total_usd.toFixed(4) + " + performance $" + r.performance_fees.total_usd.toFixed(4));
                            const d = await feesApi.getSummary(session.address, session.message, session.signature);
                            setFeesSummary(d);
                          }}
                          className="text-xs px-3 py-1.5 rounded border border-green-500/40 text-green-400 hover:bg-green-500/10 transition-colors"
                        >
                          Cobrar Agora
                        </button>
                      </div>
                    </div>
                  </div>

                  <div className="bg-white/5 rounded-lg border border-white/10 overflow-hidden">
                    <div className="p-4 border-b border-white/10 text-white/60 text-xs font-medium uppercase tracking-wide">Histórico de Cobranças</div>
                    <div className="overflow-x-auto">
                      <table className="w-full text-xs">
                        <thead className="text-white/40 border-b border-white/5">
                          <tr>
                            <th className="text-left p-3">Data</th>
                            <th className="text-left p-3">Tipo</th>
                            <th className="text-left p-3">Índice</th>
                            <th className="text-right p-3">Valor</th>
                          </tr>
                        </thead>
                        <tbody>
                          {feesSummary.collected.history.map((h: any) => (
                            <tr key={h.id} className="border-b border-white/5 hover:bg-white/3">
                              <td className="p-3 text-white/50 font-mono">{h.timestamp ? new Date(h.timestamp).toLocaleDateString("pt-BR") : "—"}</td>
                              <td className="p-3">
                                {h.type === "management_fee" ? (
                                  <span className="px-1.5 py-0.5 rounded bg-green-500/15 text-green-300 border border-green-500/20">Management</span>
                                ) : (
                                  <span className="px-1.5 py-0.5 rounded bg-purple-500/15 text-purple-300 border border-purple-500/20">Performance</span>
                                )}
                              </td>
                              <td className="p-3 text-white/60">{h.index_name}</td>
                              <td className="p-3 text-right font-mono text-white">${Number(h.fee_amount_usd).toFixed(4)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                </>
              )}
            </div>
          )}

          {/* ── ANNOUNCEMENTS TAB ─────────────────────────────────────────── */}
          {activeTab === "announcements" && (
            <>
              {/* Header */}
              <div className="flex items-center justify-between mb-4">
                <h2 className="text-white font-semibold flex items-center gap-2">
                  <Megaphone size={16} className="text-yellow-400" /> Anúncios SoDEX / Mercado
                </h2>
                <div className="flex gap-2">
                  <button
                    className="btn-secondary text-xs flex items-center gap-1"
                    disabled={annSyncing || !address}
                    onClick={async () => {
                      if (!address || !signMessageAsync) return;
                      setAnnSyncing(true);
                      try {
                        const msg = `SoSoMon Admin\nTimestamp: ${new Date().toISOString()}`;
                        const sig = await signMessageAsync({ message: msg });
                        await adminAnnouncementApi.sync(address, msg, sig);
                        const rows = await adminAnnouncementApi.getAll(address, msg, sig);
                        setAnnouncements(rows);
                      } catch (e) { console.error(e); }
                      finally { setAnnSyncing(false); }
                    }}
                  >
                    <RefreshCw size={12} className={annSyncing ? "animate-spin" : ""} />
                    {annSyncing ? "Sincronizando..." : "Sincronizar SoDEX"}
                  </button>
                  <button
                    className="btn-secondary text-xs flex items-center gap-1"
                    onClick={() => { setAnnCreateMode(true); setAnnEditId(null); }}
                  >
                    <Plus size={12} /> Novo Manual
                  </button>
                </div>
              </div>

              {/* Create form */}
              {annCreateMode && (
                <div className="card mb-4 space-y-3">
                  <h3 className="text-white text-sm font-medium">Novo Anúncio Manual</h3>
                  <input
                    className="input w-full text-xs"
                    placeholder="Título"
                    value={annNewTitle}
                    onChange={e => setAnnNewTitle(e.target.value)}
                  />
                  <textarea
                    className="input w-full text-xs"
                    rows={3}
                    placeholder="Corpo / detalhe (opcional)"
                    value={annNewBody}
                    onChange={e => setAnnNewBody(e.target.value)}
                  />
                  <div className="flex gap-2">
                    <select
                      className="input text-xs flex-1"
                      value={annNewSeverity}
                      onChange={e => setAnnNewSeverity(e.target.value)}
                    >
                      <option value="info">info</option>
                      <option value="warning">warning</option>
                      <option value="critical">critical</option>
                    </select>
                    <input
                      className="input text-xs flex-1"
                      placeholder="Símbolos (ADA,UNI,...)"
                      value={annNewSymbols}
                      onChange={e => setAnnNewSymbols(e.target.value)}
                    />
                    <input
                      type="datetime-local"
                      className="input text-xs flex-1"
                      value={annNewDeadline}
                      onChange={e => setAnnNewDeadline(e.target.value)}
                    />
                  </div>
                  <div className="flex gap-2 justify-end">
                    <button className="btn-secondary text-xs" onClick={() => setAnnCreateMode(false)}>Cancelar</button>
                    <button
                      className="btn-primary text-xs"
                      onClick={async () => {
                        if (!address || !annNewTitle || !signMessageAsync) return;
                        try {
                          const msg = `SoSoMon Admin\nTimestamp: ${new Date().toISOString()}`;
                          const sig = await signMessageAsync({ message: msg });
                          await adminAnnouncementApi.create(address, msg, sig, {
                            title: annNewTitle,
                            body: annNewBody || undefined,
                            severity: annNewSeverity,
                            affects_symbols: annNewSymbols ? annNewSymbols.split(",").map(s => s.trim()) : [],
                            action_deadline: annNewDeadline || undefined,
                            source: "manual",
                          });
                          const rows = await adminAnnouncementApi.getAll(address, msg, sig);
                          setAnnouncements(rows);
                          setAnnCreateMode(false);
                          setAnnNewTitle(""); setAnnNewBody(""); setAnnNewSeverity("info"); setAnnNewSymbols(""); setAnnNewDeadline("");
                        } catch (e) { console.error(e); }
                      }}
                    >
                      Salvar
                    </button>
                  </div>
                </div>
              )}

              {/* List */}
              <div className="card">
                {annLoading ? (
                  <div className="text-white/30 text-sm text-center py-8">Carregando…</div>
                ) : announcements.length === 0 ? (
                  <div className="text-white/30 text-sm text-center py-8">Nenhum anúncio. Clique em "Sincronizar SoDEX".</div>
                ) : (
                  <div className="space-y-2">
                    {announcements.map(ann => {
                      const severityColor = ann.severity === "critical" ? "text-red-400 border-red-500/30" :
                        ann.severity === "warning" ? "text-yellow-400 border-yellow-500/30" : "text-blue-400 border-blue-500/30";
                      const isEditing = annEditId === ann.id;
                      return (
                        <div key={ann.id} className={`border rounded-lg p-3 ${ann.is_active ? "border-white/10" : "border-white/5 opacity-50"}`}>
                          <div className="flex items-start gap-2">
                            <span className={`text-xs font-bold uppercase tracking-wider shrink-0 mt-0.5 ${severityColor}`}>
                              {ann.severity}
                            </span>
                            <div className="flex-1 min-w-0">
                              <div className="flex items-center gap-2">
                                <span className="text-white text-xs font-medium truncate">{ann.title}</span>
                                <span className="text-white/30 text-xs shrink-0">[{ann.source}]</span>
                              </div>
                              {ann.affects_symbols.length > 0 && (
                                <div className="flex gap-1 mt-1 flex-wrap">
                                  {ann.affects_symbols.map(s => (
                                    <span key={s} className="bg-white/10 text-white/60 text-xs px-1.5 py-0.5 rounded">{s}</span>
                                  ))}
                                </div>
                              )}
                              {ann.body && !isEditing && (
                                <p className="text-white/40 text-xs mt-1 line-clamp-2">{ann.body}</p>
                              )}
                              {ann.action_deadline && (
                                <p className="text-orange-400 text-xs mt-1">Prazo: {new Date(ann.action_deadline).toLocaleDateString("pt-BR")}</p>
                              )}
                              {/* Edit inline */}
                              {isEditing && (
                                <div className="mt-2 space-y-2">
                                  <textarea
                                    className="input w-full text-xs"
                                    rows={2}
                                    placeholder="Corpo / detalhe"
                                    value={annEditBody}
                                    onChange={e => setAnnEditBody(e.target.value)}
                                  />
                                  <div className="flex gap-2">
                                    <input
                                      className="input text-xs flex-1"
                                      placeholder="Símbolos (ADA,UNI,...)"
                                      value={annEditSymbols}
                                      onChange={e => setAnnEditSymbols(e.target.value)}
                                    />
                                    <input
                                      type="datetime-local"
                                      className="input text-xs flex-1"
                                      value={annEditDeadline}
                                      onChange={e => setAnnEditDeadline(e.target.value)}
                                    />
                                  </div>
                                  <div className="flex gap-2 justify-end">
                                    <button className="btn-secondary text-xs" onClick={() => setAnnEditId(null)}>Cancelar</button>
                                    <button
                                      className="btn-primary text-xs"
                                      onClick={async () => {
                                        if (!address || !signMessageAsync) return;
                                        try {
                                          const msg = `SoSoMon Admin\nTimestamp: ${new Date().toISOString()}`;
                                          const sig = await signMessageAsync({ message: msg });
                                          await adminAnnouncementApi.update(address, msg, sig, ann.id, {
                                            body: annEditBody || undefined,
                                            affects_symbols: annEditSymbols ? annEditSymbols.split(",").map(s => s.trim()) : undefined,
                                            action_deadline: annEditDeadline || null,
                                          });
                                          const rows = await adminAnnouncementApi.getAll(address, msg, sig);
                                          setAnnouncements(rows);
                                          setAnnEditId(null);
                                        } catch (e) { console.error(e); }
                                      }}
                                    >
                                      Salvar
                                    </button>
                                  </div>
                                </div>
                              )}
                            </div>
                            {/* Actions */}
                            <div className="flex gap-1 shrink-0">
                              <button
                                title="Editar"
                                className="text-white/30 hover:text-white/80 p-1"
                                onClick={() => {
                                  setAnnEditId(ann.id);
                                  setAnnEditBody(ann.body || "");
                                  setAnnEditSymbols(ann.affects_symbols.join(","));
                                  setAnnEditDeadline(ann.action_deadline ? ann.action_deadline.slice(0, 16) : "");
                                  setAnnCreateMode(false);
                                }}
                              >
                                <RefreshCw size={12} />
                              </button>
                              <button
                                title={ann.is_active ? "Desativar" : "Ativar"}
                                className="text-white/30 hover:text-white/80 p-1"
                                onClick={async () => {
                                  if (!address || !signMessageAsync) return;
                                  try {
                                    const msg = `SoSoMon Admin\nTimestamp: ${new Date().toISOString()}`;
                                    const sig = await signMessageAsync({ message: msg });
                                    await adminAnnouncementApi.update(address, msg, sig, ann.id, { is_active: !ann.is_active });
                                    const rows = await adminAnnouncementApi.getAll(address, msg, sig);
                                    setAnnouncements(rows);
                                  } catch (e) { console.error(e); }
                                }}
                              >
                                {ann.is_active ? <EyeOff size={12} /> : <Eye size={12} />}
                              </button>
                            </div>
                          </div>
                          <div className="text-white/20 text-xs mt-1">
                            {ann.published_at ? new Date(ann.published_at).toLocaleDateString("pt-BR") : new Date(ann.created_at).toLocaleDateString("pt-BR")}
                            {ann.labels.length > 0 && <span className="ml-2">{ann.labels.join(", ")}</span>}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            </>
          )}

        </div>
      </main>
    </div>
  );
}
