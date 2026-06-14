/* ============================================================================
   Data mode -- "robust either way" live / demonstration state
   ----------------------------------------------------------------------------
   SmartLoad's operator console is built to look fully operational on a
   representative dataset (Demonstration) and to switch seamlessly to real data
   when a backend is reachable (Live connected). This module owns that model:

     mode        -- the overall posture: "live" once any panel reports live
                    data, otherwise "demo". Demonstration is an intentional,
                    professional state, never a degraded one.
     connection  -- "connected" | "connecting" | "offline": the link health
                    that drives the calm indicator.

   Panels do not each ship their own banner. Instead every panel resolves its
   data through useLiveOrDemo, then registers the source it actually used. The
   provider aggregates those registrations so the single global DataModeBadge
   reflects reality: if even one panel is live we are "live"; while panels are
   still resolving we are "connecting"; if every resolved panel fell back we are
   an offline "demo".

   This module is deliberately free of any API import -- callers pass their own
   loader function -- so it stays a pure presentation-layer primitive.
   ============================================================================ */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

// ── types ──────────────────────────────────────────────────────────────────

/** Which dataset a value came from. */
export type DataSource = "live" | "demo";

/** Overall console posture. */
export type DataMode = "live" | "demo";

/** Link health to the decision plane. */
export type ConnectionState = "connected" | "connecting" | "offline";

/** Lifecycle of a single useLiveOrDemo resolution. */
export type LoadStatus = "loading" | "ready" | "error";

export interface DataModeContextValue {
  /** Aggregate posture across all registered panels. */
  mode: DataMode;
  /** Aggregate link health across all registered panels. */
  connection: ConnectionState;
  /**
   * Register (or update) the source a panel resolved to. Returns an unregister
   * function; call it when the panel unmounts so stale sources don't linger.
   * `null` source means "still resolving" and reads as connecting.
   */
  register: (panelId: string, source: DataSource | null) => () => void;
  /** Count of panels currently registered (handy for diagnostics). */
  panelCount: number;
}

const DataModeContext = createContext<DataModeContextValue | null>(null);

// Stable standalone fallback so a panel used outside a provider doesn't get a
// fresh `register` identity every render (which would re-fire useLiveOrDemo).
const STANDALONE_CTX: DataModeContextValue = {
  mode: "demo",
  connection: "connecting",
  register: () => () => undefined,
  panelCount: 0,
};

// ── provider ─────────────────────────────────────────────────────────────────

export interface DataModeProviderProps {
  children?: ReactNode;
  /**
   * Posture before any panel has resolved. Defaults to "connecting" so the
   * first paint reads as an honest "establishing link", then settles.
   */
  initialConnection?: ConnectionState;
}

function aggregate(sources: Map<string, DataSource | null>): {
  mode: DataMode;
  connection: ConnectionState;
} {
  if (sources.size === 0) {
    return { mode: "demo", connection: "connecting" };
  }
  let anyLive = false;
  let anyPending = false;
  let anyDemo = false;
  for (const src of sources.values()) {
    if (src === null) anyPending = true;
    else if (src === "live") anyLive = true;
    else anyDemo = true;
  }
  // Any live panel => we are live and connected.
  if (anyLive) return { mode: "live", connection: "connected" };
  // Nothing live yet, but something is still resolving => connecting.
  if (anyPending) return { mode: "demo", connection: "connecting" };
  // Everything resolved and all fell back => intentional offline demonstration.
  if (anyDemo) return { mode: "demo", connection: "offline" };
  return { mode: "demo", connection: "connecting" };
}

export function DataModeProvider({ children }: DataModeProviderProps) {
  // A ref holds the live map; state holds the derived snapshot so consumers
  // re-render only when the aggregate posture actually changes.
  const sourcesRef = useRef<Map<string, DataSource | null>>(new Map());
  const [snapshot, setSnapshot] = useState<{ mode: DataMode; connection: ConnectionState }>(
    { mode: "demo", connection: "connecting" },
  );
  const [panelCount, setPanelCount] = useState(0);

  const recompute = useCallback(() => {
    const next = aggregate(sourcesRef.current);
    setSnapshot((prev) =>
      prev.mode === next.mode && prev.connection === next.connection ? prev : next,
    );
    setPanelCount(sourcesRef.current.size);
  }, []);

  const register = useCallback(
    (panelId: string, source: DataSource | null) => {
      sourcesRef.current.set(panelId, source);
      recompute();
      return () => {
        sourcesRef.current.delete(panelId);
        recompute();
      };
    },
    [recompute],
  );

  const value = useMemo<DataModeContextValue>(
    () => ({
      mode: snapshot.mode,
      connection: snapshot.connection,
      register,
      panelCount,
    }),
    [snapshot.mode, snapshot.connection, register, panelCount],
  );

  return <DataModeContext.Provider value={value}>{children}</DataModeContext.Provider>;
}

