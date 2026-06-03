/**
 * tools/demo-ui/web/src/state/DemoStateContext.tsx
 * ─────────────────────────────────────────────────
 * Hoisted shared state: polled DemoState, polled DemoMetrics, SSE event
 * feed, toast notifications, and an `action()` helper that wraps a BFF
 * call with toast feedback. Lives at the app root so navigating between
 * pages doesn't reset polling or drop SSE events.
 *
 * Pages consume via `useDemo()`; never read these directly off the
 * provider value.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";

import { api, type DemoMetrics, type DemoState } from "../api";
import {
  FEED_MAX,
  METRICS_POLL_MS,
  POLL_MS,
  feedSummary,
  type FeedItem,
} from "../utils";


interface ToastState {
  msg: string;
  ok: boolean;
}

export interface DemoContextValue {
  state: DemoState | null;
  metrics: DemoMetrics | null;
  feed: FeedItem[];
  sseConnected: boolean;
  error: string | null;
  busy: boolean;
  toast: ToastState | null;
  action: (label: string, fn: () => Promise<unknown>) => Promise<void>;
}

const Ctx = createContext<DemoContextValue | null>(null);


export function DemoStateProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<DemoState | null>(null);
  const [metrics, setMetrics] = useState<DemoMetrics | null>(null);
  const [feed, setFeed] = useState<FeedItem[]>([]);
  const [sseConnected, setSseConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState<ToastState | null>(null);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // ── DemoState polling ───────────────────────────────────────────────────
  useEffect(() => {
    let cancelled = false;
    async function tick() {
      try {
        const r = await api.getDemoState();
        if (!cancelled) { setState(r); setError(null); }
      } catch (err: any) {
        if (!cancelled) setError(err.message || "demo/state fetch failed");
      }
    }
    tick();
    const id = setInterval(tick, POLL_MS);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  // ── DemoMetrics polling ─────────────────────────────────────────────────
  useEffect(() => {
    let cancelled = false;
    async function fetchMetrics() {
      try {
        const m = await api.getDemoMetrics();
        if (!cancelled) setMetrics(m);
      } catch {
        // TimescaleDB may not be configured — silent
      }
    }
    fetchMetrics();
    const id = setInterval(fetchMetrics, METRICS_POLL_MS);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  // ── SSE event stream ────────────────────────────────────────────────────
  useEffect(() => {
    const es = new EventSource("/api/ui/events");
    es.onopen = () => setSseConnected(true);
    es.onerror = () => setSseConnected(false);
    es.onmessage = (evt) => {
      try {
        const msg = JSON.parse(evt.data) as { channel: string; envelope: any };
        const item: FeedItem = {
          id: msg.envelope?.event_id ?? String(Date.now()) + Math.random(),
          channel: msg.channel,
          ts: new Date().toLocaleTimeString(),
          summary: feedSummary(msg.channel, msg.envelope),
        };
        setFeed((prev) => [item, ...prev].slice(0, FEED_MAX));
      } catch {
        // ignore parse errors
      }
    };
    return () => { es.close(); setSseConnected(false); };
  }, []);

  // ── Action helper with toast ────────────────────────────────────────────
  const showToast = useCallback((msg: string, ok: boolean) => {
    if (toastTimer.current) clearTimeout(toastTimer.current);
    setToast({ msg, ok });
    toastTimer.current = setTimeout(() => setToast(null), 3500);
  }, []);

  const action = useCallback(
    async (label: string, fn: () => Promise<unknown>) => {
      setBusy(true);
      try {
        await fn();
        showToast(`${label} — OK`, true);
      } catch (err: any) {
        showToast(`${label} — ${err.message || "error"}`, false);
      } finally {
        setBusy(false);
      }
    },
    [showToast],
  );

  const value: DemoContextValue = {
    state,
    metrics,
    feed,
    sseConnected,
    error,
    busy,
    toast,
    action,
  };

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}


export function useDemo(): DemoContextValue {
  const v = useContext(Ctx);
  if (!v) throw new Error("useDemo must be used inside <DemoStateProvider>");
  return v;
}
