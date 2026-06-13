"use strict";

// Seeded, dependency-free PRNG utilities for the closed-loop backend's
// service-time model. `mulberry32` gives a fast, well-distributed 32-bit
// stream; `makeGaussian` draws standard-normal deviates (Marsaglia polar
// method) for the lognormal service-time distribution. Seeding every stream
// is what makes a benchmark run reproducible.

// Returns a function producing floats in [0, 1) from a 32-bit integer seed.
function mulberry32(seed) {
  let a = seed >>> 0;
  return function next() {
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// Wraps a uniform rng() into a standard-normal sampler. Uses the Marsaglia
// polar method and caches the second deviate, so a pair of normals costs one
// pair of accepted uniforms. Deterministic given a deterministic rng().
function makeGaussian(rng) {
  let spare = null;
  return function gaussian() {
    if (spare !== null) {
      const value = spare;
      spare = null;
      return value;
    }
    let u;
    let v;
    let s;
    do {
      u = rng() * 2 - 1;
      v = rng() * 2 - 1;
      s = u * u + v * v;
    } while (s >= 1 || s === 0);
    const mul = Math.sqrt((-2 * Math.log(s)) / s);
    spare = v * mul;
    return u * mul;
  };
}

module.exports = { mulberry32, makeGaussian };
