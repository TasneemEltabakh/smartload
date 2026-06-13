"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const { createPool } = require("../lib/pool");

test("admits up to WORKERS concurrently, queues the rest", async () => {
  const pool = createPool({ workers: 2, queueMax: 4 });

  const g1 = await pool.acquire();
  const g2 = await pool.acquire();
  assert.equal(g1.ok, true);
  assert.equal(g2.ok, true);
  assert.equal(pool.inFlight, 2);

  // Third acquire must queue. Don't await — it won't resolve until a release.
  const p3 = pool.acquire();
  assert.equal(pool.queueDepth, 1);
  assert.equal(pool.inFlight, 2);

  // Free a slot — the queued waiter resolves and inFlight stays at WORKERS.
  g1.release();
  const g3 = await p3;
  assert.equal(g3.ok, true);
  assert.equal(pool.queueDepth, 0);
  assert.equal(pool.inFlight, 2);

  g2.release();
  g3.release();
  assert.equal(pool.inFlight, 0);
});

test("serves queued waiters in FIFO order", async () => {
  const pool = createPool({ workers: 1, queueMax: 8 });

  const g0 = await pool.acquire(); // occupies the only slot
  assert.equal(pool.inFlight, 1);

  const resolved = [];
  const p1 = pool.acquire().then((g) => {
    resolved.push(1);
    return g;
  });
  const p2 = pool.acquire().then((g) => {
    resolved.push(2);
    return g;
  });
  const p3 = pool.acquire().then((g) => {
    resolved.push(3);
    return g;
  });
  assert.equal(pool.queueDepth, 3);

  g0.release();
  const g1 = await p1;
  assert.deepEqual(resolved, [1]);

  g1.release();
  const g2 = await p2;
  assert.deepEqual(resolved, [1, 2]);

  g2.release();
  const g3 = await p3;
  assert.deepEqual(resolved, [1, 2, 3]);

  g3.release();
  assert.equal(pool.inFlight, 0);
});

test("sheds requests beyond QUEUE_MAX", async () => {
  const pool = createPool({ workers: 1, queueMax: 2 });

  const g0 = await pool.acquire(); // slot
  const p1 = pool.acquire(); // queued (1)
  const p2 = pool.acquire(); // queued (2) — queue now full
  assert.equal(pool.queueDepth, 2);

  const shedGrant = await pool.acquire(); // queue full → shed
  assert.equal(shedGrant.ok, false);
  assert.equal(pool.shed, 1);
  assert.equal(pool.accepted, 3); // g0 + p1 + p2
  assert.equal(pool.total, 4);

  g0.release();
  (await p1).release();
  (await p2).release();
  assert.equal(pool.inFlight, 0);
});

test("total always equals accepted + shed", async () => {
  const pool = createPool({ workers: 2, queueMax: 1 });

  const a = await pool.acquire();
  const b = await pool.acquire();
  const pq = pool.acquire(); // queued
  const s1 = await pool.acquire(); // shed
  const s2 = await pool.acquire(); // shed

  assert.equal(s1.ok, false);
  assert.equal(s2.ok, false);
  assert.equal(pool.accepted, 3);
  assert.equal(pool.shed, 2);
  assert.equal(pool.total, pool.accepted + pool.shed);

  a.release();
  (await pq).release();
  b.release();
  assert.equal(pool.inFlight, 0);
});

test("double release is idempotent and cannot corrupt inFlight", async () => {
  const pool = createPool({ workers: 1, queueMax: 0 });

  const g = await pool.acquire();
  assert.equal(pool.inFlight, 1);
  g.release();
  assert.equal(pool.inFlight, 0);
  g.release(); // no-op
  assert.equal(pool.inFlight, 0);

  // queueMax 0 means no queueing: a second concurrent acquire sheds.
  const g2 = await pool.acquire();
  assert.equal(g2.ok, true);
  const g3 = await pool.acquire();
  assert.equal(g3.ok, false);
  g2.release();
  assert.equal(pool.inFlight, 0);
});