/**
 * Read the aggregate data-mode posture. Returns a safe standalone default when
 * used outside a provider, so a lone panel never crashes.
 */
export function useDataMode(): DataModeContextValue {
  return useContext(DataModeContext) ?? STANDALONE_CTX;
}

// ── useLiveOrDemo ─────────────────────────────────────────────────────────────

export interface LiveOrDemo<T> {
  /** The resolved value: live data on success, the demo fallback otherwise. */
  value: T;
  /** Where `value` came from. */
  source: DataSource;
  /** Resolution lifecycle for skeletons / inline errors. */
  state: LoadStatus;
  /**
   * True only for a *partial* outage: this panel failed while the console is
   * otherwise live (at least one panel reached a backend). In standalone
   * Demonstration mode -- where every panel falls back -- this stays false, so
   * panels render their representative data cleanly and the single global
   * DataModeBadge is the only "demonstration" signal. Gate per-panel ErrorState
   * banners on this, not on `state === "error"`.
   */
  degraded: boolean;
  /** Re-run the loader (e.g. an ErrorState retry affordance). */
  reload: () => void;
}

export interface UseLiveOrDemoOptions {
  /** Abandon the live call after this many ms and fall back. Default 4000. */
  timeoutMs?: number;
  /**
   * Optional id used to register this panel's source with the DataModeProvider.
   * Omit to opt out of the global aggregate (the hook still works standalone).
   */
  panelId?: string;
  /**
   * Re-run when any value in this array changes (same contract as a dependency
   * array). The loader identity is intentionally NOT a dependency so inline
   * arrow functions don't cause refetch loops.
   */
  deps?: ReadonlyArray<unknown>;
}

function withTimeout<T>(p: Promise<T>, ms: number): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const id = window.setTimeout(() => reject(new Error("timeout")), ms);
    p.then(
      (v) => {
        window.clearTimeout(id);
        resolve(v);
      },
      (e) => {
        window.clearTimeout(id);
        reject(e);
      },
    );
  });
}

/**
 * Resolve a value as "live or demo": run an async loader with a timeout; on
 * success return the live value (source "live"), on any error or timeout return
 * the supplied demonstration fallback (source "demo"). Generic and API-agnostic
 * -- the caller passes the loader, so this never imports the app's api layer.
 *
 * When a panelId is supplied (and a DataModeProvider is mounted) the resolved
 * source is registered with the provider so the global indicator reflects it.
 *
 * The demonstration fallback is shown immediately while loading, so a panel is
 * never blank -- it shows representative data, then quietly upgrades to live.
 */
export function useLiveOrDemo<T>(
  loader: () => Promise<T>,
  demo: T,
  options: UseLiveOrDemoOptions = {},
): LiveOrDemo<T> {
  const { timeoutMs = 4000, panelId, deps = [] } = options;
  const { register, mode } = useDataMode();

  const [value, setValue] = useState<T>(demo);
  const [source, setSource] = useState<DataSource>("demo");
  const [state, setState] = useState<LoadStatus>("loading");
  const [nonce, setNonce] = useState(0);

  // Keep the latest loader without making it a dependency (avoids refetch loops
  // from inline arrow functions the views pass in).
  const loaderRef = useRef(loader);
  loaderRef.current = loader;
  const demoRef = useRef(demo);
  demoRef.current = demo;

  const reload = useCallback(() => setNonce((n) => n + 1), []);

  useEffect(() => {
    let cancelled = false;
    let unregister: (() => void) | undefined;
    if (panelId) unregister = register(panelId, null); // null => "connecting"

    setState("loading");
    // Show representative data immediately; never blank.
    setValue(demoRef.current);
    setSource("demo");

    withTimeout(loaderRef.current(), timeoutMs).then(
      (v) => {
        if (cancelled) return;
        setValue(v);
        setSource("live");
        setState("ready");
        if (panelId) {
          unregister?.();
          unregister = register(panelId, "live");
        }
      },
      () => {
        if (cancelled) return;
        setValue(demoRef.current);
        setSource("demo");
        setState("error");
        if (panelId) {
          unregister?.();
          unregister = register(panelId, "demo");
        }
      },
    );

    return () => {
      cancelled = true;
      unregister?.();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [timeoutMs, panelId, register, nonce, ...deps]);

  // A failure is only worth a per-panel banner when the console is otherwise
  // live (partial outage). In standalone demonstration every panel falls back,
  // so `mode` stays "demo" and `degraded` is false -- no banner noise.
  const degraded = state === "error" && mode === "live";

  return { value, source, state, degraded, reload };
}
