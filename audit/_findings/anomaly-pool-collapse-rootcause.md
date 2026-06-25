# Anomaly pool-collapse root-cause: why the live stack 502s 100% under load

Worktree: `G:\smartload-audit-modules` (branch `chore/run-project-20260615`, HEAD `5a28e83` + working tree)
Stack: compose project `smartload`, RUNNING + healthy containers. Reproduced live under a Locust
swarm (50 users, ~90 rps). Read-only investigation; no code, config, or container state changed.
Date: 2026-06-15.

## Status: resolved + regression-guarded (2026-06-25)

Both leverage-1 and leverage-2 fixes from the "Fix options" section below are in
the tree and wired:

- **(1) Allowlist the detector's scoring set** — `build_features_from_rows`
  drops `NON_BACKEND_INSTANCES = {backend_pool, unknown}` before any backend is
  scored (`services/anomaly-detector/runloop.py`).
- **(2) Sidecar rejects non-backend verdicts** — `handle_anomaly` ignores any
  verdict whose translated key is not in the live discovered pool
  (`services/lb-sidecar/runloop.py`), and `app.py` passes that live pool on every
  anomaly message.

Each guard had a per-component unit test, but nothing asserted the two compose so
the *loop* cannot re-close. That gap is now closed by a composed-unit regression
that drives the real shipper, detector, and sidecar functions through a sustained
all-down 502 window and proves no phantom verdict and no phantom exclusion are
ever produced, while a genuine single-backend fault is still excluded:
`tests/unit/pool-collapse/test_pool_collapse_loop.py`. The operational recovery
note at the bottom is retained for any stack still running a pre-fix build.

## TL;DR

A freshly-built stack put under sustained load collapses to a **total, self-sustaining 502 outage**:
every request returns `502 Bad Gateway` even though all five `test-backend` replicas are healthy and
barely loaded (~18% utilisation; a direct hit returns `Hello from ...`).

The cause is a **closed feedback loop across four components**, not a single bug, and not in any AI
model:

1. **NGINX logs the upstream *block name* `backend_pool` as the "upstream" on a 502** (when no live
   server is reachable). The all-down sentinel leaks into telemetry as if it were a backend.
2. **The lb-otel-shipper passes `backend_pool` through unchanged** as the `backend` label
   (`canonicalize_backend`, by design for the "all-down sentinel"), so a metric stream with
   `instance = "backend_pool"` and `error_rate = 1.0` is shipped.
3. **The anomaly-detector scores every distinct `instance` it sees, with no allowlist** against the
   real backend set (`build_features_from_rows`). It therefore scores the phantom `backend_pool` as a
   backend, the error channel trips (`error_rate 1.0 > 0.05`), and it publishes
   `{"backend_id":"backend_pool","status":"unhealthy"}`.
4. **The lb-sidecar translates `backend_pool` → `backend_pool:8080`** and feeds it through the
   exclusion path; combined with real-backend exclusions from the initial load ramp, the upstream
   empties. NGINX then 502s **everything**, which makes every access-log line carry
   `upstream_addr=backend_pool` → `error_rate` stays pinned at 1.0 → the detector keeps flagging.
   The loop never clears.

Once the pool is empty, **no real-backend metrics are ever produced again** (requests never reach a
backend), so the detector only ever sees `backend_pool`, and the two empty-pool guards (sidecar
quorum guard + adapter "last-known-good" net) cannot recover the pool. The on-disk `upstream.conf`
freezes in the all-down state.

This is the *same class* of self-sustaining outage the code comments already warn about (the
v1.0.7an isolation_forest revert, the quorum guard, the adapter safety net) — but those mitigations
all assume the unhealthy verdicts name **real** backends. They do not defend against a verdict whose
`backend_id` is the upstream block name itself.

---

## The loop, in one diagram

