// ============================================================================
// loadWithFallback -- live-or-sample data resolution
// ----------------------------------------------------------------------------
// Wraps an API call with a timeout and a sample fallback. Returns the live
// value and source="live" on success, or the supplied sample value and
// source="sample" on error or timeout. The Flightdeck uses the source to show
// a "sample data" indicator in the Topbar when any panel is running offline.
// ============================================================================

export type DataSource = "live" | "sample";

export interface Loaded<T> {
  value: T;
  source: DataSource;
}

const DEFAULT_TIMEOUT_MS = 4000;

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

export async function loadWithFallback<T>(
  call: () => Promise<T>,
  sample: T,
  timeoutMs: number = DEFAULT_TIMEOUT_MS,
): Promise<Loaded<T>> {
  try {
    const value = await withTimeout(call(), timeoutMs);
    return { value, source: "live" };
  } catch {
    return { value: sample, source: "sample" };
  }
}
