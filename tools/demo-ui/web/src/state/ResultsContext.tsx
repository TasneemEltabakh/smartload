/**
 * tools/demo-ui/web/src/state/ResultsContext.tsx
 * ────────────────────────────────────────────────
 * Hoists the single results load (see results/load.ts) to the app root so
 * navigating between presentation surfaces doesn't re-fetch. Strictly read-only:
 * there is no polling of a live stack and no action()/mutation helper — this is
 * a presentation of finished results, not a console that steers anything.
 */

import { createContext, useContext, type ReactNode } from "react";

import { useResults, type ResultsLoad } from "../results/load";

const Ctx = createContext<ResultsLoad | null>(null);

export function ResultsProvider({ children }: { children: ReactNode }) {
  const value = useResults();
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useResultsCtx(): ResultsLoad {
  const v = useContext(Ctx);
  if (!v) throw new Error("useResultsCtx must be used inside <ResultsProvider>");
  return v;
}
