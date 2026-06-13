"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const { mulberry32 } = require("../lib/prng");
const { createServiceTimeModel } = require("../lib/service_time");

function sampleStats(model, n) {
  let sum = 0;
  let sumSq = 0;
  for (let i = 0; i < n; i += 1) {
    const x = model.sample();
    sum += x;
    sumSq += x * x;
  }
  const mean = sum / n;
  const variance = sumSq / n - mean * mean;
  return { mean, cv: Math.sqrt(Math.max(0, variance)) / mean };
}

test("mulberry32 is deterministic per seed and bounded in [0, 1)", () => {
  const a = mulberry32(12345);
  const b = mulberry32(12345);
  const seqA = Array.from({ length: 16 }, () => a());
  const seqB = Array.from({ length: 16 }, () => b());
  assert.deepEqual(seqA, seqB);

  const c = mulberry32(54321);
  const seqC = Array.from({ length: 16 }, () => c());
  assert.notDeepEqual(seqA, seqC);

  for (const x of seqA) {
    assert.ok(x >= 0 && x < 1, `out of range: ${x}`);
  }
});

test("constant distribution returns the mean exactly", () => {
  const m = createServiceTimeModel({ dist: "constant", meanMs: 20, cv: 0, seed: 1 });
  assert.equal(m.dist, "constant");
  assert.equal(m.sample(), 20);
  assert.equal(m.sample(), 20);
});

test("exponential sample mean and CV approximate their targets", () => {
  const m = createServiceTimeModel({ dist: "exponential", meanMs: 20, cv: 1, seed: 7 });
  const { mean, cv } = sampleStats(m, 50000);
  assert.ok(Math.abs(mean - 20) / 20 < 0.05, `mean ${mean}`);
  assert.ok(Math.abs(cv - 1) < 0.1, `cv ${cv}`);
});

test("lognormal sample mean and CV approximate their targets", () => {
  const m = createServiceTimeModel({ dist: "lognormal", meanMs: 20, cv: 1.0, seed: 7 });
  const { mean, cv } = sampleStats(m, 50000);
  assert.ok(Math.abs(mean - 20) / 20 < 0.06, `mean ${mean}`);
  assert.ok(Math.abs(cv - 1.0) < 0.2, `cv ${cv}`);
});

test("same seed and config produce an identical sample sequence", () => {
  const cfg = { dist: "lognormal", meanMs: 20, cv: 1, seed: 99 };
  const a = createServiceTimeModel(cfg);
  const b = createServiceTimeModel(cfg);
  const seqA = Array.from({ length: 20 }, () => a.sample());
  const seqB = Array.from({ length: 20 }, () => b.sample());
  assert.deepEqual(seqA, seqB);
});

test("lognormal with cv=0 degenerates to the mean", () => {
  const m = createServiceTimeModel({ dist: "lognormal", meanMs: 15, cv: 0, seed: 3 });
  assert.ok(Math.abs(m.sample() - 15) < 1e-9);
});

test("zero mean yields zero service time for any distribution", () => {
  for (const dist of ["constant", "exponential", "lognormal"]) {
    const m = createServiceTimeModel({ dist, meanMs: 0, cv: 1, seed: 3 });
    assert.equal(m.sample(), 0);
  }
});
