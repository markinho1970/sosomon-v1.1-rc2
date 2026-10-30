"use client";

import Link from "next/link";
import Image from "next/image";
import { useState, useRef, useEffect } from "react";
import { usePathname } from "next/navigation";
import { Menu, X, FlaskConical, ChevronDown, Bot, Megaphone, Globe } from "lucide-react";
import { ConnectButton } from "@rainbow-me/rainbowkit";
import { useAccount } from "wagmi";
import { useRouter } from "next/navigation";
import { useNetworkMode } from "@/lib/NetworkModeContext";
import { useLang } from "@/lib/LanguageContext";
import { LANGUAGES, type Lang } from "@/lib/i18n/translations";

export default function Navbar() {
  const [open, setOpen] = useState(false);
  const [langOpen, setLangOpen] = useState(false);
  const langRef = useRef<HTMLDivElement>(null);
  const { isTestnet, resetToMainnet } = useNetworkMode();
  const { isConnected } = useAccount();
  const { lang, setLang, t } = useLang();
  const pathname = usePathname();
  const router = useRouter();

  const isHome = pathname === "/";

  const isFaucetPage = pathname === "/faucet-sepolia";

  const currentLang = LANGUAGES.find(l => l.code === lang)!;

  useEffect(() => {
    function onClick(e: MouseEvent) {
      if (langRef.current && !langRef.current.contains(e.target as Node)) setLangOpen(false);
    }
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, []);

  return (
    <nav className="fixed top-0 left-0 right-0 z-50 border-b border-brand-border bg-black/80 backdrop-blur-md">
      {isTestnet && (
        <div className="w-full bg-orange-500 text-black py-1.5 px-4 flex items-center justify-center gap-2 text-xs font-bold">
          <FlaskConical size={13} />
          {t("testnet_banner")}
        </div>
      )}

      <div className="max-w-7xl mx-auto px-4 sm:px-6 flex items-center justify-between py-2">

        <Link href="/" className="flex items-center">
          <Image src="/logo.png" alt="SoSoMon" width={320} height={80} priority className="h-12 w-auto" />
        </Link>

        <div className="hidden md:flex items-center gap-6">
          <Link href="/indexes" className="text-sm text-white/60 hover:text-white transition-colors">{t("nav_indexes")}</Link>
          <Link href="/transparency" className="text-sm text-white/60 hover:text-white transition-colors">{t("nav_transparency")}</Link>
          <Link href="/ai-insights" className="flex items-center gap-1 text-sm text-white/60 hover:text-white transition-colors">
            <Bot size={13} />
            {t("nav_ai_insights")}
          </Link>
          <Link href="/universe" className="flex items-center gap-1 text-sm text-white/60 hover:text-white transition-colors">
            <Globe size={13} />
            Universo
          </Link>

          {isTestnet ? (
            <Link href="/faucet-sepolia" className="text-sm text-white/60 hover:text-white transition-colors">{t("nav_faucet")}</Link>
          ) : (
            <span title={t("nav_faucet_locked_hint")} className="text-sm text-white/20 cursor-not-allowed select-none">{t("nav_faucet")}</span>
          )}

          <Link href="/announcements" className="flex items-center gap-1.5 text-sm text-white/60 hover:text-white transition-colors">
            <Megaphone size={13} />
            Anúncios
          </Link>

          <Link href="/whats-new" className="flex items-center gap-1.5 text-sm text-amber-400/80 hover:text-amber-300 transition-colors font-medium">
            <span className="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse" />
            {t("nav_whats_new")}
          </Link>
        </div>

        <div className="hidden md:flex items-center gap-2">
          {isConnected ? (
            <Link href="/dashboard" className="text-sm text-white/60 hover:text-white px-4 py-2 rounded-lg border border-white/10 hover:border-white/20 transition-all">
              {t("nav_dashboard")}
            </Link>
          ) : (
            <span title={t("nav_dashboard_locked")} className="text-sm text-white/20 cursor-not-allowed select-none px-4 py-2 rounded-lg border border-white/5">
              {t("nav_dashboard")}
            </span>
          )}

          <div ref={langRef} className="relative">
            <button
              onClick={() => setLangOpen(v => !v)}
              className="flex items-center gap-1.5 px-3 py-2 rounded-lg border border-white/10 bg-white/5 text-white/60 hover:text-white hover:border-white/20 text-xs font-medium transition-all"
            >
              <span>{currentLang.flag}</span>
              <span>{currentLang.code.toUpperCase()}</span>
              <ChevronDown size={11} className={`transition-transform ${langOpen ? "rotate-180" : ""}`} />
            </button>
            {langOpen && (
              <div className="absolute right-0 top-full mt-1 w-40 bg-[#0d1117] border border-white/10 rounded-xl shadow-2xl py-1 z-50">
                {LANGUAGES.map(l => (
                  <button
                    key={l.code}
                    onClick={() => { setLang(l.code as Lang); setLangOpen(false); }}
                    className={`w-full text-left flex items-center gap-2.5 px-3 py-2 text-xs transition-all ${
                      lang === l.code ? "text-white bg-white/8" : "text-white/50 hover:text-white hover:bg-white/5"
                    }`}
                  >
                    <span className="text-base">{l.flag}</span>
                    <span>{l.label}</span>
                    {lang === l.code && <span className="ml-auto w-1.5 h-1.5 rounded-full bg-amber-400" />}
                  </button>
                ))}
              </div>
            )}
          </div>

          {isConnected || isHome ? (
            <ConnectButton accountStatus="avatar" chainStatus="none" showBalance={false} />
          ) : (
            <button
              onClick={() => { resetToMainnet(); router.push("/"); }}
              className="btn-primary px-5 py-2 text-sm"
            >
              {t("nav_login")}
            </button>
          )}
        </div>

        <button className="md:hidden text-white/60 hover:text-white" onClick={() => setOpen(!open)}>
          {open ? <X size={20} /> : <Menu size={20} />}
        </button>
      </div>

      {open && (
        <div className="md:hidden border-t border-brand-border bg-black px-4 py-4 flex flex-col gap-4">
          <Link href="/indexes" className="text-sm text-white/60 hover:text-white" onClick={() => setOpen(false)}>{t("nav_indexes")}</Link>
          <Link href="/transparency" className="text-sm text-white/60 hover:text-white" onClick={() => setOpen(false)}>{t("nav_transparency")}</Link>
          <Link href="/ai-insights" className="flex items-center gap-1 text-sm text-white/60 hover:text-white" onClick={() => setOpen(false)}>
            <Bot size={13} />
            {t("nav_ai_insights")}
          </Link>
          <Link href="/universe" className="flex items-center gap-1 text-sm text-white/60 hover:text-white" onClick={() => setOpen(false)}>
            <Globe size={13} />
            Universo
          </Link>
          {isTestnet ? (
            <Link href="/faucet-sepolia" className="text-sm text-white/60 hover:text-white" onClick={() => setOpen(false)}>{t("nav_faucet")}</Link>
          ) : (
            <span className="text-sm text-white/20 cursor-not-allowed">{t("nav_faucet")}</span>
          )}
          <Link href="/announcements" className="flex items-center gap-1.5 text-sm text-white/60 hover:text-white" onClick={() => setOpen(false)}>
            <Megaphone size={13} />
            Anúncios
          </Link>
          <Link href="/whats-new" className="text-sm text-amber-400/80 hover:text-amber-300 font-medium" onClick={() => setOpen(false)}>{t("nav_whats_new")}</Link>
          {isConnected ? (
            <Link href="/dashboard" className="text-sm text-center text-white/60 border border-white/10 rounded-lg px-4 py-2" onClick={() => setOpen(false)}>{t("nav_dashboard")}</Link>
          ) : (
            <span className="text-sm text-center text-white/20 cursor-not-allowed select-none border border-white/5 rounded-lg px-4 py-2">{t("nav_dashboard")}</span>
          )}
          <div className="grid grid-cols-4 gap-1">
            {LANGUAGES.map(l => (
              <button key={l.code} onClick={() => { setLang(l.code as Lang); setOpen(false); }}
                className={`py-2 rounded-lg text-lg text-center transition-all ${lang === l.code ? "bg-white/10" : "hover:bg-white/5"}`}>
                {l.flag}
              </button>
            ))}
          </div>
          <div className="flex justify-center">
            {isConnected || isHome ? (
              <ConnectButton accountStatus="full" chainStatus="none" showBalance={false} />
            ) : (
              <button
                onClick={() => { resetToMainnet(); router.push("/"); setOpen(false); }}
                className="btn-primary px-6 py-2 text-sm w-full"
              >
                {t("nav_login")}
              </button>
            )}
          </div>
        </div>
      )}
    </nav>
  );
}
