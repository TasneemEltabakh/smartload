/**
 * tools/demo-ui/web/src/App.tsx
 * ──────────────────────────────
 * Router shell for the SmartLoad Dev Console. The DemoStateProvider hoists
 * polling (state / metrics / services) + SSE + toast so route changes don't
 * reset subscriptions. Each route renders inside the Layout's <Outlet />.
 *
 *   /            Dashboard   — stack health + live session metrics
 *   /benchmarks  Benchmarks  — adaptive-bench + baseline results (charts)
 *   /run         Run         — one-click load profiles + live monitor
 *   /controls    Controls    — algorithm / scenarios / manual fault injection
 *   /feed        Live Feed   — SSE decision-plane stream
 */

import { Navigate, Route, Routes } from "react-router-dom";

import Layout from "./Layout";
import Benchmarks from "./pages/Benchmarks";
import Controls from "./pages/Controls";
import Dashboard from "./pages/Dashboard";
import Feed from "./pages/Feed";
import Run from "./pages/Run";
import { DemoStateProvider } from "./state/DemoStateContext";


export default function App() {
  return (
    <DemoStateProvider>
      <Routes>
        <Route path="/" element={<Layout />}>
          <Route index element={<Dashboard />} />
          <Route path="benchmarks" element={<Benchmarks />} />
          <Route path="run" element={<Run />} />
          <Route path="controls" element={<Controls />} />
          <Route path="feed" element={<Feed />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </DemoStateProvider>
  );
}
