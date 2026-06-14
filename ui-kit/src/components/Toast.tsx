/* ============================================================================
   Toast / Toaster -- transient notices
   ----------------------------------------------------------------------------
   <Toaster> mounts once near the app root and exposes push() through context.
   useToast() returns the pusher. A critical tone is reserved for safety events
   (safe_mode engaged).
   ============================================================================ */
import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react";

export type ToastTone = "info" | "ok" | "crit";

export interface ToastInput {
  title: ReactNode;
  /** Mono detail line. */
  detail?: ReactNode;
  tone?: ToastTone;
  /** Auto-dismiss after this many ms (default 3200). */
  ttl?: number;
}

interface ToastRecord extends ToastInput {
  id: number;
}

interface ToastApi {
  push: (toast: ToastInput) => void;
}

const ToastContext = createContext<ToastApi | null>(null);

let counter = 0;

const accent: Record<ToastTone, string> = {
  info: "var(--sl-mint)",
  ok: "var(--sl-ok)",
  crit: "var(--sl-crit)",
};

export function Toaster({ children }: { children?: ReactNode }) {
  const [items, setItems] = useState<ToastRecord[]>([]);

  const push = useCallback((toast: ToastInput) => {
    const id = ++counter;
    setItems((prev) => [...prev, { ...toast, id }]);
    window.setTimeout(() => {
      setItems((prev) => prev.filter((t) => t.id !== id));
    }, toast.ttl ?? 3200);
  }, []);

  const api = useMemo<ToastApi>(() => ({ push }), [push]);

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div
        style={{
          position: "fixed",
          bottom: 24,
          left: "50%",
          transform: "translateX(-50%)",
          zIndex: 90,
          display: "flex",
          flexDirection: "column",
          gap: 10,
          alignItems: "center",
          pointerEvents: "none",
        }}
      >
        {items.map((t) => {
          const tone = t.tone ?? "info";
          return (
            <div
              key={t.id}
              role="status"
              style={{
                background: "var(--sl-text)",
                color: "var(--sl-surface)",
                borderRadius: "var(--sl-radius-md)",
                padding: "13px 18px",
                boxShadow: "var(--sl-shadow-2)",
                display: "flex",
                alignItems: "center",
                gap: 12,
                maxWidth: 560,
                pointerEvents: "auto",
              }}
            >
              <span
                style={{
                  width: 8,
                  height: 8,
                  borderRadius: "50%",
                  background: accent[tone],
                  flex: "0 0 auto",
                  boxShadow: `0 0 8px ${accent[tone]}`,
                }}
              />
              <div>
                <div style={{ fontSize: 13, fontWeight: 600 }}>{t.title}</div>
                {t.detail != null ? (
                  <div
                    style={{
                      fontSize: 11.5,
                      fontFamily: "var(--sl-font-mono)",
                      opacity: 0.75,
                      marginTop: 1,
                    }}
                  >
                    {t.detail}
                  </div>
                ) : null}
              </div>
            </div>
          );
        })}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastApi {
  const ctx = useContext(ToastContext);
  if (!ctx) {
    // No-op fallback so callers outside a Toaster don't crash.
    return { push: () => undefined };
  }
  return ctx;
}
