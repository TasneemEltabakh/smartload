"use strict";

const { mulberry32, makeGaussian } = require("./prng");

// Builds a per-request service-time sampler (milliseconds) for an M/G/c
// backend. The distribution is what gives routing something to exploit:
// constant service time makes round-robin near-optimal, whereas a heavy-tailed
// (lognormal) service time rewards load-aware routing.
//
//   dist     "constant" | "exponential" | "lognormal" (default)
//   meanMs   target mean service time E[S]
//   cv       coefficient of variation (lognormal only; exponential is fixed
//            at 1, constant at 0)
//   seed     PRNG seed for reproducibility
//
// Returns { dist, mean, cv, sample() }.
function createServiceTimeModel({ dist, meanMs, cv, seed }) {
  const mean = Math.max(0, Number(meanMs) || 0);
  const coeffVar = Math.max(0, Number(cv) || 0);
  const rng = mulberry32(seed >>> 0);

  // Degenerate mean — every distribution collapses to zero service time.
  if (mean === 0) {
    return { dist: "constant", mean: 0, cv: 0, sample: () => 0 };
  }

  if (dist === "constant") {
    return { dist: "constant", mean, cv: 0, sample: () => mean };
  }

  if (dist === "exponential") {
    // Inverse-CDF sampling; CV is intrinsically 1 regardless of the cv knob.
    return {
      dist: "exponential",
      mean,
      cv: 1,
      sample: () => -mean * Math.log(1 - rng()),
    };
  }

  // Lognormal (default). Solve (mu, sigma) so that E[S] = mean and CV[S] = cv.
  // With cv = 0 this degenerates cleanly to a constant `mean`.
  const gaussian = makeGaussian(rng);
  const sigma2 = Math.log(1 + coeffVar * coeffVar);
  const sigma = Math.sqrt(sigma2);
  const mu = Math.log(mean) - sigma2 / 2;
  return {
    dist: "lognormal",
    mean,
    cv: coeffVar,
    sample: () => Math.exp(mu + sigma * gaussian()),
  };
}

module.exports = { createServiceTimeModel };
