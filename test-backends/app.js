const express = require("express");
const os = require("os");

const { loadConfig } = require("./lib/config");
const { createServiceTimeModel } = require("./lib/service_time");
const { createPool } = require("./lib/pool");

const app = express();

const SERVER_ID = process.env.SERVER_ID || os.hostname();
const PORT = process.env.PORT || 8080;

// ── Closed-loop backend model ────────────────────────────────────────────────
//
// Each replica is a small M/G/c queue rather than a constant-delay echo:
//   * `WORKERS` concurrent service slots; arrivals beyond that wait in a
//     bounded FIFO queue (`QUEUE_MAX`); arrivals beyond that are shed (503).
//   * per-request service time is drawn from a seeded distribution
//     (constant | exponential | lognormal).
// Observed latency = queue-wait + service-time, so it rises with load — which
// is the whole point: it gives a load balancer something real to optimise.
//
// Backward-compat: the legacy knobs still work. `RESPONSE_DELAY_MS` seeds the
// service-time mean; `SLOW_HOSTNAME`/`SLOW_DELAY_MS` add a per-replica offset
// to the service time; `FAIL_ALL`/`FAIL_HEALTH` are unchanged. See lib/config.js.
const cfg = loadConfig(process.env, { hostname: os.hostname() });

const model = createServiceTimeModel({
  dist: cfg.dist,
  meanMs: cfg.meanMs,
  cv: cfg.cv,
  seed: cfg.seed,
});

const pool = createPool({ workers: cfg.workers, queueMax: cfg.queueMax });

// Deterministic per-request additions to the sampled service time: the slow-
// host offset (fixed at startup) plus a runtime-injected delay.
const slowExtraMs = cfg.isSlowHost ? cfg.slowDelayMs : 0;

// ── Runtime-overridable delay ────────────────────────────────────────────────
//
// POST /_admin/delay {"ms": 200} adds 200 ms to this replica's service time on
// every subsequent `/`. Used by the baseline-vs-smartload harness to inject a
// slow-response anomaly without restarting the container. In the closed-loop
// model this also raises utilisation, so a large enough delay builds a queue
// (and, past QUEUE_MAX, sheds 503 — the opt-in saturation regime). POST
// {"ms": 0} clears the override.
let runtimeDelayMs = 0;

function staticBaselineMs() {
  // Deterministic part of the per-request service-time mean.
  return cfg.meanMs + slowExtraMs;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function yieldImmediate() {
  return new Promise((resolve) => setImmediate(resolve));
}

// Optional CPU-bound mode (CPU_BOUND=true): burn real CPU for ~targetMs instead
// of sleeping, yielding between chunks so other pooled requests still progress.
// NOTE: Node is single-threaded, so concurrent busy work shares one core and
// effective concurrency falls below WORKERS under load. The modeled-sleep path
// is the reproducible measurement path; this knob is a realism sanity-check.
async function busyFor(targetMs) {
  const end = Date.now() + targetMs;
  let acc = 0;
  while (Date.now() < end) {
    for (let i = 0; i < 100000; i += 1) {
      acc += Math.sqrt(acc + i + 1);
    }
    await yieldImmediate();
  }
  return acc;
}

async function serve(serviceMs) {
  if (serviceMs <= 0) {
    return;
  }
  if (cfg.cpuBound) {
    await busyFor(serviceMs);
  } else {
    await sleep(serviceMs);
  }
}

/*
---------------------------------------------------
Request Logging Middleware
Logs every request in structured JSON format. The recorded latency_ms now
captures queue-wait + service-time end to end.
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
      client_ip: req.ip,
    };

    console.log(JSON.stringify(log));
  });

  next();
});

app.use(express.json());

/*
---------------------------------------------------
Main Route — admitted through the worker pool / queue
---------------------------------------------------
*/
app.get("/", async (req, res) => {
  const grant = await pool.acquire();
  if (!grant.ok) {
    // Queue is full — shed load. This is what lets error_rate rise under
    // genuine overload (the opt-in saturation regime).
    return res.status(503).json({ status: "overloaded", server: SERVER_ID });
  }

  try {
    const serviceMs = model.sample() + slowExtraMs + runtimeDelayMs;
    await serve(serviceMs);

    if (cfg.failAll) {
      return res.status(503).json({ status: "unavailable", server: SERVER_ID });
    }

    return res.send(`Hello from ${SERVER_ID}`);
  } finally {
    grant.release();
  }
});

/*
---------------------------------------------------
Runtime delay override
---------------------------------------------------
*/
function delayState() {
  const baseline = staticBaselineMs();
  return {
    server: SERVER_ID,
    is_slow_host: cfg.isSlowHost,
    static_baseline_ms: baseline,
    runtime_override_ms: runtimeDelayMs,
    effective_ms: baseline + runtimeDelayMs,
    in_flight: pool.inFlight,
    queue_depth: pool.queueDepth,
    shed: pool.shed,
  };
}

app.post("/_admin/delay", (req, res) => {
  const next = Number(req.body && req.body.ms);
  if (!Number.isFinite(next) || next < 0 || next > 10000) {
    return res.status(400).json({ error: "ms must be a number in [0, 10000]" });
  }
  runtimeDelayMs = next;
  res.json(delayState());
});

app.get("/_admin/delay", (req, res) => {
  res.json(delayState());
});

/*
---------------------------------------------------
Congestion stats — live worker-pool / queue snapshot
---------------------------------------------------
*/
app.get("/_admin/stats", (req, res) => {
  res.json({
    server: SERVER_ID,
    in_flight: pool.inFlight,
    queue_depth: pool.queueDepth,
    workers: pool.workers,
    queue_max: pool.queueMax,
    accepted: pool.accepted,
    shed: pool.shed,
    total: pool.total,
    service_dist: model.dist,
    service_cv: model.cv,
    service_mean_ms: cfg.meanMs,
    effective_mean_ms: staticBaselineMs() + runtimeDelayMs,
    runtime_override_ms: runtimeDelayMs,
    is_slow_host: cfg.isSlowHost,
    cpu_bound: cfg.cpuBound,
  });
});

/*
---------------------------------------------------
Health Check Endpoint — fast and NOT pooled, so saturation of `/` can never
flap the container's health (the Compose healthcheck targets /health).
---------------------------------------------------
*/
app.get("/health", (req, res) => {
  if (cfg.failHealth || cfg.failAll) {
    return res.status(503).json({ status: "unhealthy", server: SERVER_ID });
  }

  res.json({ status: "healthy", server: SERVER_ID });
});

/*
---------------------------------------------------
Start Server
---------------------------------------------------
*/
app.listen(PORT, () => {
  console.log(
    JSON.stringify({
      event: "server_started",
      server: SERVER_ID,
      port: PORT,
      model: {
        dist: model.dist,
        mean_ms: cfg.meanMs,
        cv: model.cv,
        workers: cfg.workers,
        queue_max: cfg.queueMax,
        cpu_bound: cfg.cpuBound,
        is_slow_host: cfg.isSlowHost,
        slow_delay_ms: slowExtraMs,
      },
      timestamp: new Date().toISOString(),
    })
  );
});
