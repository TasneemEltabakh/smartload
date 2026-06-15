/**
 * tools/demo-ui/web/src/App.tsx
 * ──────────────────────────────
 * Router shell for the SmartLoad benchmark & audit presentation. Strictly
 * read-only: the ResultsProvider loads ONE results bundle (see results/load.ts)
 * and every route renders from it. No run/simulate/mutation routes exist.
 *
 *   /            Overview     — headline KPIs + verdicts across every suite
 *   /benchmarks  Benchmarks   — systems × parameters comparisons (the centerpiece)
 *   /audit       Audit        — control-loop audit & test results
 *   /dashboards  Dashboards   — read-only Grafana embeds
 */

import { Navigate, Route, Routes } from "react-router-dom";

import Layout from "./Layout";
import Audit from "./pages/Audit";
import Benchmarks from "./pages/Benchmarks";
import Dashboards from "./pages/Dashboards";
import Overview from "./pages/Overview";
import { ResultsProvider } from "./state/ResultsContext";

export default function App() {
  return (
    <ResultsProvider>
      <Routes>
        <Route path="/" element={<Layout />}>
          <Route index element={<Overview />} />
          <Route path="benchmarks" element={<Benchmarks />} />
          {/* legacy path */}
          <Route path="comparisons" element={<Navigate to="/benchmarks" replace />} />
          <Route path="audit" element={<Audit />} />
          <Route path="dashboards" element={<Dashboards />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </ResultsProvider>
  );
}
