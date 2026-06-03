const express = require("express");
const os = require("os");

const app = express();

const SERVER_ID = process.env.SERVER_ID || os.hostname();
const PORT = process.env.PORT || 8080;

// ── Static latency knobs ────────────────────────────────────────────────────
//
// RESPONSE_DELAY_MS         baseline per-request latency, applied to every
//                           backend instance (compose sets the same value
//                           on the scaled service)
// SLOW_HOSTNAME             if my os.hostname() matches this exact string,
//                           apply SLOW_DELAY_MS *additionally* on every
//                           request. Lets a single backend in a 5-replica
//                           pool become deterministically slow without
//                           splitting the compose service into 5 explicit
//                           ones. Used by experiments/baseline-vs-smartload/
//                           to introduce backend heterogeneity for #148.
// SLOW_DELAY_MS             additional latency when SLOW_HOSTNAME matches.
// FAIL_HEALTH / FAIL_ALL    legacy boolean toggles for the demo BFF's chaos
//                           injection — kept for back-compat.
const RESPONSE_DELAY_MS = Number(process.env.RESPONSE_DELAY_MS || 0);
const SLOW_HOSTNAME = process.env.SLOW_HOSTNAME || "";
const SLOW_DELAY_MS = Number(process.env.SLOW_DELAY_MS || 0);
const FAIL_HEALTH = process.env.FAIL_HEALTH === "true";
const FAIL_ALL = process.env.FAIL_ALL === "true";

const IS_SLOW = SLOW_HOSTNAME && os.hostname() === SLOW_HOSTNAME;
const STATIC_BASELINE_DELAY_MS = RESPONSE_DELAY_MS + (IS_SLOW ? SLOW_DELAY_MS : 0);

// ── Runtime-overridable delay ────────────────────────────────────────────────
//
// POST /_admin/delay {"ms": 200} sets `runtimeDelayMs` for this instance;
// every subsequent `/` and `/health` request adds it to the static baseline.
// Used by the baseline-vs-smartload benchmark harness to inject a slow-
// response anomaly without restarting the container (and without taking
// the connection down, so NGINX's passive health checks don't catch it —
// which is exactly the case where SmartLoad's reactive lb-sidecar wins).
//
// POST {"ms": 0} clears the override; the instance returns to baseline.
let runtimeDelayMs = 0;
function currentDelayMs() {
  return STATIC_BASELINE_DELAY_MS + runtimeDelayMs;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/*
---------------------------------------------------
Request Logging Middleware
Logs every request in structured JSON format
---------------------------------------------------
*/
app.use((req, res, next) => {
  const start = Date.now();

  res.on("finish", () => {
    const log = {
      timestamp: new Date().toISOString(),
      service: "backend",
      server: SERVER_ID,
      method: req.method,
      path: req.originalUrl,
      status: res.statusCode,
      latency_ms: Date.now() - start,
      client_ip: req.ip
    };

    console.log(JSON.stringify(log));
  });

  next();
});

/*
---------------------------------------------------
Main Route
---------------------------------------------------
*/
app.get("/", async (req, res) => {

  const d = currentDelayMs();
  if (d > 0) {
    await sleep(d);
  }

  if (FAIL_ALL) {
    return res.status(503).json({
      status: "unavailable",
      server: SERVER_ID
    });
  }

  res.send(`Hello from ${SERVER_ID}`);
});

/*
---------------------------------------------------
Runtime delay override
---------------------------------------------------
*/
app.use(express.json());
app.post("/_admin/delay", (req, res) => {
  const next = Number(req.body && req.body.ms);
  if (!Number.isFinite(next) || next < 0 || next > 10000) {
    return res.status(400).json({ error: "ms must be a number in [0, 10000]" });
  }
  runtimeDelayMs = next;
  res.json({
    server: SERVER_ID,
    is_slow_host: IS_SLOW,
    static_baseline_ms: STATIC_BASELINE_DELAY_MS,
    runtime_override_ms: runtimeDelayMs,
    effective_ms: currentDelayMs(),
  });
});

app.get("/_admin/delay", (req, res) => {
  res.json({
    server: SERVER_ID,
    is_slow_host: IS_SLOW,
    static_baseline_ms: STATIC_BASELINE_DELAY_MS,
    runtime_override_ms: runtimeDelayMs,
    effective_ms: currentDelayMs(),
  });
});


/*
---------------------------------------------------
Health Check Endpoint
---------------------------------------------------
*/
app.get("/health", async (req, res) => {

  const d = currentDelayMs();
  if (d > 0) {
    await sleep(d);
  }

  if (FAIL_HEALTH || FAIL_ALL) {
    return res.status(503).json({
      status: "unhealthy",
      server: SERVER_ID
    });
  }

  res.json({
    status: "healthy",
    server: SERVER_ID
  });
});

/*
---------------------------------------------------
Start Server
---------------------------------------------------
*/
app.listen(PORT, () => {

  console.log(JSON.stringify({
    event: "server_started",
    server: SERVER_ID,
    port: PORT,
    timestamp: new Date().toISOString()
  }));

});