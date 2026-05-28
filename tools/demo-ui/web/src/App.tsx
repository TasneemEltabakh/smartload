import DemoPage from "./pages/Demo";

export default function App() {
  return (
    <div className="layout-single">
      <header className="demo-header">
        <span className="demo-title">SmartLoad</span>
        <span className="demo-subtitle">Developer Demo</span>
      </header>
      <main className="content">
        <DemoPage />
      </main>
    </div>
  );
}
