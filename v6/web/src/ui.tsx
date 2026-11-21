import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import type { Fix, FixStatus, Severity } from "./api";

// ------------------------------------------------------------------ formatting (design spec §2)
export function count(n: number | null | undefined, system: "in" | "intl" = "in"): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return Math.round(n).toLocaleString(system === "in" ? "en-IN" : "en-US");
}
export function ago(iso?: string | null): string {
  if (!iso) return "";
  const t = new Date(iso).getTime();
  const s = Math.round((Date.now() - t) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return new Date(iso).toLocaleDateString("en-IN", { day: "numeric", month: "short" }) + ", " +
    new Date(iso).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" });
}
export function day(iso: string): string {
  return new Date(iso).toLocaleDateString("en-IN", { day: "numeric", month: "short" });
}
export const STATUS_TEXT: Record<FixStatus, string> = {
  open: "Open", in_progress: "In progress", fixed_pending_check: "Fixed — waiting for check", verified: "Verified", wont_fix: "Won't fix",
};
export function statusPill(f: Pick<Fix, "status" | "regressed">) {
  if (f.regressed && f.status === "open") return <span className="pill new">Regressed</span>;
  const cls = f.status === "verified" ? "good" : f.status === "in_progress" ? "warn" : f.status === "fixed_pending_check" ? "good" : "";
  return <span className={`pill ${cls}`}>{STATUS_TEXT[f.status]}</span>;
}
export function Sev({ s }: { s: Severity }) {
  return <span className={`sev ${s}`}>{s[0].toUpperCase() + s.slice(1)}</span>;
}
export function Avatar({ name }: { name?: string | null }) {
  if (!name) return <span className="avatar none" title="Unassigned">?</span>;
  return <span className="avatar" title={name}>{name.split(/\s+/).map((p) => p[0]).slice(0, 2).join("").toUpperCase()}</span>;
}

// ------------------------------------------------------------------ icons (Lucide-style strokes)
const PATHS: Record<string, string> = {
  today: "M4 12a8 8 0 1 0 16 0 8 8 0 0 0-16 0zM12 7v5l3 2",
  live: "M3 12h4l2-6 4 12 2-6h6",
  fix: "M9 11l3 3 8-8M20 12v6a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h9",
  page: "M7 3h7l5 5v13H7zM14 3v5h5",
  journey: "M5 19a2 2 0 1 0 0-4 2 2 0 0 0 0 4zM19 9a2 2 0 1 0 0-4 2 2 0 0 0 0 4zM7 17h6a3 3 0 0 0 0-6h-2a3 3 0 0 1 0-6h6",
  plug: "M9 2v5M15 2v5M6 7h12v4a6 6 0 0 1-12 0zM12 17v5",
  shield: "M12 3l8 3v6c0 4.5-3.4 8-8 9-4.6-1-8-4.5-8-9V6z",
  desktop: "M3 5h18v11H3zM8 20h8M12 16v4",
  mobile: "M8 3h8v18H8zM11 18h2",
  up: "M4 17l6-6 4 4 6-8",
  play: "M7 5l12 7-12 7z",
  layers: "M12 3l9 5-9 5-9-5zM3 13l9 5 9-5",
  settings: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z",
  pause: "M9 6v12M15 6v12",
};
export function Icon({ name }: { name: string }) {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d={PATHS[name] || ""} fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

// ------------------------------------------------------------------ toast
const ToastCtx = createContext<(msg: string) => void>(() => {});
export const useToast = () => useContext(ToastCtx);
export function ToastProvider({ children }: { children: ReactNode }) {
  const [msg, setMsg] = useState<string | null>(null);
  const timer = useRef<number>();
  const show = useCallback((m: string) => {
    setMsg(m);
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setMsg(null), 3200);
  }, []);
  return (
    <ToastCtx.Provider value={show}>
      {children}
      {msg && <div className="toast" role="status">{msg}</div>}
    </ToastCtx.Provider>
  );
}

// ------------------------------------------------------------------ data hook
export function useLoad<T>(fn: () => Promise<T>, deps: unknown[]): { data: T | null; error: string | null; reload: () => void; loading: boolean } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let live = true;
    setLoading(true);
    fn().then((d) => { if (live) { setData(d); setError(null); } })
      .catch((e) => { if (live) setError(e?.detail || e?.message || String(e)); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);
  return { data, error, loading, reload: () => setTick((t) => t + 1) };
}

export function ErrorBox({ text, onRetry }: { text: string; onRetry?: () => void }) {
  return (
    <div className="error-box" role="alert">
      <p style={{ margin: 0 }}>{text}</p>
      {onRetry && <button className="btn" style={{ marginTop: 10 }} onClick={onRetry}>Try again</button>}
    </div>
  );
}

export async function copyText(text: string): Promise<boolean> {
  try { await navigator.clipboard.writeText(text); return true; } catch { return false; }
}

export function Spark({ values }: { values: number[] }) {
  if (values.length < 2) return null;
  const min = Math.min(...values) - 2, max = Math.max(...values) + 2;
  const pts = values.map((v, i) => [(i / (values.length - 1)) * 300, 52 - ((v - min) / Math.max(1, max - min)) * 46]);
  const d = pts.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)} ${y.toFixed(1)}`).join(" ");
  const last = pts[pts.length - 1];
  return (
    <svg className="spark" viewBox="0 0 300 56" preserveAspectRatio="none" role="img"
      aria-label={`Health score trend from ${Math.round(values[0])} to ${Math.round(values[values.length - 1])}`}>
      <path d={`${d} L300 56 L0 56 Z`} fill="var(--measure-soft)" />
      <path d={d} fill="none" stroke="var(--measure)" strokeWidth="2" vectorEffect="non-scaling-stroke" />
      <circle cx={last[0]} cy={last[1]} r="3.5" fill="var(--measure)" />
    </svg>
  );
}
