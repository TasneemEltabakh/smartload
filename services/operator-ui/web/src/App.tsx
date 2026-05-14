import { NavLink, Route, Routes } from "react-router-dom";

import HomePage from "./pages/Home";
import PolicyPage from "./pages/Policy";

export default function App() {
  return (
    <div className="layout">
      <aside className="sidebar">
        <h1>SmartLoad</h1>
        <div className="tagline">Operator UI</div>
        <nav className="nav">
          <NavLink to="/" end>Home</NavLink>
          <NavLink to="/policy">Policy</NavLink>
          <a href="/api/docs" target="_blank" rel="noreferrer">API docs</a>
        </nav>
      </aside>
      <main className="content">
        <Routes>
          <Route path="/" element={<HomePage />} />
          <Route path="/policy" element={<PolicyPage />} />
        </Routes>
      </main>
    </div>
  );
}
