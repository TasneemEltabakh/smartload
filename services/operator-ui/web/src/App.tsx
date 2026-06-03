import { useEffect, useState } from "react";
import { NavLink, Route, Routes } from "react-router-dom";
import {
  LayoutDashboard,
  Cpu,
  ShieldCheck,
  ScrollText,
  Play,
  ExternalLink,
} from "lucide-react";

import ActionsPage from "./pages/Actions";
import AuditPage from "./pages/Audit";
import EngineDetailPage from "./pages/EngineDetail";
import HomePage from "./pages/Home";
import LiveEnginesPage from "./pages/LiveEngines";
import PolicyPage from "./pages/Policy";
import { api } from "./api";

type ConnState = "ok" | "warn" | "bad";

const CONN_POLL_MS = 15_000;

export default function App() {
  const [conn, setConn] = useState<ConnState>("warn");

  useEffect(() => {
    let cancelled = false;
    async function tick() {
      try {
        const r = await api.health();
        if (cancelled) return;
        setConn(r.all_ok ? "ok" : "warn");
      } catch {
        if (!cancelled) setConn("bad");
      }
    }
    tick();
    const id = setInterval(tick, CONN_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  const connLabel =
    conn === "ok" ? "Live connected" : conn === "warn" ? "Degraded" : "Disconnected";

  return (
    <div className="layout">
      <aside className="sidebar">
        <h1>SmartLoad</h1>
        <div className="tagline">Operator UI</div>
        <nav className="nav">
          <NavLink to="/" end>
            <LayoutDashboard size={16} strokeWidth={1.75} /> Home
          </NavLink>
          <NavLink to="/engines">
            <Cpu size={16} strokeWidth={1.75} /> Engines
          </NavLink>
          <NavLink to="/policy">
            <ShieldCheck size={16} strokeWidth={1.75} /> Policy
          </NavLink>
          <NavLink to="/audit">
            <ScrollText size={16} strokeWidth={1.75} /> Audit
          </NavLink>
          <NavLink to="/actions">
            <Play size={16} strokeWidth={1.75} /> Actions
          </NavLink>
          <a href="/api/docs" target="_blank" rel="noreferrer">
            <ExternalLink size={16} strokeWidth={1.75} /> API docs
          </a>
        </nav>

        <div className="sidebar-footer">
          <div className={`conn ${conn === "ok" ? "" : conn}`}>
            <span className="dot" />
            <span>{connLabel}</span>
          </div>
          <div className="operator">
            <div className="operator-avatar">OP</div>
            <div>
              <div className="operator-name">operator</div>
              <div className="operator-role">on-call</div>
            </div>
          </div>
        </div>
      </aside>

      <main className="content">
        <Routes>
          <Route path="/" element={<HomePage />} />
          <Route path="/engines" element={<LiveEnginesPage />} />
          <Route path="/engines/:service" element={<EngineDetailPage />} />
          <Route path="/policy" element={<PolicyPage />} />
          <Route path="/audit" element={<AuditPage />} />
          <Route path="/actions" element={<ActionsPage />} />
        </Routes>
      </main>
    </div>
  );
}