```
 load ramp ─► real backends shed 503 / latency channel trips
              │
              ▼
   lb-sidecar excludes real backends (1..N)  ──► upstream shrinks
              │
              ▼
   pool empties ─► NGINX 502s, $upstream_addr = "backend_pool" (block name, no server)
              │
              ▼
   lb-otel-shipper ships  instance="backend_pool", error_rate=1.0   (canonicalize_backend passes it through)
              │
              ▼
   anomaly-detector scores "backend_pool" (no allowlist) ─► error channel: 1.0 > 0.05 ─► UNHEALTHY
              │
              ▼
   lb-sidecar: translate("backend_pool") = "backend_pool:8080" ─► exclusion path
              │
              └──────────────────────────  every request 502s  ◄────────────  (loop closes, irreversible)
```

---

## Evidence (live captures)

**1. 100% of traffic 502s, backends are healthy.**

```
# GET / through the LB
6 sequential requests → 502, 502, 502, 502, 502, 502
Locust aggregate: reqs=9847 fails=9847 (100%) rps=89.6, all "502 Server Error: Bad Gateway"

# direct hit on a backend from inside the network → fine
docker exec smartload-load-balancer-1 wget -qO- http://smartload-test-backend-1:8080/
  → "Hello from b97b7f80980f"   (EXIT=0)
```

**2. The rendered upstream has no live server (flapping, shrinking listing).**

```nginx
# /etc/nginx/conf.d/upstream.conf  (read twice, seconds apart)
upstream backend_pool {
    server smartload-test-backend-1:8080 down;
    server smartload-test-backend-2:8080 down;
    server smartload-test-backend-3:8080 down;
    # all backends temporarily excluded
}
```

**3. The published anomaly event names the upstream block, not a backend.**

```json
// redis PSUBSCRIBE smartload.anomaly
{"source":"anomaly-detector","payload":{
  "backend_id":"backend_pool","status":"unhealthy","score":1.0,
  "model_version":"trend_rule","metric":"error_rate",
  "observed_value":1.0,"threshold":0.05,"severity":"critical"}}
```

**4. The sidecar quorum guard is operating on a phantom key.**

```
[lb-sidecar] anomaly guard: quorum guard: kept last active backend in service (backend_pool:8080)
[lb-sidecar] routing shadow — not applied (0 rankings)
```

`backend_pool:8080` is not a real server — it is `normalize_backend_key(translate_one("backend_pool"))`.

**5. The only instance in the DB is the phantom, at 100% error, with zero real-backend rows.**

```sql
SELECT instance, metric_name, ROUND(AVG(value)::numeric,3), COUNT(*)
FROM metrics WHERE time > NOW() - INTERVAL '90 seconds'
  AND metric_name IN ('error_rate','request_latency_ms') GROUP BY 1,2;

 backend_pool | error_rate         | 1.000 | 8125
 backend_pool | request_latency_ms | 0.000 | 8125
```

No `test-backend-*` rows exist — confirming requests never reach a backend, so the loop cannot
self-heal.

---

## Component-by-component

### A. NGINX — the all-down sentinel leaks as an "upstream"
`services/load-balancer/nginx/nginx.conf:73-74` proxies to `http://$backend_pool`. When the upstream
block has no live `server`, NGINX records the **block name** `backend_pool` as `$upstream_addr` on
the 502. This is the origin of the phantom instance.

### B. lb-otel-shipper — passes the sentinel through by design
`services/lb-otel-shipper/app.py:146-159` `canonicalize_backend()`:
```python
# Handles ... the all-down sentinel (the upstream block name `backend_pool`, no IP → returned unchanged)
if not sep or not _IPV4_RE.match(host):   # not an ip:port (e.g. backend_pool)
    return last
```
The comment shows this is *known and intentional* — but the consequence is that `backend_pool`
becomes a first-class `instance` in the metric stream with a 100% error rate during any 502 window.

### C. anomaly-detector — scores anything that appears, no allowlist
`services/anomaly-detector/runloop.py:146-181` `build_features_from_rows()` builds one
`BackendFeatures` per **distinct `instance` string in the query result**. There is no filter against
the real/known backend set, so `backend_pool` is scored like a backend. The error channel in
`engines/trend_rule/engine.py:145-149` fires unconditionally (no warmup needed):
```python
if err > self.error_rate_threshold:   # 1.0 > 0.05
    return AnomalyScore(..., "unhealthy", metric="error_rate", ...)
```

