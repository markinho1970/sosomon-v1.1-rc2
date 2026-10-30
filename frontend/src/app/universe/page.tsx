"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import { Globe, RefreshCw, TrendingUp, TrendingDown, Minus, Filter, ChevronDown, Clock } from "lucide-react";
import Navbar from "../components/Navbar";

interface UniverseAsset {
  symbol: string;
  name: string;
  theme: string;
  ssi_weight: number;
  marketcap: number;
  mcap_rank: number;
  volume_24h: number;
  vol_mcap_ratio: number;
  roi_7d: number;
  roi_30d: number;
  roi_3m: number;
  high_3m: number;
  dist_from_high: number;
  beta_vs_ssi: number;
  current_price: number;
  in_basket: boolean;
  is_anchor: boolean;
  sodex_listed: boolean;
  sodex_status: string;
  updated_at: string | null;
}

const THEME_LABELS: Record<string, string> = {
  depin: "DePIN",
  defi: "DeFi",
  "ai-crypto": "AI & Tech",
  rwa: "RWA",
};

const THEME_COLORS: Record<string, string> = {
  depin: "text-orange-400 bg-orange-400/10 border-orange-400/20",
  defi: "text-purple-400 bg-purple-400/10 border-purple-400/20",
  "ai-crypto": "text-blue-400 bg-blue-400/10 border-blue-400/20",
  rwa: "text-emerald-400 bg-emerald-400/10 border-emerald-400/20",
};

const SORT_OPTIONS = [
  { value: "mcap_rank", label: "MCap Rank" },
  { value: "roi_30d",   label: "ROI 30d" },
  { value: "roi_7d",    label: "ROI 7d" },
  { value: "ssi_weight",label: "SSI Weight" },
  { value: "vol",       label: "Volatilidade" },
  { value: "vol_mcap",  label: "Vol/MCap" },
];

const REFRESH_OPTIONS = [
  { value: 0,      label: "Off" },
  { value: 3000,   label: "3s" },
  { value: 5000,   label: "5s" },
  { value: 10000,  label: "10s" },
  { value: 30000,  label: "30s" },
  { value: 60000,  label: "1m" },
  { value: 300000, label: "5m" },
];

