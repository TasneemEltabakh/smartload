/**
 * tools/demo-ui/web/src/App.tsx
 * ──────────────────────────────
 * Router shell. The DemoStateProvider hoists polling + SSE + toast so
 * route changes don't reset subscriptions. Each route renders inside
 * the Layout's <Outlet />.
 */

import { Navigate, Route, Routes } from "react-router-dom";

import Layout from "./Layout";
import Benchmark from "./pages/Benchmark";
import Controls from "./pages/Controls";
import Feed from "./pages/Feed";
import Overview from "./pages/Overview";
import { DemoStateProvider } from "./state/DemoStateContext";


export default function App() {
  return (
    <DemoStateProvider>
      <Routes>
        <Route path="/" element={<Layout />}>
          <Route index element={<Overview />} />
          <Route path="controls" element={<Controls />} />
          <Route path="feed" element={<Feed />} />
          <Route path="benchmark" element={<Benchmark />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </DemoStateProvider>
  );
}