### D. lb-sidecar — translates the phantom and routes it through exclusion
`services/lb-sidecar/runloop.py:494-512` `handle_anomaly()` does
`normalize_backend_key(registry.translate_one("backend_pool"))` → `backend_pool:8080` and then runs
the exclusion path. The quorum guard `_excluding_would_empty_pool` (`runloop.py:455-478`) only
protects the *last entry of `current_state().upstream_weights`*; it does not reject a verdict whose
key is not a real backend.

### E. nginx adapter — the empty state is self-cementing
`services/shared/lb_adapters/nginx/__init__.py:200-252` `_render_and_reload()` has a "last-known-good"
net: if every weight is excluded it keeps the retained conf **only if it still names a resolvable
active server** (`_retained_conf_still_serviceable`, lines 254-287). Once the file is already
all-down, the retained conf names no active server → the guard falls through and **rewrites all-down
again** (lines 217-230). So the empty pool is re-rendered every cycle rather than recovered.

---

## Why the existing guards don't save it
- The **sidecar quorum guard** and the **adapter safety net** both assume unhealthy verdicts name
  *real* backends. They protect "the last real backend"; they don't reject a verdict for the upstream
  block name. The phantom `backend_pool:8080` sails through.
- The **trend_rule recovery suppressor** (slope-based) needs real-backend latency samples to detect
  recovery. After collapse there are none — only `backend_pool` at error 1.0 — so nothing ever flips
  back to healthy and no `include_backend` is ever issued.
- **`max_fails=0`** on the NGINX side (adapter line 27) correctly prevents NGINX-side ejection, but
  the ejection here is done actively by the sidecar, so that mitigation is bypassed.

---

## Bootstrap trigger (how it starts)
The very first 502s come from real-backend exclusions during the load ramp: at swarm start the
backends momentarily shed 503 (queue pressure) and/or the latency channels trip, so a few real
backends get excluded. The instant the pool is even briefly empty, NGINX emits `backend_pool` lines
and step C→D makes the collapse irreversible. So the *trigger* is real-backend false-positives under
burst, but the *irreversibility* is the `backend_pool` loop.

---

## Fix options (not applied — for decision)

Ordered by leverage. (1) alone breaks the loop; (1)+(2) is the robust pair.

1. **Allowlist the anomaly-detector's scoring set** (root fix, smallest blast radius).
   In `build_features_from_rows` (or the publish gate `should_publish`), skip any `instance` that is
   not a known backend — explicitly drop the upstream block name (`backend_pool`) and any
   non-`host:port` / non-resolvable instance. The detector should never score the LB aggregate as a
   backend.

2. **Make the sidecar reject non-backend verdicts** (defence-in-depth).
   In `handle_anomaly`, drop any verdict whose translated key is not in the live discovered backend
   set (the `discover_all_backends` result), instead of excluding `backend_pool:8080`.

3. **Stop the sentinel at the shipper** (optional, narrows the surface).
   Map the all-down sentinel `backend_pool` to a non-backend label (e.g. `lb_aggregate` or `unknown`)
   so it can never be mistaken for an `instance` downstream. Note: telemetry/RL/queries also read
   these labels — verify no consumer depends on `backend_pool` as an instance first.

4. **Cold-start hardening of the error channel** (reduces the bootstrap trigger).
   The error channel fires with no warmup and no minimum-sample weighting on `error_rate` avg. A
   short error-rate confirmation window (or requiring `sample_count` above a floor before the error
   channel can mark unhealthy) would stop a ramp-burst of 503s from excluding real backends in the
   first place.

## To recover the running stack (operational, separate from the fix)
The collapse is sticky on disk. A clean recovery without a code change:
`docker compose restart load-balancer lb-sidecar anomaly-detector` and **drain traffic first** so the
pool can repopulate with real-backend metrics before load resumes — otherwise it re-collapses. The
durable fix is (1)+(2).
