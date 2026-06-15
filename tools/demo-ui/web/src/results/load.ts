/**
 * tools/demo-ui/web/src/results/load.ts
 * ───────────────────────────────────────
 * THE SINGLE DATA-LOADING SEAM. There is exactly one place the presentation UI
 * reads results from, and it is here. To inject the finished VPS run you do ONE
 * of the following — and touch no component:
 *
 *   1. Drop the new results bundle at  public/results/results.json
 *      (it is served at  /results/results.json ), or
 *   2. Point the UI at a read-only endpoint by setting the env var
 *      VITE_RESULTS_URL=https://host/path/results.json  at build/dev time.
 *
 * The fetched JSON is run through the ONE adapter (normalizeBundle) so a missing
 * or half-written file degrades to the PENDING state instead of crashing.
 */

import { useEffect, useState } from "react";

import { emptyBundle, normalizeBundle } from "./adapter";
import type { ResultsBundle } from "./schema";

/** Where results come from. Override at build time with VITE_RESULTS_URL. */
export const RESULTS_URL: string =
  (import.meta as any).env?.VITE_RESULTS_URL || "/results/results.json";

export interface ResultsLoad {
  bundle: ResultsBundle;
  loading: boolean;
  /** Network/parse error message, if the source could not be read at all. */
  error: string | null;
  /** The URL the data was loaded from (shown in the provenance footer). */
  source: string;
}

export async function fetchResults(url: string = RESULTS_URL): Promise<ResultsBundle> {
  const r = await fetch(url, { headers: { Accept: "application/json" }, cache: "no-store" });
  if (!r.ok) throw new Error(`results fetch failed: HTTP ${r.status}`);
  const raw = await r.json();
  return normalizeBundle(raw);
}

/**
 * Hook the whole app uses. Returns a fully-rendered PENDING bundle (never null)
 * so the UI always has a valid contract to draw, even before/without data.
 */
export function useResults(url: string = RESULTS_URL): ResultsLoad {
  const [bundle, setBundle] = useState<ResultsBundle>(() => emptyBundle());
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetchResults(url)
      .then((b) => {
        if (!cancelled) setBundle(b);
      })
      .catch((e) => {
        if (!cancelled) {
          setError(e?.message || "failed to load results");
          setBundle(emptyBundle());
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [url]);

  return { bundle, loading, error, source: url };
}
