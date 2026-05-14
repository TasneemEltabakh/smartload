import { useEffect, useState } from "react";

import { api, type HealthSummary, type ServiceHealth } from "../api";

const POLL_MS = 10_000;

function classFor(svc: ServiceHealth): string {
  if (svc.status === "ok") return "health-pill ok";
  if (svc.status === "degraded") return "health-pill degraded";
  return "health-pill bad";
}

export default function HomePage() {
  const [data, setData] = useState<HealthSummary | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function tick() {
      try {
        const r = await api.health();
        if (!cancelled) {
          setData(r);
          setError(null);
        }
      } catch (err: any) {
        if (!cancelled) setError(err.message || "health fetch failed");
      }
    }
    tick();
    const id = setInterval(tick, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  return (
    <>
      <div className="card">
        <h2>Service health</h2>
        <div className="meta">
          Polled every {POLL_MS / 1000}s · {data?.all_ok ? "all services healthy" : "one or more services degraded"}
          {error ? ` · last error: ${error}` : null}
        </div>
        <div className="health-grid">
          {data
            ? Object.entries(data.services).map(([name, svc]) => (
                <div key={name} className={classFor(svc)}>
                  <div className="name">{name}</div>
                  <div className="status">
                    {svc.status}
                    {svc.status_code != null ? ` · HTTP ${svc.status_code}` : null}
                  </div>
                  {svc.redis != null || svc.timescaledb != null ? (
                    <div className="status">
                      redis={String(svc.redis)} · timescaledb={String(svc.timescaledb)}
                    </div>
                  ) : null}
                  {svc.error ? <div className="status">{svc.error}</div> : null}
                </div>
              ))
            : <div className="status">loading…</div>}
        </div>
      </div>
    </>
  );
}
