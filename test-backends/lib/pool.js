"use strict";

// A worker-pool + bounded-FIFO-queue concurrency limiter — the piece that
// turns the backend from open-loop (latency independent of load) into a
// closed-loop M/G/c queue (observed latency = queue-wait + service-time).
//
//   workers   number of concurrent service slots (c)
//   queueMax  FIFO capacity for requests that arrive while every slot is busy
//
// acquire() resolves to a grant once a slot is free:
//   { ok: true, release() }  — caller serves the request, then calls release()
//   { ok: false }            — queue is full; caller should shed (HTTP 503)
//
// Releasing a slot hands it directly to the head of the queue (when one is
// waiting), so the in-flight count never exceeds `workers` and queued requests
// are served strictly FIFO.
function createPool({ workers, queueMax }) {
  const maxWorkers = Math.max(1, Math.floor(Number(workers) || 1));
  const maxQueue = Math.max(0, Math.floor(Number(queueMax) || 0));

  let inFlight = 0;
  const waiters = []; // resolver thunks, FIFO
  let accepted = 0;
  let shed = 0;
  let total = 0;

  function makeGrant() {
    let released = false;
    return {
      ok: true,
      release() {
        if (released) {
          return; // idempotent — a stray double-release can't corrupt state
        }
        released = true;
        const next = waiters.shift();
        if (next) {
          next(); // transfer this slot to the head waiter; inFlight unchanged
        } else {
          inFlight -= 1;
        }
      },
    };
  }

  function acquire() {
    total += 1;
    if (inFlight < maxWorkers) {
      inFlight += 1;
      accepted += 1;
      return Promise.resolve(makeGrant());
    }
    if (waiters.length < maxQueue) {
      accepted += 1;
      return new Promise((resolve) => {
        waiters.push(() => resolve(makeGrant()));
      });
    }
    shed += 1;
    return Promise.resolve({ ok: false });
  }

  return {
    acquire,
    get inFlight() {
      return inFlight;
    },
    get queueDepth() {
      return waiters.length;
    },
    get accepted() {
      return accepted;
    },
    get shed() {
      return shed;
    },
    get total() {
      return total;
    },
    get workers() {
      return maxWorkers;
    },
    get queueMax() {
      return maxQueue;
    },
  };
}

module.exports = { createPool };