// Dropdown genérico customizado (evita problema de cores do <select> nativo)
function Dropdown({
  options,
  value,
  onChange,
  prefix,
}: {
  options: { value: string | number; label: string }[];
  value: string | number;
  onChange: (v: string | number) => void;
  prefix?: string;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const selected = options.find(o => o.value === value);

  useEffect(() => {
    function onClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, []);

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen(v => !v)}
        className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-white/10 bg-[#0d1117] text-white/60 text-xs hover:border-white/20 hover:text-white transition-all cursor-pointer"
      >
        {prefix && <span className="text-white/30">{prefix}</span>}
        <span className="text-white/80 font-medium">{selected?.label ?? "—"}</span>
        <ChevronDown size={10} className={`text-white/30 transition-transform ${open ? "rotate-180" : ""}`} />
      </button>
      {open && (
        <div className="absolute left-0 top-full mt-1 w-36 bg-[#0d1117] border border-white/10 rounded-xl shadow-2xl py-1 z-50">
          {options.map(o => (
            <button
              key={o.value}
              onClick={() => { onChange(o.value); setOpen(false); }}
              className={`w-full text-left px-3 py-2 text-xs transition-all ${
                value === o.value
                  ? "text-white bg-white/8 font-medium"
                  : "text-white/50 hover:text-white hover:bg-white/5"
              }`}
            >
              {o.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function fmtPct(v: number, decimals = 1) {
  if (v === null || v === undefined) return "—";
  return `${v > 0 ? "+" : ""}${v.toFixed(decimals)}%`;
}

function fmtMcap(v: number) {
  if (!v) return "—";
  if (v >= 1e9) return `$${(v / 1e9).toFixed(1)}B`;
  if (v >= 1e6) return `$${(v / 1e6).toFixed(0)}M`;
  return `$${v.toFixed(0)}`;
}

function fmtPrice(v: number) {
  if (!v) return "—";
  if (v >= 1000) return `$${v.toFixed(0)}`;
  if (v >= 1) return `$${v.toFixed(2)}`;
  return `$${v.toFixed(4)}`;
}

function pctColor(v: number) {
  if (!v && v !== 0) return "text-white/30";
  const r = parseFloat(v.toFixed(1));
  if (r === 0) return "text-white/30";
  return v > 0 ? "text-green-400" : "text-red-400";
}

function volColor(v: number) {
  if (v >= 1.5) return "text-red-400";
  if (v >= 1.1) return "text-orange-400";
  if (v <= 0.7) return "text-blue-400";
  return "text-white/60";
}

function PctArrow({ v }: { v: number }) {
  const r = parseFloat((v ?? 0).toFixed(1));
  if (r > 0) return <TrendingUp size={11} className="text-green-400 inline mr-0.5" />;
  if (r < 0) return <TrendingDown size={11} className="text-red-400 inline mr-0.5" />;
  return <Minus size={11} className="text-white/30 inline mr-0.5" />;
}

function AssetCard({ asset }: { asset: UniverseAsset }) {
  const themeClass = THEME_COLORS[asset.theme] ?? "text-white/40 bg-white/5 border-white/10";
  const statusOk = asset.sodex_status === "TRADING";

  return (
    <div className={`
      relative rounded-2xl border bg-white/[0.03] p-4 flex flex-col gap-3
      transition-all duration-200 hover:bg-white/[0.06]
      ${asset.in_basket ? "border-brand-orange/30 shadow-[0_0_0_1px_rgba(249,115,22,0.15)]" : "border-white/8"}
    `}>
      {/* Top row */}
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5">
          <span className={`w-1.5 h-1.5 rounded-full ${statusOk ? "bg-green-400" : asset.sodex_listed ? "bg-red-400" : "bg-white/20"}`} />
          <span className={`text-[10px] font-medium ${statusOk ? "text-green-400" : asset.sodex_listed ? "text-red-400" : "text-white/30"}`}>
            {asset.sodex_listed ? asset.sodex_status : "Não listado"}
          </span>
        </div>
        <div className="flex items-center gap-1.5">
          <span className={`text-[10px] font-semibold px-2 py-0.5 rounded-full border ${themeClass}`}>
            {THEME_LABELS[asset.theme] ?? asset.theme}
          </span>
          {asset.in_basket && (
            <span className="text-[10px] font-bold px-2 py-0.5 rounded-full bg-brand-orange/15 text-brand-orange border border-brand-orange/30">
              {asset.is_anchor ? "ÂNCORA" : "CESTA"}
            </span>
          )}
        </div>
      </div>

      {/* Symbol + price */}
      <div className="flex items-end justify-between">
        <div>
          <div className="text-lg font-bold text-white leading-tight">{asset.symbol}</div>
          <div className="text-[11px] text-white/40 truncate max-w-[120px]">{asset.name}</div>
        </div>
        <div className="text-right">
          <div className="text-sm font-semibold text-white">{fmtPrice(asset.current_price)}</div>
          <div className="text-[10px] text-white/40">#{asset.mcap_rank} · {fmtMcap(asset.marketcap)}</div>
        </div>
      </div>

      {/* ROI grid */}
      <div className="grid grid-cols-3 gap-1 text-center">
        {[
          { label: "7d", v: asset.roi_7d },
          { label: "30d", v: asset.roi_30d },
          { label: "3m", v: asset.roi_3m },
        ].map(({ label, v }) => (
          <div key={label} className="bg-white/[0.03] rounded-lg px-1 py-1.5">
            <div className="text-[9px] text-white/30 mb-0.5">{label}</div>
            <div className={`text-xs font-semibold tabular-nums ${pctColor(v)}`}>
              <PctArrow v={v} />
              {fmtPct(v)}
            </div>
          </div>
        ))}
      </div>

      {/* Métricas */}
      <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-[10px]">
        <div className="flex justify-between">
          <span className="text-white/30">SSI Wt</span>
          <span className="text-white/60 font-medium">{asset.ssi_weight.toFixed(1)}%</span>
        </div>
        <div className="flex justify-between">
          <span className="text-white/30">σ 30d</span>
          <span className={`font-medium ${volColor(asset.beta_vs_ssi)}`}>{asset.beta_vs_ssi.toFixed(2)}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-white/30">Vol/MCap</span>
          <span className="text-white/60 font-medium">
            {asset.vol_mcap_ratio > 0 ? asset.vol_mcap_ratio.toFixed(3) : "—"}
          </span>
        </div>
        <div className="flex justify-between">
          <span className="text-white/30">vs High 3m</span>
          <span className={`font-medium ${pctColor(asset.dist_from_high)}`}>
            {fmtPct(asset.dist_from_high)}
          </span>
        </div>
      </div>
    </div>
  );
}

export default function UniversePage() {
  const [assets, setAssets] = useState<UniverseAsset[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [updatedAt, setUpdatedAt] = useState<string | null>(null);
  const [themeFilter, setThemeFilter] = useState<string>("all");
  const [basketFilter, setBasketFilter] = useState<string>("all");
  const [sortBy, setSortBy] = useState<string>("mcap_rank");
  const [autoInterval, setAutoInterval] = useState<number>(5000);
  const [countdown, setCountdown] = useState<number>(0);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const countdownRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const params = themeFilter !== "all" ? `?theme=${themeFilter}` : "";
      const res = await fetch(`/api/universe${params}`);
      const json = await res.json();
      setAssets(json.data ?? []);
      setUpdatedAt(json.updated_at ?? null);
    } catch (e) {
      console.error("Universe load error:", e);
    } finally {
      setLoading(false);
    }
  }, [themeFilter]);

  useEffect(() => { load(); }, [load]);

  // Auto-refresh logic
  useEffect(() => {
    if (intervalRef.current) clearInterval(intervalRef.current);
    if (countdownRef.current) clearInterval(countdownRef.current);
    if (autoInterval <= 0) { setCountdown(0); return; }

    setCountdown(autoInterval / 1000);
    intervalRef.current = setInterval(() => {
      load();
      setCountdown(autoInterval / 1000);
    }, autoInterval);

    countdownRef.current = setInterval(() => {
      setCountdown(prev => (prev <= 1 ? autoInterval / 1000 : prev - 1));
    }, 1000);

    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current);
      if (countdownRef.current) clearInterval(countdownRef.current);
    };
  }, [autoInterval, load]);

  const triggerDbRefresh = async () => {
    setRefreshing(true);
    try {
      await fetch("/api/universe/refresh", { method: "POST" });
      alert("Refresh da base iniciado (~5 min). Use o auto-refresh para ver quando atualizar.");
    } catch {
      alert("Erro ao iniciar refresh.");
    } finally {
      setRefreshing(false);
    }
  };

  const filtered = assets
    .filter(a => basketFilter === "all" ? true : basketFilter === "basket" ? a.in_basket : !a.in_basket)
    .sort((a, b) => {
      switch (sortBy) {
        case "roi_30d":    return (b.roi_30d ?? 0) - (a.roi_30d ?? 0);
        case "roi_7d":     return (b.roi_7d ?? 0) - (a.roi_7d ?? 0);
        case "ssi_weight": return (b.ssi_weight ?? 0) - (a.ssi_weight ?? 0);
        case "vol":        return (b.beta_vs_ssi ?? 1) - (a.beta_vs_ssi ?? 1);
        case "vol_mcap":   return (b.vol_mcap_ratio ?? 0) - (a.vol_mcap_ratio ?? 0);
        default:           return (a.mcap_rank ?? 999) - (b.mcap_rank ?? 999);
      }
    });

  const inBasketCount = assets.filter(a => a.in_basket).length;
  const tradingCount  = assets.filter(a => a.sodex_status === "TRADING").length;
  const updatedStr    = updatedAt
    ? new Date(updatedAt).toLocaleString("pt-BR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })
    : null;

  return (
    <div className="min-h-screen bg-brand-bg">
      <Navbar />
      <div className="max-w-7xl mx-auto px-4 pt-28 pb-16">

        {/* Header */}
        <div className="flex items-start justify-between mb-8 gap-4 flex-wrap">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-xl bg-brand-orange/10 border border-brand-orange/20 flex items-center justify-center">
              <Globe size={20} className="text-brand-orange" />
            </div>
            <div>
              <h1 className="text-2xl font-bold text-white">Universo de Ativos</h1>
              <p className="text-xs text-white/40 mt-0.5">
                Todos os ativos SoSoValue+SoDEX · {assets.length} tokens · {tradingCount} trading · {inBasketCount} em cesta
                {updatedStr && <span className="ml-2">· atualizado {updatedStr}</span>}
              </p>
            </div>
          </div>

          {/* Controles de refresh */}
          <div className="flex items-center gap-2">
            {/* Auto-refresh seletor */}
            <div className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-white/10 bg-[#0d1117]">
              <Clock size={11} className={autoInterval > 0 ? "text-brand-orange" : "text-white/30"} />
              <span className="text-[10px] text-white/30">Auto:</span>
              <Dropdown
                options={REFRESH_OPTIONS}
                value={autoInterval}
                onChange={v => setAutoInterval(v as number)}
              />
              {autoInterval > 0 && countdown > 0 && (
                <span className="text-[10px] text-brand-orange/70 tabular-nums w-6 text-right">{countdown}s</span>
              )}
            </div>

            {/* Refresh manual da base SoSoValue */}
            <button
              onClick={triggerDbRefresh}
              disabled={refreshing}
              title="Atualiza a base de dados via API SoSoValue (~5 min)"
              className="flex items-center gap-1.5 px-3 py-2 rounded-lg border border-white/10 bg-white/5 text-white/50 hover:text-white hover:border-white/20 text-xs transition-all disabled:opacity-40"
            >
              <RefreshCw size={12} className={refreshing ? "animate-spin" : ""} />
              {refreshing ? "..." : "Refresh DB"}
            </button>
          </div>
        </div>

        {/* Filtros */}
        <div className="flex flex-wrap items-center gap-2 mb-6">
          <Filter size={12} className="text-white/30" />

          {/* Tema */}
          <div className="flex items-center gap-1">
            {(["all", "depin", "defi", "ai-crypto", "rwa"] as const).map(t => (
              <button
                key={t}
                onClick={() => setThemeFilter(t)}
                className={`px-3 py-1.5 rounded-full text-xs font-medium transition-all ${
                  themeFilter === t
                    ? "bg-brand-orange text-black"
                    : "border border-white/10 text-white/50 hover:text-white hover:border-white/20"
                }`}
              >
                {t === "all" ? "Todos" : THEME_LABELS[t]}
              </button>
            ))}
          </div>

          <div className="w-px h-4 bg-white/10" />

          {/* Cesta */}
          <div className="flex items-center gap-1">
            {[
              { v: "all",        label: "Todos" },
              { v: "basket",     label: "Na Cesta" },
              { v: "candidates", label: "Candidatos" },
            ].map(({ v, label }) => (
              <button
                key={v}
                onClick={() => setBasketFilter(v)}
                className={`px-3 py-1.5 rounded-full text-xs font-medium transition-all ${
                  basketFilter === v
                    ? "bg-white/10 text-white border border-white/20"
                    : "border border-white/10 text-white/40 hover:text-white/70"
                }`}
              >
                {label}
              </button>
            ))}
          </div>

          <div className="w-px h-4 bg-white/10" />

          {/* Ordenação — dropdown customizado */}
          <div className="flex items-center gap-1.5">
            <span className="text-[10px] text-white/30">Ordenar:</span>
            <Dropdown
              options={SORT_OPTIONS}
              value={sortBy}
              onChange={v => setSortBy(v as string)}
            />
          </div>
        </div>

        {/* Legenda volatilidade */}
        <div className="flex flex-wrap items-center gap-2 mb-5">
          <span className="text-[10px] text-white/30 mr-1">σ 30d</span>
          {[
            { label: "≤ 0.7", desc: "Baixa", color: "text-blue-400 bg-blue-400/10 border-blue-400/25" },
            { label: "0.7 – 1.1", desc: "Moderada", color: "text-white/60 bg-white/5 border-white/10" },
            { label: "1.1 – 1.5", desc: "Alta", color: "text-orange-400 bg-orange-400/10 border-orange-400/25" },
            { label: "≥ 1.5", desc: "Muito alta", color: "text-red-400 bg-red-400/10 border-red-400/25" },
          ].map(({ label, desc, color }) => (
            <div key={label} className={`flex items-center gap-1.5 px-2.5 py-1 rounded-full border text-[10px] font-medium ${color}`}>
              <span className="font-bold">{label}</span>
              <span className="opacity-70">{desc}</span>
            </div>
          ))}
        </div>

        {/* Grid */}
        {loading ? (
          <div className="flex items-center justify-center py-24 text-white/30 text-sm">
            {autoInterval > 0 ? "Atualizando..." : "Carregando universo..."}
          </div>
        ) : filtered.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-24 gap-3">
            <Globe size={32} className="text-white/10" />
            <p className="text-white/30 text-sm">
              {assets.length === 0
                ? "Nenhum dado disponível. Clique em Refresh DB para carregar."
                : "Nenhum ativo com este filtro."}
            </p>
          </div>
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3">
            {filtered.map(asset => (
              <AssetCard key={`${asset.symbol}-${asset.theme}`} asset={asset} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
