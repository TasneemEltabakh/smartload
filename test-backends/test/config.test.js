"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const { loadConfig } = require("../lib/config");

test("defaults match the documented closed-loop values", () => {
  const c = loadConfig({}, { hostname: "host-a" });
  assert.equal(c.meanMs, 20);
  assert.equal(c.dist, "lognormal");
  assert.equal(c.cv, 1);
  assert.equal(c.workers, 2);
  assert.equal(c.queueMax, 64);
  assert.equal(c.cpuBound, false);
});

test("SERVICE_MEAN_MS wins, else inherits RESPONSE_DELAY_MS, else 20", () => {
  assert.equal(loadConfig({ SERVICE_MEAN_MS: "35" }, {}).meanMs, 35);
  assert.equal(loadConfig({ RESPONSE_DELAY_MS: "12" }, {}).meanMs, 12);
  assert.equal(loadConfig({ RESPONSE_DELAY_MS: "0" }, {}).meanMs, 20);
  assert.equal(loadConfig({}, {}).meanMs, 20);
  assert.equal(
    loadConfig({ SERVICE_MEAN_MS: "50", RESPONSE_DELAY_MS: "12" }, {}).meanMs,
    50
  );
});

test("unknown SERVICE_DIST falls back to lognormal; known ones normalise", () => {
  assert.equal(loadConfig({ SERVICE_DIST: "weird" }, {}).dist, "lognormal");
  assert.equal(loadConfig({ SERVICE_DIST: "Constant" }, {}).dist, "constant");
  assert.equal(loadConfig({ SERVICE_DIST: "EXPONENTIAL" }, {}).dist, "exponential");
});

test("per-replica seed varies with the host id but is reproducible", () => {
  const env = { SERVICE_SEED: "1337" };
  const a = loadConfig(env, { hostname: "rep-1" });
  const b = loadConfig(env, { hostname: "rep-2" });
  const a2 = loadConfig(env, { hostname: "rep-1" });
  assert.notEqual(a.seed, b.seed);
  assert.equal(a.seed, a2.seed);
});

test("isSlowHost matches SLOW_HOSTNAME against the hostname", () => {
  const slow = loadConfig(
    { SLOW_HOSTNAME: "rep-1", SLOW_DELAY_MS: "15" },
    { hostname: "rep-1" }
  );
  assert.equal(slow.isSlowHost, true);
  assert.equal(slow.slowDelayMs, 15);

  const fast = loadConfig(
    { SLOW_HOSTNAME: "rep-1", SLOW_DELAY_MS: "15" },
    { hostname: "rep-2" }
  );
  assert.equal(fast.isSlowHost, false);
});

test("chaos toggles parse booleans", () => {
  const c = loadConfig({ FAIL_ALL: "true", FAIL_HEALTH: "true" }, {});
  assert.equal(c.failAll, true);
  assert.equal(c.failHealth, true);
  assert.equal(loadConfig({ FAIL_ALL: "false" }, {}).failAll, false);
});
