"use strict";

// Centralised env parsing + backward-compatible defaulting for the backend.
// Kept free of I/O so it can be unit-tested by passing a plain env object and
// an explicit hostname.

function num(value, fallback) {
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
}

function bool(value) {
  return String(value || "").toLowerCase() === "true";
}

// Deterministic 32-bit FNV-1a hash — varies the PRNG seed per replica so the
// five containers that share one Compose env still draw independent (but
// reproducible) service-time streams.
function hashStringToInt(text) {
  let h = 0x811c9dc5;
  for (let i = 0; i < text.length; i += 1) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return h >>> 0;
}

function loadConfig(env = process.env, opts = {}) {
  const hostname = opts.hostname || "";
  const serverId = env.SERVER_ID || hostname || "";

  // Service-time mean: an explicit SERVICE_MEAN_MS wins; otherwise inherit the
  // legacy RESPONSE_DELAY_MS when it was set; otherwise the closed-loop default.
  const legacyDelay = num(env.RESPONSE_DELAY_MS, 0);
  let meanMs;
  if (env.SERVICE_MEAN_MS !== undefined && env.SERVICE_MEAN_MS !== "") {
    meanMs = Math.max(0, num(env.SERVICE_MEAN_MS, 20));
  } else if (legacyDelay > 0) {
    meanMs = legacyDelay;
  } else {
    meanMs = 20;
  }

  const distRaw = String(env.SERVICE_DIST || "lognormal").toLowerCase();
  const dist = ["constant", "exponential", "lognormal"].includes(distRaw)
    ? distRaw
    : "lognormal";
  const cv = Math.max(0, num(env.SERVICE_CV, 1.0));

  const baseSeed = Math.trunc(num(env.SERVICE_SEED, 1337)) >>> 0;
  const seed = serverId ? (baseSeed ^ hashStringToInt(serverId)) >>> 0 : baseSeed;

  const workers = Math.max(1, Math.trunc(num(env.WORKERS, 2)));
  const queueMax = Math.max(0, Math.trunc(num(env.QUEUE_MAX, 64)));
  const cpuBound = bool(env.CPU_BOUND);

  // Legacy per-replica slowness + chaos toggles (semantics preserved).
  const slowHostname = env.SLOW_HOSTNAME || "";
  const isSlowHost = Boolean(slowHostname) && hostname === slowHostname;
  const slowDelayMs = num(env.SLOW_DELAY_MS, 0);
  const failHealth = bool(env.FAIL_HEALTH);
  const failAll = bool(env.FAIL_ALL);

  return {
    serverId,
    meanMs,
    dist,
    cv,
    seed,
    baseSeed,
    workers,
    queueMax,
    cpuBound,
    slowHostname,
    isSlowHost,
    slowDelayMs,
    failHealth,
    failAll,
    legacyDelay,
  };
}

module.exports = { loadConfig, hashStringToInt, num };
