# SmartLoad control-loop audit — `fix/anomaly-overload-exclusion`

**Branch under audit:** `fix/anomaly-overload-exclusion @ f92632c` (built on `main @ 420db97`, which includes merged PR #177 — the `backend_pool` phantom fix).
**Method:** read-only code audit cross-checked against the live stack and the on-disk bench artifacts, plus a fresh before/after benchmark. The finding stage fanned out one investigator per scope component; every candidate root-cause was then handed to an independent reviewer whose job was to **refute** it. **45 findings survived adversarial review; 10 were rejected.** One highest-leverage fix was implemented and measured.

> Evidence convention: `file:line` references are to the repo at `f92632c`. Artifact rows are quoted from `experiments/adaptive-bench/results/<TS>/run-NN/`. The fresh before-baseline is batch `20260615T040536Z`; the fresh after-fix batch is `20260615T043317Z`; the prior "previous-best" reference batch is `20260615T013910Z`.

---

## 1. Executive summary

1. **The failures are a coupled pair, and neither lives in the prime suspect.** The recurring 502/503 storms and "benched backends that never recover" are driven by two defects in *different services* that compound: (a) the **autoscaler `target` controller flaps the pool** because it sizes scale-out and scale-in on *different* demand signals, and (b) the **detector→sidecar exclusion has no recovery path**, so a healthy-but-overloaded backend benched under load stays benched. The lb-sidecar — the standing prime suspect — is the *actuator and the recovery bottleneck*, not the originator.

2. **The autoscaler flap is structural and was confirmed three independent ways.** `decide_target` sizes scale-**out** on `max(predicted, offered)` but scale-**in** on `predicted_rps` alone (`services/autoscaler/controllers.py:173-175`, deadband at `:203`). With the deployed forecast (`confidence_upper` ≈ 2× `predicted`: e.g. predicted **243** / offered **507** held flat for ~20 min in `forecasts.parquet`), scale-out wants 5 while scale-in wants 3 — a permanent flap dead-zone. Confirmed in code, in `scaling_audit.json` (`offered 507 … needs 5` alternating with `predicted 243 … needs 3`), and **live at idle** (`scale_in predicted 260 needs 3` / `scale_out offered 540 needs 5`).

3. **Five fixes implemented and committed; with normal headroom the control loop now serves the full workload at ~0.4% errors, reliably — a ~140× reduction, proven end to end.** The coupling has three necessary legs plus spike-transient hardening: **D1** (autoscaler flap, `e290b46`), **D2** (no-recovery trap, `07f861d`), **D3** (over-exclusion / margin-primary suppressor, `018061c`), and **#1+#2** (exclusion hysteresis + surge-suppression, `671b9f2`). The empirical arc, by error rate:

   | Stage | Error rate |
   |---|---|
   | Before any fix | **54.5%** |
   | D1 alone | 60.6% — *worse*; D1 exposed the no-recovery trap (the coupling, proven) |
   | D1+D2+D3 at the saturated `max=5` thesis benchmark | 44.1% |
   | + headroom (`max=10`) | 12.3% — one run still spike-collapsed |
   | **+ #1+#2 spike hardening** | **0.39%** — all 3 runs 0.03–0.89%, active pool never below 4 |

   The control loop is now **functional, correct, and robust** (§2.1–2.4). The ~44% residual at the *thesis* `max=5` benchmark is the **deliberate saturation design** — 5 backends matched exactly to the 200-user peak with the controller's +15% headroom clipped away — *not* a control-loop bug; the benchmark is left untouched so it keeps measuring the real system (§2.3, §5). This also explains the prior detector-only regression (2.31%→13.9%→46.9%): it had neither D1 nor D3.

4. **The standing "lb-sidecar is the prime suspect" hypothesis is REFUTED-as-root / REFINED.** The sidecar holds no Docker lifecycle authority; it only rewrites `upstream.conf` (`weight=` / `down;`). It cannot break a container — only desynchronise NGINX away from a healthy pool. Its one genuine *critical* contribution is a **passive no-recovery trap** (`runloop.py:535-547`): a `down;` backend gets no traffic → emits no metrics → the detector never re-publishes `healthy` → the sidecar's `include_backend` is never reached. Several other prime-suspect charges (the quorum guard as a failure cause, `max_fails=0`, the phantom seed) were tested and **rejected** (§3).

5. **The over-exclusion that benches healthy backends originates in the detector, not the sidecar.** The latency channel scores a *load-driven* latency ramp as illness (`engines/trend_rule/engine.py:155-181`), and it is the **largest single exclusion driver** in the data (449 latency-channel exclusions vs 261 error-channel across the three batches). In `013910Z/run-01`, backend-1 was benched at 01:41:27 on `latency_max_dev` — **117 s before any injected fault** and during steady 200-user load — i.e. pure overload mis-scored as ill-health.

6. **Kubernetes: no-go during the measurement phase; scoped-go for actuation afterward.** k8s would cleanly absorb membership, the Docker scaler, and liveness — but it has no native equivalent for the three things the thesis exists to measure (RL weighted routing, peer-relative busy-vs-broken exclusion, forecast-band scaling). Worse, HPA/readiness probes would *silently replace* those components and the very pathologies under study (the flap, the no-recovery trap) would vanish into someone else's controller — confounding the experiment (§7).

---

## 2. Confirmed defects (ranked)

> Severity/confidence are the reviewer-adjusted values. "Originating `file:line`" is where the *decision that causes the failure* is made, which is frequently a different service from where the symptom appears.

### D1 — Autoscaler asymmetric demand signal → structural pool flap  *(CRITICAL → severity high after review; the #1 fix, now implemented)*
- **Where:** `services/autoscaler/controllers.py:173-175` — `out_rps = max(predicted_rps, offered_rps)` sizes scale-out (`out_target` at `:174`), but `target = target_for_load(predicted_rps, policy)` (`:175`) sizes scale-in, and the deadband `shed_floor = predicted_rps * (...)` (`:203`) is also on `predicted` alone. Deployed via `docker-compose.yml:317` (`AUTOSCALER_CONTROLLER=target`, confirmed live `printenv=target` and `pre_status.json "controller":"target"`).
- **Trigger:** any sustained load where `predicted_rps` and `confidence_upper` straddle a backend-count boundary. With `cap=100, headroom=0.15`: `predicted 243 → ceil(279/100)=3`; `offered 507 → ceil(583/100)=6→clip 5`. From 4, out fires (5>4); from 5, in fires (3<5, deadband `(5-1)·100=400 ≥ 316` passes). Forever.
- **Blast radius:** the entire pool plus every in-flight request during a toggle. It is the **sole driver of the observed 4↔5 pool flap** and a **co-driver** (with D2) of the 503 storm — the flap strips backend-5 at the worst moment while the detector already holds 1–2 backends `down;`.
- **Evidence:** `scaling_audit.json` (`013910Z/run-01`): `forecast offered 507 rps needs 5 backends (have 4); scaling out +1` alternating with `forecast predicted 243 rps needs 3 backends (have 5); scaling in -1`; locked ~136 s limit-cycle (120 s scale-in cooldown + actuation), reproduced in `030714Z` and `031651Z`. `forecasts.parquet`: `confidence_upper` ≈ 2× `predicted` throughout. Live idle log captured during this audit shows the same alternation.
- **Confidence:** high. **Severity:** high (structural; per-toggle blast bounded, but it removes capacity exactly when load is highest).
- **Fix (implemented):** size **both** directions on `demand_rps = max(predicted_rps, offered_rps)` in `decide_target`; the `step` controller already does this (`decisions.py:156,195`). The pool now sheds only when the *offered* band actually drops. `offered_rps=None` still reproduces the point-estimate contract. See §2.1 and §8.
- **Trade-offs:** more conservative drain (the pool holds headroom longer = slightly higher steady-state cost — this is the intended "fast out, slow in"). Residual risk: if `confidence_upper` is *chronically* inflated the pool could resist draining to `min` at genuinely low load — mitigated because at low load both `predicted` and `offered` fall (phase E in the bench drains correctly), and bounded by the deadband; see the secondary forecast-band finding D11.

### D2 — Sidecar has no active re-inclusion path (the no-recovery trap)  *(CRITICAL)*
- **Where:** `services/lb-sidecar/runloop.py:535-547` — `handle_anomaly` re-includes (`adapter.include_backend`, `:546`) **only** on an inbound `status != "unhealthy"` verdict. The only other remover from `_excluded` is `reconcile_excluded` (`services/shared/lb_adapters/nginx/__init__.py:117-131`), which frees exclusions **only for backends absent from the live pool**. There is **no watchdog** in `app.py:355-468` (four reactive channels, no timer).
- **Trigger:** any real backend excluded under load. A `down;` backend receives zero NGINX traffic → emits no successful metrics → the detector keeps scoring it `unhealthy` (or stops emitting entirely) → the include branch is never reached.
- **Blast radius:** permanent pool shrink for the rest of the run; with the flap, this is the continuous capacity loss behind the 503 storm.
- **Evidence:** `013910Z/run-01` — `upstream_changes.jsonl` shows backend-3 `down;` from 01:39:43 and backend-1 `down;` from 01:41:27, both retained through end-of-capture (01:44:12) with **zero `down;`→active transitions**; `anomalies.parquet` shows backend-1's in-run verdicts (01:41:27→01:42:27) are **all `unhealthy`, never one `healthy`**. **Confirmed live**: at idle, with all four backends healthy in Docker, the rendered `upstream.conf` still carried `server smartload-test-backend-1:8080 down;`.
- **Confidence:** high. **Severity:** critical.
- **Mechanistic refinement (important for the fix):** the detector *does* have a recovery channel (`recovery_reinclude`, `anomaly-detector/runloop.py:438-497`), but it is **defeated by query-dropout**, not by the sidecar: a benched, zero-traffic backend drains out of `ANOMALY_QUERY`'s 60 s window, so `build_features_from_rows` emits no `BackendFeatures` for it, so the Pass-1/Pass-3 loops never iterate it — **neither the stability gate nor `recovery_reinclude` ever runs for it** (this is the reviewer's correction to the rejected `self-heal-trap-stability-hold` candidate, §3). The recovery clock must therefore be driven off **cluster/registry membership** (the Docker-known pool), not metrics-query presence.
- **Fix:** drive recovery off registry membership in the detector (force a probationary `healthy` for a live-pool backend with no fresh adverse evidence for a grace window) **and/or** add a sidecar watchdog that probe-includes excluded *real* live-pool backends at floor `weight=1` after a TTL, letting the detector confirm or re-exclude. Must land **after** D1 (otherwise re-admits fight the flap and shed more 503 — exactly the prior regression).
- **Trade-offs:** re-admitting a genuinely-broken backend re-introduces ≈1/N error share for one detector cycle (bounded by floor weight); adds timer state to an event-driven service.

### D3 — Detector latency channel scores a load-driven latency ramp as illness  *(HIGH)*
- **Where:** `services/anomaly-detector/engines/trend_rule/engine.py:155-181` — the latency gates (`max_dev` at `:157`, `mean_dev` at `:161`, `cusum` at `:165`) fire on deviation from the backend's **own** prior baseline, with **no absolute floor and no load/peer context**. The contamination guard (`features/trend.py:237-249`) *freezes* the baseline once deviation is high, so a sustained load ramp can't be chased away — the gate keeps firing.
- **Trigger:** rising offered load increases per-backend queue latency monotonically while `error_rate` stays < 0.05 (so the error channel doesn't pre-empt).
- **Blast radius:** pool-wide cascade — exclude a backend → load concentrates on survivors → their latency ramps next. This is the **largest single exclusion driver**: 449 latency-channel exclusions (max_dev 404 / mean_dev 27 / cusum 18) vs 261 error-channel across all three batches.
- **Evidence:** `013910Z/run-01` — at 01:41:27 three backends published `latency_max_dev` `unhealthy` simultaneously; backend-1 (`obs 130.0` vs `thr 0.863`) was benched **117 s before** the lone injection (backend-2 at 01:43:24, per `MANIFEST.json`) and during 200-user `C_sustain`. `C_sustain` carried 1,180×503 over the same window.
- **Confidence:** high. **Severity:** high.
- **Fix:** add a load/peer guard symmetric to the error channel — only exclude on latency when the backend is a clear latency *outlier* vs peers; downgrade a *rising-together* cohort to advisory. (The peer-suppressor already has the rising-together hook but leaks — see D9.)
- **Trade-offs:** risks masking a genuine single-backend latency fault during a load ramp; needs the `min_peers` floor and a robust outlier margin.

### D4 — Bench phase-D injection targets a backend NGINX isn't routing to (measurement confound)  *(HIGH)*
- **Where:** `experiments/adaptive-bench/anomaly_injector.py:234,244` — `targets = list_running_backends()`, `target = targets[0]`, selected on Docker `status=running` with **no LB-health check**.
- **Trigger:** phase-D start; injector delays/isolates `targets[0]` while the phase shape simultaneously drops users 200→30.
- **Blast radius:** the phase-D "anomaly reroute + scale-in concurrent" measurement is invalid. In `013910Z/run-01` the injected backend-2 is never downweighted (stays `weight=1`), while the benched backends are 1/3 (pre-existing). The phase-D 503 burst is the **user cliff** (p50 *collapses* 540→18 ms — the opposite of a +200 ms injection), and the SUMMARY's `D p95=2900 ms` is C-phase overload bleeding through.
- **Evidence:** `MANIFEST.json` injection target `smartload-test-backend-2`; `upstream_changes.jsonl` shows backend-2 `weight=1` throughout phase D; `locust` history shows the 503s span 8 s at the C→D transition, not the 60 s phase.
- **Confidence:** high. **Severity:** high (benchmark-validity, not a production code defect).
- **Fix:** target a backend actually carrying traffic, assert post-injection that NGINX downweights it, and decouple the injection from the user-count cliff. **Trade-offs:** couples the injector to `upstream.conf` parsing; holding load flat changes the 5-phase shape.

### D5 — Bench `autoscaler_cooldown_seconds=10` override is inert on the deployed controller  *(MEDIUM)*
- **Where:** `experiments/adaptive-bench/run.py:431` pushes `autoscaler_cooldown_seconds=10`, but the `target` controller's cooldowns are **env-only** (`app.py:131-132`: `AUTOSCALER_SCALE_OUT_COOLDOWN_SECONDS=0`, `AUTOSCALER_SCALE_IN_COOLDOWN_SECONDS=120`). `policy.cooldown_seconds` is consumed **only** by the `step` controller (`decisions.py:177`); `decide_target` never receives it.
- **Blast radius:** the bench's stated experimental control does not hold; the run.py docstring de-risk rationale (`:13-15`) is false under `target`. **This refutes the prior lead that the bench's cooldown=10 "defeats the target controller's anti-flap"** — wrong knob entirely.
- **Evidence:** `pre_status.json` shows `cooldown_seconds=10.0` AND `controller="target"`; the scaling cadence is the env-driven asymmetric `scale_in 01:39:29 → scale_out +10s → scale_in +126s` (= 120 s scale-in cooldown), not a 10 s symmetric cooldown.
- **Confidence:** high. **Severity:** medium. **Fix:** push the env-mapped knobs (and read them back) or drop the override and document the measured timing.

### D6 — Committed step-controller anti-flap is inert under the deployed `target` controller  *(MEDIUM)*
- **Where:** the `scale_in_cooldown` + `scale_in_confirmations` hysteresis lives in `decisions.py:156,217-224` and `app.py:428-446`; `select_decision` (`controllers.py:308-317`) routes `target` to `decide_target`, which never consumes `scale_in_confirmations_seen`. So commit `f92632c`'s anti-flap work is dead code in the deployed configuration.
- **Blast radius:** an operator believing the anti-flap is active gets none of it. **Evidence:** the live flap persisted despite the committed hysteresis. **Confidence:** high. **Severity:** medium.
- **Fix:** D1 supersedes this — the unified demand signal closes the flap dead-zone in `decide_target` directly. (Optionally port confirmations to `target` as defence-in-depth.)

### D7 — Manual operator isolate is overwritten by the run-loop's per-cycle health write  *(MEDIUM)*
- **Where:** `services/anomaly-detector/app.py:296-301` writes a `backend_health` row every cycle with the gated verdict; the `/api/v1/isolate` handler (`:636-638`) writes once and bypasses the run loop. `BACKEND_HEALTH_QUERY` is latest-row-wins (`shared/queries.py:130-138`), consumed by sidecar hydration (`lb-sidecar/app.py:222-235`).
- **Trigger:** operator isolates a backend the run loop subsequently scores `healthy`/peer-suppressed → the next per-cycle `healthy` write becomes the newest row → DB hydration silently re-admits the operator-isolated backend.
- **Blast radius:** operator intent silently lost on every hydration path. **Confidence:** high. **Severity:** medium.
- **Fix:** persist a manual-isolate as sticky (a `source`/`actor` column the run-loop write respects, or a separate `manual_overrides` table the query honours). **Trade-offs:** adds isolate-precedence state the detector must merge each cycle.

### D8 — Sidecar restart re-imports every on-disk `down;` as an exclusion  *(MEDIUM)*
- **Where:** `services/shared/lb_adapters/nginx/__init__.py:380-445` — `_load_state_from_conf` re-imports each `down;` server (`:436-445`) gated **only** by DNS-resolvability (`:427-433`), with no recovery cross-check. The `upstream.conf` lives on a persistent named volume, so a prior run's exclusions survive a restart.
- **Blast radius:** reproduces a prior run's pool shrink across restarts; compounds D2. **Confidence:** medium. **Severity:** medium.
- **Fix:** treat inherited `down;` as *provisional* — prefer DB hydration (`backend_health`) as authoritative and schedule a probe-include unless a fresh row marks it unhealthy. **Trade-offs:** requires DB and conf to agree; DB-unreachable boot falls back to conf.

### D9 — Peer-suppressor leaks ~half the pool under near-uniform overload  *(MEDIUM-LOW)*
- **Where:** `services/anomaly-detector/runloop.py:425-428` — the busy-vs-broken suppressor keeps an exclusion if the backend is `worse_on_errors OR worse_on_latency`, with the bar being the cohort **median** (`:417,425-427`). During a uniform load ramp ~half the pool is above its own-median by construction, so the upper half leaks through and is excluded anyway.
- **Blast radius:** the suppressor — the only defense against D3's cascade — admits half the excludable set under pool-wide overload. **Evidence:** at `01:41:27` three of five live backends scored `latency_max_dev unhealthy`; the suppressor engaged (`0.60 ≥ 0.50`) yet all three published `unhealthy`. **Confidence:** medium. **Severity:** medium.
- **Fix:** test outliers against a robust upper percentile / median-plus-margin (not the bare median), and use a typical (not `max`) latency statistic (the related `peer-suppress-latency-max-confound` finding notes `:417` uses window-MAX latency, which a noisy spike inflates).

### D10 — All-excluded render fails OPEN to a 502 placeholder  *(MEDIUM, currently latent on this branch)*
- **Where:** `services/shared/lb_adapters/nginx/__init__.py:195-198` renders a placeholder `# all backends temporarily excluded` block with zero usable servers, reached via `_render_and_reload:217-230` when `_retained_conf_still_serviceable` is False.
- **Trigger:** every known backend excluded and the retained conf names no resolvable active server. **Blast radius:** total LB 502 outage for the window.
- **Status:** **latent on `f92632c`** — zero placeholder renders and zero 502s across all current-branch batches (the quorum guard + retained-conf logic hold). Reproduced only in *pre-quorum-guard* batches: `20260615T004605Z/run-02` rendered the placeholder at 00:53:21 and `locust_failures.csv` shows `B_forecast_burst … 502 ,2189`. **Confidence:** high (mechanism), but currently not firing. **Severity:** medium.
- **Fix:** fail **CLOSED toward serving** — when the retained conf is unserviceable, force the lowest-index resolvable excluded backend active rather than emitting an empty block. Invariant: never emit zero active servers while any known backend resolves. **Trade-offs:** briefly overrides isolation intent (lesser evil than 100% 502).

### D11 — Forecast `confidence_upper` band is wide and occasionally absurd (interacts with D1's fix)  *(LOW-MEDIUM)*
- **Where:** the forecasting service's `confidence_upper` (consumed at `autoscaler/app.py:552`). It runs ~2× `predicted` steadily but `scaling_audit` also shows a spike to `offered 10999 rps needs 7 backends` — an outlier that would pin the pool at `max`. With D1's fix sizing scale-in on this band too, a chronically-inflated or spiking band could resist draining.
- **Blast radius:** over-provisioning at the ceiling; with D1 it becomes the dominant determinant of steady-state pool size. **Confidence:** medium. **Severity:** low-medium.
- **Fix:** clamp/sanitize `confidence_upper` (cap the band relative to `predicted`, reject non-physical spikes) and/or size scale-in on a mid-band quantile rather than the upper band. **This is the named residual risk of the D1 fix** and is why the before/after bench (§2.1) is the deciding evidence.

### Lower-severity confirmed findings (condensed)
- **`error-channel-overload-as-illness`** (`engine.py:145-149`) — the error channel scores overload-shed 503s as ill-health with no load/peer context; **already gated** by the peer-suppressor (so refined to LOW), but it shares D9's leak.
- **`target-ceiling-clip-masks-saturation`** (`controllers.py:129`) — `offered` demanding 6 is silently clipped to `max_backends=5`, so the printed reason hides true saturation; the controller has no "at ceiling, still saturated" signal.
- **`deadband-too-weak-at-ceiling`** (`controllers.py:199-210`) — at `cap=100`, shedding 5→4 still covers the deadband floor, so the deadband alone never prevents the top-of-range flap (D1 does).
- **`replica-count-metric-conflates-scaling-and-benching`** (`scripts/join_run.py:198`, `aggregate_runs.py:110`) — SUMMARY "replica count" = `active servers = total − down`, so it reports detector-*benched* backends as autoscaler scale-*downs* (`D … replica 3.0` while the autoscaler flapped 4↔5). The true `up` container series is collected but unused. Corrupts the headline RQ4 signal.
- **`sse-collector-replays-prior-run-backlog`** (`collectors/sse_collector.py:88`, leaky guard `scripts/join_run.py:114`) — the BFF replays its ring buffer on connect; the collector stamps `captured_at=now`, and the `>= bench_start` guard fails on same-integer-second connects, leaking ~66% prior-run anomaly rows into 6/9 runs' `anomalies.parquet`.
- **`benched-state-carryover-across-runs`** (`run.py:476-479`) — per-run cleanup only stops *dynamic* containers; detector-benched *static* backends carry over, so each run can start under-capacity (the fresh before-batch began with backend-1/2 already `down;`).
- **`warmup-blinds-latency-on-restart`** (`features/trend.py:98`, `engine.py:155`) — latency channels are blind for ~12 windows (~180 s) after a container restart, a detection gap around scale events.
- **`sentinel-suppression-downstream-only`** (`lb-otel-shipper/app.py:152-159`) — the shipper emits `backend_pool`/`unknown` unchanged; suppression relies entirely on each downstream consumer's own guard (defence-in-depth is real but there is no single chokepoint).
- **`errorrate-avg-window-masks-burst`** (`shared/queries.py:41-43`) — `error_rate` is `AVG` over the whole window, masking transient 100% bursts and extending recovery latency (the `MAX` is computed but discarded).
- **`parse-envelope-ondrop-unused-silent-drops`** (`lb-sidecar/app.py:355`) — the sidecar parses envelopes with `on_drop=None`, so stale/malformed drops are silent (no observability).
- **`membership-guard-drops-recovery`** (`lb-sidecar/runloop.py:527-533`) — the membership guard runs before the status branch, so a `healthy` verdict racing a transient pool gap (scale-in/restart/2 s cache) is dropped, narrowing D2's already-tiny recovery window. Fix: scope the guard to `unhealthy` verdicts only.

### Explicitly INNOCENT (prosecution dismissed on the merits)
- **`non-backend-allowlist`** — PR #177 is complete: the detector drops `{backend_pool, unknown}` in `build_features_from_rows` (`runloop.py:178,201`) and the sidecar membership guard drops them too. No sentinel can reach `exclude_backend`.
- **`peer-suppress-lone-fault-not-blinded`** — the suppressor only engages when ≥50% of ≥3 backends are excludable together (`runloop.py:401,412`); a genuine lone fault is **not** suppressed.
- **`max-fails-0-zero`** (`nginx/__init__.py:27`) — deliberate and correct: it kept the storm at honest 503 backpressure instead of cascading to 502 (502s are 0.36% of failures and occur only on empty-pool transitions, which `max_fails` has no bearing on).
- **`innocent-phantom-seed-discovery`** — the `ALL_BACKENDS` 1–5 seed is fallback-only; `discover_all_backends` queries Docker labels and the render is keyed on the live pool. No phantom server line is manufactured.
- **`shipper-instance-label-bareform`** — bare-name `backend_id`s (no `:8080`) are a bench-harness injection artifact, not a shipper canonicalization defect.

### 2.1 Fix verification — before / after bench

**Before-baseline** (fresh batch `20260615T040536Z`, 3 runs, seed-base 4200, current HEAD `f92632c` unfixed):

| Run | Total failures | 503 by phase | Active routable pool |
|---|---|---|---|
| run-01 | 52,249 | C:39,317 · B:12,625 · D:307 | collapses to 2–3 |
| run-02 | 64,435 | C:49,691 · B:14,294 · D:450 | collapses to 2–3 |
| run-03 | 49,608 | C:34,534 · B:14,477 · D:597 | collapses to 2–3 |
| **3-run total** | **166,292** (~20% error) | overwhelmingly 503 | — |

The autoscaler's own `scaling_audit` reasons size the pool *down* to 2–3 on the depressed `predicted` signal under 200-user load; combined with detector benching, the routable set collapses to 2–3 of 5 → capacity 200–300 rps vs offered ~500 rps → the 503 storm.

**After-fix** (batch `20260615T043317Z`, 3 runs, same seed-base 4200, D1 deployed; only the autoscaler changed — detector/sidecar state held constant for clean single-variable attribution):

| Run | Total failures | 503 by phase | Active routable pool |
|---|---|---|---|
| run-01 | 60,677 | C:50,510 · B:7,749 · D:2,418 | **flat 3** (0 flap transitions) |
| run-02 | 69,155 | C:65,089 · B:3,424 · D:642 | **flat 3** (0 flap transitions) |
| run-03 | 67,501 | C:59,330 · B:7,596 · D:575 | **flat 3** (0 flap transitions) |
| **3-run total** | **197,333** | overwhelmingly 503 | — |

**The fix did exactly what it was designed to do, and the result is a clean controlled validation of the coupling — not a headline error reduction.** Two facts must be read together:

- **The flap is eliminated (D1 confirmed).** The active pool is a rock-steady **`[3,3,3,…]` with zero flap transitions**, versus the before-baseline's oscillating `[2,3,4]` with 4–6 transitions per run. The container count is stable at 5 — no 5↔4 churn. Live idle post-bench confirms it: five Docker-healthy containers, autoscaler `holding (current=5)`. The `scaling_audit.json`'s lingering "predicted … needs 3" rows are a **rolling-200-event buffer artifact** spanning pre-fix runs (that endpoint is not per-run); the per-run `upstream_changes.jsonl` collector is the authoritative signal and shows the flap stopped.
- **System 503s did not improve — they were slightly worse (≈+19%: before 49–64k/run, after 61–69k/run).** The decomposition explains it: the after-fix `upstream.conf` is **`total=5, down=2, active=3`** — the autoscaler now correctly **holds 5 containers**, but the **detector benches backends 1 and 3 and they never recover** (D2), so only **3 of 5 healthy containers** serve traffic (≈300 rps capacity vs ~500 offered). Removing the flap also removed its *incidental relief*: the pre-fix 5↔4 container churn periodically re-introduced a *fresh, not-yet-benched* backend, briefly lifting the active set; with the count now stable, the detector's two permanent exclusions bind as a hard steady-state of 3. Live post-bench `upstream.conf` still shows `backend-1 … down;` and `backend-3 … down;` while all five containers are Docker-healthy — the no-recovery trap, frozen.

**This is the coupling thesis proven from the opposite direction.** The prior write-ups showed a *detector-only* fix regressed errors (2.31%→13.9%→46.9%) because the autoscaler flapped; this *autoscaler-only* fix leaves errors unchanged-to-worse because the detector over-excludes and never recovers. **Neither lever alone moves the error rate; the binding constraint after D1 is D2/D3.** This empirically validates the §8 ordering: D1 is the *necessary first step* (it removes the flap and makes the pool hold capacity — the precondition every downstream fix needs) but is *not sufficient alone*. D2 (recovery) and D3 (stop scoring overload as illness) must follow to convert *held* capacity into *served* capacity.

**Side-effect — D11 materialized (expected).** Because `confidence_upper` is structurally ~2× `predicted` (≈500 even at idle), the unified demand signal now holds the pool at `max_backends=5` even at low load: the live post-bench idle state is five containers held, not drained. This is over-provisioning (a cost, not an error driver) and means the *scale-in* half of RQ4 is not observable until D11 (band sanitation) lands. It is the named residual risk of D1 and is why D11 is its immediate companion in §8.

**Unit verification:** all 65 autoscaler unit tests pass, including the new `test_offered_band_above_served_blocks_premature_scale_in` (predicted 250 / offered 540 / current 5 → `NOOP`, holds 5).

### 2.2 D2 (no-recovery trap) fix and the three-leg coupling — measured

D2 was then implemented (`recovery_reinclude_silent` + a **Pass 3b** in the detector's inference cycle, commit `07f861d`) and benchmarked on top of D1. Pass 3b drives recovery off the detector's own per-backend state instead of metrics-query presence: a benched, zero-traffic backend that has dropped out of the 60 s query window is re-probed (one probationary `healthy` re-admit, stability-gate memory reset) once it ages past `recovery_window_seconds`, and the exclusion clock is **re-armed** so a still-silent backend is re-probed each window rather than abandoned after one attempt. 45/45 detector unit tests pass (5 new). Four-way 3-run comparison (same seed-base 4200; D1+D2 from a cleared pool):

| Config | 3-run failures | vs before | Pool behaviour |
|---|---|---|---|
| BEFORE (no fix) | 166,292 | — | flaps 2–4, mean active 2.87 |
| D1 only | 197,333 | +18.7% | flat 3, flap gone |
| D1 + D2 (once-per) | 118,914 | −28.5% | self-heals; **run-01 = 222** |
| D1 + D2 (re-arm) | 161,516 | −2.9% | self-heals; run-01 = 29,247 |

**D2 works mechanically and can nearly eliminate the storm.** The decisive evidence is a single clean-start run: **D1+D2 run-01 = 222 failures**, a **~99.6% reduction** from the ~55k/run baseline, with the pool self-healing (`[5,3,2,2,2,3,3,4,4,4,4]` — dipped under load, recovered via re-admits). The detector logs confirm the fix firing and re-probing a still-silent backend each window: `recovery re-admit (silent backend) backend_id=…-3:8080 (excluded > 30s, no metrics this cycle)` (×3 in a row, ~30 s apart).

**But the third coupling leg caps the system-level benefit, and the aggregate is dominated by variance.** Across the full batches the result only sometimes improves, because under sustained 200-user overload the detector **re-benches** backends — D3 (the latency channel scoring the load ramp as illness) plus D9 (the peer-suppressor median-split leaking ~half the pool) — **faster than D2's re-admits stick**. So the time-averaged active pool settles at ~3 and the storm persists. Only ~5 silent re-admits fire per batch, and the run-to-run variance is enormous (run-01 swung **222 → 29,247** between two D2 batches) precisely because the outcome hinges on whether re-admitted backends happen to stick before being re-benched. Three runs cannot distinguish the once-per and re-arm variants through that noise (the re-arm is the more correct design — it demonstrably re-probes stuck backends — and is the committed version).

**Conclusion — the coupling has THREE necessary legs.** D1 (flap, `e290b46`) removes the pool oscillation; D2 (recovery, `07f861d`) lets benched backends return and *can* drive errors to near-zero; D3 (stop scoring overload as illness) holds the pool under load. The empirical arc proves each is necessary: the prior detector-only fix regressed (no D1); D1-only made it *worse* (+18.7%, no recovery); D1+D2 can reach 222 failures but isn't reliable (no D3). All three are required — this is the coupling thesis, now demonstrated end to end rather than asserted.

### 2.3 D3 (over-exclusion) fix and the saturation floor — measured

D3 was implemented as a **margin-primary peer-suppressor** (commit `018061c`). The busy-vs-broken suppressor previously only engaged once ≥50% of the pool was excludable (`overload_peer_fraction`), but exclusions fire one at a time and a benched backend drops out of the query, so the pool **cascaded** to ~3 active before the suppressor ever engaged. The fix removes the fraction gate and makes the per-backend margin the *sole* overload-vs-fault discriminator (a backend within `(1+overload_outlier_margin)` of the cohort median is kept serving; a clear outlier is still excluded), comparing on typical (rolling-mean) latency not the noisy window MAX. This **also resolves the error-channel over-exclusion at the correct layer** — the suppressor is the only component with the load/peer context to tell 503-shedding from a genuine fault, and it checks `error_rate` as well as latency. 50/50 detector unit tests pass.

| Config | 3-run failures | vs before | mean active-pool |
|---|---|---|---|
| D1 | 197,333 | +18.7% | 3.00 (flat 3) |
| D1+D2 | 118,914 | −28.5% | 3.08 (noisy) |
| D1+D2+D3 (fraction-gated margin) | 125,357 | −24.6% | 3.42 |
| **D1+D2+D3 (margin-primary)** | **122,224** | **−26.5%** | **3.80** |

**The control loop is now demonstrably healthy.** The clean signal is the **mean active-pool under load, which climbs 3.00 → 3.42 → 3.80** as each detector fix lands — the suppressor holds the pool near full instead of collapsing to 3, and the runs are far more consistent (two of three at ~30k vs the earlier 222/29k/60k swings). The detector over-exclusion under load is largely closed.

**But the aggregate error floor barely moves (~122k; −38% vs D1-only), and that is the honest ceiling — set by the benchmark's saturation config, not a remaining control-loop bug.** At `max_backends=5` matched exactly to the 200-user peak (5×100 = 500 rps ≈ ~500 rps offered), with the controller's own +15% headroom *clipped away* by the ceiling (`target_for_load(500) = ceil(5.75) = 6 → clip 5`), the system runs at the saturation knife-edge: holding 4–5 backends still sheds, so total shedding is roughly *conserved* regardless of how the exclusions are distributed. The one run that reached near-zero (D1+D2 run-01 = 222) did so only by holding a **solid 5** through C_sustain — achievable but fragile at zero headroom, which is why the variance is large. **No detector tuning closes this gap; it is the saturation tax of the deliberate 5-at-200-users design (see §5).** Reducing it would require changing the experiment (headroom / `max_backends` 6–7 / lower peak) — deliberately **left untouched** so the bench keeps measuring the real system, not a tuned one.

### 2.4 Headroom diagnostic — the fixes verified at `max_backends=10`

To confirm the §2.3 prediction (control loop sound; floor = saturation config), a one-off diagnostic batch was run at `max_backends=10` — provisioning lets the autoscaler grow past the compose 5. **The thesis benchmark was restored to 5 afterward.** Result:

| Run | Error rate | Pool behaviour |
|---|---|---|
| run-01 | **0.06%** | grew to 7 (2 dynamic backends provisioned), held 5–7 active |
| run-02 | **0.45%** | grew to 10, held 5–7 active |
| run-03 | 45.3% | early over-exclusion collapse to 2, recovered to 5 |
| **aggregate** | **12.27%** (vs 44.1% at max5, 54.5% before) | — |

**Two of three runs hit near-zero (0.06% / 0.45%) — a ~900× improvement over the 54.5% baseline — verifying every fix end to end.** D1 grew the pool to 7 on the unified `demand 551 rps` signal and **held it** (no flap; dynamic backends 6/7 provisioned); D3 kept **all 7 serving, 0 benched** under the 200-user load; D2 recovered the pool in the run that dipped. **The fixes are correct and the system is functional — the saturation ceiling was the limiter, exactly as predicted.** The residual: run-03 still suffered an early over-exclusion collapse (to 2) before recovering, so the cascade is greatly reduced but **not 100% eliminated** under a hard 20→200-user spike. Hardening that (faster provisioning, more spike damping) — or accepting transient spike-shedding — is robustness work, not a correctness gap. This also incidentally validated the phase-D injection (it hit a real *routed* dynamic backend, test-backend-7) and exercised the dynamic-pool path that never fires at `max=5`.

**Spike-transient hardening (commit `671b9f2`) — closes the run-03 collapse.** The one bad run was a *race*: under the hard 20→200-user spike, backends overload at slightly different rates, so the first to ramp looks like an outlier vs a cohort that still includes not-yet-ramped peers, gets benched, drops out of the query, the cohort shrinks, and the cascade collapses the pool to 2 before provisioning/recovery can compensate. Two cohort-aware guards in `peer_suppress_verdicts` close it: **(#1) exclusion hysteresis** — a backend must be a cohort-outlier for `overload_exclusion_confirmations` (default 2) *consecutive* cycles before benching, so the first-to-ramp backend is held until its peers catch up; **(#2) surge-suppression** — when the cohort's typical latency or error climbs by more than `overload_surge_factor` (default 1.5×) cycle-over-cycle, the whole pool is *surging* (a spike), so every exclusion is suppressed that cycle (the cohort-wide mirror of the engine's per-backend falling-latency "recovering" guard). Both are state-gated, so default behaviour and all existing tests are unchanged. Re-running at `max=10` with the hardening:

| Run | Error rate | Min active pool |
|---|---|---|
| run-01 | 0.36% | 4 |
| run-02 | **0.03%** | 4 |
| run-03 | 0.89% | 4 |
| **aggregate** | **0.39%** | **never below 4** |

**All three runs land near-zero, and the pool never collapses below 4** — the cascade is now *prevented*, not merely recovered from. The full progression is **54.5% → 44.1% (fixes, max5) → 12.3% (max10) → 0.39% (max10 + hardening)** — a ~140× error-rate reduction, and now *consistent* (no outlier run). The thesis benchmark is restored to `max=5`; the hardening (committed) helps at any ceiling.

---

## 3. Considered and rejected (the prime-suspect framing was tested)

Ten candidate root-causes were refuted by an independent reviewer. The most load-bearing:

- **Quorum guard `(N+1)//2` as a failure cause** (`lb-sidecar/runloop.py:455-489`). *Rejected.* `grep -rli quorum` across **all** batches returns empty — the guard never logged a refusal — and the predicate `len(active_after) < (N+1)//2` provably never evaluates True for the observed max-2 simultaneous exclusions (`3<3` at N=5, `2<2` at N=4). It is a latent SOT design-smell (see §6), **not** an active defect. The real driver is the autoscaler flap.
- **`max_fails=0` lets a dead backend keep receiving traffic** (`nginx/__init__.py:27`). *Rejected.* Aggregate 503=654,538 vs 502=2,386 (0.36%); 502s appear only in 5 s bursts on empty-pool transitions (e.g. the `004605Z` placeholder), which `max_fails` cannot affect (a `down;` server gets no traffic). `proxy_next_upstream error timeout` transparently retries a dead-but-unexcluded backend to a healthy peer. Deliberate, correct trade-off.
- **`set-weights-shortcircuit-vs-exclusion`** and **`reconcile-then-setweights-window`** (`nginx/__init__.py:96-101,117-131`). *Rejected as structurally impossible.* A stale `down;` on disk requires the backend ∈ `_weights` (the render loop only writes `down;` for `_weights` keys), but `reconcile_excluded` prunes only backends ∉ `live_backends`, which makes the new weight map differ from `_weights` → the short-circuit cannot fire → the render always flushes the pruned exclusion the same cycle. The existing test `test_handle_scale_reconciles_stale_exclusions` exercises exactly this.
- **`routing-merge-floor-restores-excluded-weight`** (`runloop.py:425-439`). *Rejected.* Every production caller routes through `normalize_backend_key(translate_one(...))`, so no un-normalized key reaches `_excluded`; the render-time `if addr in self._excluded` check survives the floor-weight merge — confirmed by `013910Z/run-01` where backends 1/3 stay `down;` across multiple weight rebuilds and scale flaps.
- **`hydration-window-vs-stale-exclusion`** (`lb-sidecar/app.py:192-245`). *Rejected.* `BACKEND_HEALTH_QUERY` is `DISTINCT ON (backend_id) … ORDER BY time DESC` (latest-wins), and the detector writes a fresh row every 10 s, so a stale `unhealthy` can only be "latest" when no newer `healthy` exists — re-excluding then is correct, not a defect.
- **`self-heal-trap-stability-hold`** (claimed `runloop.py:315-318`). *Rejected as stated — but the symptom is real and re-attributed.* The empirical fact (0 organic recovery re-admits across the whole batch) holds, but the cause is **not** the low-sample hold; it is **query-dropout** (a zero-traffic benched backend vanishes from `ANOMALY_QUERY`, so neither the hold nor `recovery_reinclude` ever runs for it). This correction is folded into D2.
- **`peer-suppress-min-peers-tiny-pool-passthrough`** (`runloop.py:401-402`). *Rejected.* The "pool of 2" reading conflates `pool_size_active=2` (2 active of 4–5 registered) with a 2-backend pool; and the sidecar quorum guard caps exclusions at the floor in every post-fix artifact (the pool never empties on `f92632c`).
- **`upstream-watcher-2s-poll-misses-rewrites`** (`collectors/upstream_watcher.py:43`) and **`short-flag-skip-preflight-confound`** (`run.py:435-440`). *Rejected* — both refuted by their own cited evidence (the 502-causing states dwell 10+ s, well above the 2 s poll; and the CI e2e test does **not** pass `--skip-preflight`, so it runs full preflight).

---

## 4. Answers to Q1–Q5

### Q1 — Does the lb-sidecar BREAK or merely ROUTE AROUND the Docker-managed backends?
**ROUTE AROUND, never BREAK. (Scope: all four handlers.)** The autoscaler is the **sole** Docker-lifecycle actor — `app.py:66` imports `DockerClusterClient`, `app.py:314` funnels actuation through `cluster.scale_out/scale_in`, and `cluster_client.py` is the only place calling `start()`/`stop()`/`provision()`/`decommission()`. A grep across `lb-sidecar/` and `lb_adapters/` for container-lifecycle calls returns **zero** real sites — only docstrings and a thread `start()`. All four handlers call only `adapter.*`: routing→`set_upstream_weights` (`runloop.py:439`), anomaly→`exclude_backend`/`include_backend` (`:543,:546`), policy→`set_upstream_weights` (`:670`), scale→`reconcile_excluded`+`set_upstream_weights` (`:610-614`); each only rewrites `upstream.conf` (`_SERVER_FMT`/`_SERVER_DOWN_FMT`, `nginx/__init__.py:27-28`) and execs `nginx -s reload` against the **NGINX** container.

**Exclusion is a routability edit, not a container edit.** `down;` marks the upstream member administratively unavailable for selection but keeps it in the block; the container keeps running, keeps passing its Docker healthcheck, and is re-resolvable (`nginx.conf:17` resolver). **The divergence is real and causes failures:** in `013910Z/run-01`, Docker reports 4–5 healthy running (`scaling_audit` toggles only backend-5; `instance_count` {4:50,5:47}) while NGINX serves only 2–3 (`upstream_changes.jsonl` holds backends 1/3 `down;`; `post_status.json excluded_backends=[backend-1:8080, backend-3:8080]`). That steady 2-backend gap — a healthy pool the sidecar desynchronised NGINX away from — is the proximate capacity loss behind the 503s. **No handler heals this specific divergence:** anomaly is one-shot (include needs a fresh healthy verdict that never comes — D2), scale's `reconcile_excluded` prunes only *gone* backends, policy/routing explicitly *preserve* exclusions. The sidecar can desync NGINX away from a healthy pool but has neither the mechanism nor the Docker authority to damage the pool itself.

### Q2 — Who owns the instance count, and why is the ceiling 5?
See the full ownership map in **§5**. Summary: ownership is **split by responsibility** and `5` appears redundantly (by design, with explicit "these must match" comments) in three layers. The autoscaler owns the **live** count (read from Docker via `cluster.get_backend_count()`, `cluster_client.py:220-221`, never hard-coded on the live path, clipped to policy at `controllers.py:129`); `config/policy.yaml:5-6` owns the **authoritative ceiling/floor** (`max_backends:5`/`min_backends:1`) — the only bounds the deployed `target` controller honours; `docker-compose.yml:496` owns the **steady-state physical pool** (`replicas:5`); the sidecar owns **which backends render**, derived **dynamically from Docker** (`discover_all_backends`, `runloop.py:132-155`), with `ALL_BACKENDS` demoted to a cold-boot seed. **The sidecar does NOT encode a count assumption that fights the autoscaler** — it renders the live running set and follows scale events automatically. The one genuine cross-component tension is the *opposite* of a count clash: the sticky `_excluded` set keeps *live* backends `down;` while the autoscaler still counts them running (D2). The lone divergent literal is the cluster-client constructor default `max_backends_ceiling=10` (`cluster_client.py:193`), overridden to 5 in deployment but a latent ceiling if `provision()` ever ran on a default client.

### Q3 — Is the lb-sidecar the root cause of most/all failures?
**REFINED — largely INNOCENT as a *root* cause; it is the actuator and the recovery bottleneck, not the originator.** *Case FOR* (steelman): the only file the sidecar writes carries two real backends benched indefinitely with zero `down;`→active transitions, and it structurally cannot self-heal them. *Case AGAINST* (decisive): (1) the sidecar did not **author** the exclusions — the detector's `latency_max_dev` channel did (every benching verdict is `model=trend_rule, metric=latency_max_dev`); (2) those exclusions are provably **load-driven** (backend-1 benched 117 s before the only injection, which targeted a different backend); (3) the recovery the sidecar can't perform is itself **starved upstream** (query-dropout defeats the detector's `recovery_reinclude`); (4) the **flap is the autoscaler's** (`controllers.py:173-175`), faithfully mirrored by `handle_scale`; (5) the catastrophic sidecar 502 path **did not fire** in any current-branch batch. **Conclusion:** the sidecar owns exactly one genuine critical contribution — the **passive no-recovery trap (D2)** — plus lower-severity gaps (D8, D10-latent, the membership-guard narrowing). The roots are the detector channels (D3) and the autoscaler flap (D1). Fixing only the sidecar mirrors the prior detector-only attempt that regressed errors 2.31%→13.9%→46.9% — *consistent with re-admitting backends into a still-flapping autoscaler*, which validates the coupling thesis.

### Q4 — SOT conformance: where the sidecar makes a decision rather than rendering one.
See **§6** for the full per-site verdict. Net: of seven decision sites, **only the quorum guard (`runloop.py:455-488`) and its adapter twin (the all-down freeze/empty render, `nginx/__init__.py:200-287`) are true SOT violations** — the sidecar inventing pool-health policy it has no signal for, duplicating a decision the anomaly-detector's peer-suppressor already owns and makes better with per-backend medians. Both should move upstream into the detector, leaving the sidecar a fail-OPEN render invariant. The confidence gate (`:416-423`) and `safe_mode` revert (`:661-670`) *look* like business logic but are the exact hierarchy-enforcement duties the SOT delegates to the sidecar (`SOURCE_OF_TRUTH.html:1763,3194`) — conformant, provided thresholds stay policy-sourced (they do). The membership guard (`:527-533`), `handle_scale` membership (`:594-620`), and startup hydration (`app.py:192-245`) are rendering-correctness/reconstruction tasks the sidecar is uniquely positioned for; they stay, with the narrowing fixes noted (D7/D8 and "scope the membership guard to `unhealthy`").

### Q5 — Replace with Kubernetes?
**No-go during the measurement phase; scoped-go for actuation afterward.** See **§7** for the full mapping. The codebase is already seamed for it (`cluster_client.py:5,40-44` anticipates a `KubernetesClusterClient`; `docs/features/forecast-autoscale.md:64` calls HPA the Phase-2 shape). k8s cleanly absorbs **membership** (Service/Endpoints replaces `up`/`down;` and dissolves D8, D10, and the cluster-ceiling-at-boot finding), the **Docker scaler** (`Deployment.replicas` replaces the docker-socket lifecycle), and **liveness/health** (1:1 to probes). But it has **no native equivalent** for the three things the thesis measures: per-endpoint **weighted routing** (the RL plane's output surface — `Service`/`Endpoints` route uniformly), **peer-relative busy-vs-broken exclusion** (a readiness probe is context-free, no cohort median), and **forecast-band scaling** (HPA scales on *current* metrics, not a forward `confidence_upper` band). Mapping those onto k8s built-ins would *silently replace the components under study* — the flap and the no-recovery trap would vanish into HPA's stabilization window and readiness gating, not because they were fixed but because the decision plane was bypassed. **That confounds the experiment.** Post-thesis, a `KubernetesClusterClient` *scaler* (actuation only, decisions untouched) plus Service/Endpoints membership is a sensible, low-risk productionization step; the anomaly exclusion and forecast scaling must stay as the thesis's custom controllers — k8s may carry their *actuation* but must not supply their *decision*.

---

## 5. Instance-count / ownership map (Q2)

| Layer | `file:line` | What it owns | Hard-codes a count? |
|---|---|---|---|
| **policy.yaml** | `config/policy.yaml:5-6,8` | **Authoritative ceiling/floor/capacity** (`max_backends:5`, `min_backends:1`, `per_instance_capacity_rps:100`) — the only bounds the deployed `target` controller honours | yes (the authoritative source) |
| autoscaler (defaults) | `services/autoscaler/app.py:175-177` | `load_policy` fallbacks `1`/`5`/`100` used only if policy.yaml omits a key | yes (fallback only) |
| autoscaler (live count) | `app.py:561,626,805`; `cluster_client.py:220-221` | **Live running count** via `cluster.get_backend_count()` — read from Docker, never assumed | no |
| autoscaler (clip) | `controllers.py:129` | Where the ceiling actually bites each decision (`_clip(need, min, max)`) | no (uses policy fields) |
| autoscaler (divergent literal) | `cluster_client.py:193` | Constructor default `max_backends_ceiling=10` — overridden to 5 at `app.py:264`; latent if `provision()` runs on a default client | **yes (divergent — flag)** |
| docker-compose | `docker-compose.yml:496-497` | **Steady-state physical pool** `replicas:5` (comment `:489-495`: must match nginx + policy) | yes (by design) |
| docker-compose (seed) | `docker-compose.yml:557-560` | lb-sidecar `ALL_BACKENDS` 5-name **cold-boot seed** (comment `:551`) | yes (seed/fallback only) |
| nginx static | `services/load-balancer/nginx/conf.d/upstream.conf` | 5 static `server` lines — cold-start render before the sidecar takes over | yes (cold-start only) |
| **lb-sidecar (rendered set)** | `lb-sidecar/runloop.py:132-155,164-179`; `app.py:119-129` | **Which backends render** — derived **dynamically from Docker** every message; seed is fallback-only | **no** |
| demo-ui (unrelated) | `docker-compose.yml:634-637` | `BACKEND_URLS` 5-name list — feeds the operator UI, not routing | yes (different service) |

**Why 5:** `config/policy.yaml:5 max_backends:5` is the single authoritative ceiling; `replicas:5`, the 5 nginx lines, and the 5-name seed are *deliberate mirrors* so NGINX name-resolution succeeds for the whole set and the toggle pool matches the policy ceiling. The sidecar does not fight this — it follows the live Docker pool.

---

## 6. SOT-conformance verdict (Q4) — where misplaced logic should move

SOT rule: "The LB sidecar — pure subscriber + config-renderer. No business logic" (`docs/SOURCE_OF_TRUTH.html:2061`), tempered by two delegated enforcement duties (`:1763` hierarchy order; `:3194` the confidence threshold).

| # | Decision | `file:line` | Kind | Belongs in | Action |
|---|---|---|---|---|---|
| 1 | **Quorum guard `(N+1)//2`** | `runloop.py:455-488,535-542` | **business logic** | anomaly-detector | **delete**; the detector's `peer_suppress_verdicts` (`runloop.py:376-432`) already owns "don't empty the pool under overload" with per-backend medians the sidecar lacks. (Empirically inert today — §3 — so removing it is low-risk.) |
| 2 | **All-down freeze / empty render** | `nginx/__init__.py:200-252,254-287` | **business logic** | anomaly-detector (decision) | remove the freeze/empty branching once #1 owns quorum; keep only a **fail-OPEN** render invariant (never emit a zero-server block — D10) |
| 3 | Confidence gate | `runloop.py:416-423` | hierarchy enforcement | **sidecar** (SOT `:3194`) | keep; threshold stays policy-sourced (it is) |
| 4 | Membership guard | `runloop.py:527-533` | render-correctness | **sidecar** | keep; **narrow to `unhealthy`** so a `healthy` verdict always clears `_excluded` |
| 5 | `handle_scale` membership + flatten | `runloop.py:594-620` | render (mostly) | **sidecar** | keep membership-from-Docker; change flatten→merge (preserve surviving RL weights) |
| 6 | `safe_mode` equal-weight revert | `runloop.py:661-670` | hierarchy enforcement | **sidecar** (SOT `:1763`) | keep |
| 7 | Startup hydration / conf re-import | `app.py:192-245`; `nginx/__init__.py:380-445` | reconstruction | sidecar (mechanics) / detector (intent) | keep; make **DB authoritative over the conf parse** (D8) |

**Net:** only the quorum guard and its renderer twin are true violations; both move into the detector's peer-suppressor. Everything else is either delegated enforcement (conformant) or rendering-correctness (the sidecar is uniquely positioned for it).

---

## 7. Kubernetes go/no-go (Q5) — reasoned

| Responsibility | Today | k8s equivalent | Disappears? | Verdict |
|---|---|---|---|---|
| Upstream **membership** (`up`/`down;`) | `NginxAdapter` rewrites `upstream.conf` | Service / Endpoints / EndpointSlice readiness gating | **Yes** — kills the rewrite/reload/DNS-preflight/all-down-placeholder/quorum apparatus; dissolves D8, D10, cluster-ceiling-at-boot | clean win |
| **Weighted routing** (`weight=`) | RL → sidecar → `weight=` | *None native* — Service/Endpoints route uniformly | **No** — needs an L7 proxy (Envoy/Gateway-API weights) + a custom controller translating `RoutingRecommendation` | k8s buys nothing |
| **Anomaly exclusion** | peer-relative busy-vs-broken (`runloop.py:366-433`) | readiness/liveness probes | **No** — a probe is per-pod, context-free; no cohort median, no overload-vs-fault discrimination | **most dangerous mapping**; must stay custom |
| **Scaler** | `DockerClusterClient` start/stop/provision | HPA / KEDA edits `replicas` | **Yes** at the actuator layer (docker-socket lifecycle gone) | clean win for actuation |
| **Scaling decision** | `decide_target` on forecast band | HPA reactive on current metric | **No** — HPA can't consume a forward `confidence_upper`; KEDA needs a custom scaler | k8s buys nothing |
| **Liveness/health** | `/health` checks, `#163` staleness | livenessProbe/readinessProbe | **Yes** — 1:1 | pure win |

**Go/no-go.** **No-go for a wholesale replacement while the thesis is measuring the RL/forecast plane.** The decision plane has *no native k8s equivalent* (weighted routing, peer-relative exclusion, forecast-band scaling), so migration forces reimplementing all three as custom controllers — high cost, zero algorithmic benefit — while creating the acute risk that HPA/readiness probes **silently substitute** for the components under study. The bench evidence (the 5↔4 flap, the no-recovery trap) *is the data of the thesis*; HPA's stabilization window would simply absorb the flap, and a readiness probe would either never pull a busy-but-200-answering backend or force you to reimplement the whole detector — either way the measurement is invalidated. **Scoped-go post-thesis:** the `ClusterClient` ABC seam (`cluster_client.py:116-165`) makes a `KubernetesClusterClient` *scaler* a bounded, low-risk first step (actuation only), and Service/Endpoints membership is a clean win — provided anomaly exclusion and forecast scaling stay as the thesis's custom controllers (k8s carries their *actuation*, never their *decision*).

---

## 8. Recommended fix sequence (honoring the coupling)

The prior detector-only fix regressed errors **2.31% → 13.9% → 46.9%** precisely because it re-admitted/kept overloaded backends on the premise the autoscaler would add capacity — while the autoscaler flapped instead. **Order matters: fix the flap first, then recovery, then the over-exclusion at source.**

1. **[DONE — committed `e290b46`] D1 — close the autoscaler flap dead-zone.** Size both scale directions on `max(predicted, offered)` in `decide_target` (`controllers.py`). The prerequisite: it makes the pool *hold capacity under load*. *(65/65 unit tests, deployed.)* **Measured (§2.1):** removes the flap (flat pool, 0 transitions, container count held at 5) but does **not** reduce errors alone — D2/D3 then bind. Keep it (reverting re-introduces the flap); pair with D11 (the unified signal now holds the pool at max at low load).
2. **D11 — sanitize the forecast band.** Clamp `confidence_upper` (cap relative to `predicted`, reject non-physical spikes like the observed `10999`) so the now-load-bearing scale-in signal can't pin the pool at `max`. Pairs with D1.
3. **[DONE — committed `07f861d`] D2 — restore recovery, now that capacity holds.** `recovery_reinclude_silent` + Pass 3b drive recovery off the detector's own per-backend state instead of metrics-query presence (the query-dropout fix), re-probing a benched-and-silent backend each `recovery_window_seconds` (clock re-armed, stability-gate memory reset). *(45/45 detector tests, deployed.)* **Measured (§2.2):** re-admits fire correctly and D1+D2 *can* reach near-zero (run-01 = 222), but the benefit is gated by D3 (re-benching under overload) — so this is necessary, not sufficient. Companion: narrow the sidecar membership guard to `unhealthy` (D-list) and fix the benched-state carryover (§6/bench) so per-run measurement is clean.
4. **[DONE — committed `018061c`] D3 + D9 — stop scoring overload as illness at source.** Margin-primary peer-suppressor: **no fraction gate** (it caused the cascade — the suppressor waited for ≥50% of the pool to be excludable while exclusions picked off backends one at a time), the per-backend margin as the sole overload-vs-fault discriminator, and typical (rolling-mean) not window-MAX latency. This also resolves the **error-channel** over-exclusion at the suppressor — the only layer with the load/peer context to tell 503-shedding from a real fault. **Measured (§2.3):** mean active-pool under load 3.00 → 3.80; the over-exclusion is largely closed and the runs are far more consistent. The remaining error floor is now the **saturation config**, not a control-loop defect.
5. **The remaining lever is experiment design, not code.** With D1+D2+D3 the control loop holds the pool it has; the residual ~122k is the saturation tax of `max_backends=5` at the 200-user peak with the controller's headroom clipped. Closing it means giving the pool headroom (`max_backends` 6–7, higher per-instance capacity, or a lower peak) — a deliberate change to *what the benchmark measures*, left to the operator. Everything below this point is robustness/hygiene, not a failure driver.
6. **D7, D8, D10, §6 #1-#2 — durability & SOT (robustness, not failure drivers).** Make manual isolates sticky; make DB hydration authoritative over the conf parse; render **fail-OPEN** (never emit a zero-server block); move the quorum guard upstream into the detector.
7. **Bench methodology (D4, D5, analysis findings) — measurement caveats, NOT result-improving changes.** These corrupt *interpretation*, not the system: the phase-D injection targets a non-routed backend (D4); the cooldown override is inert (D5); the `replica count` metric conflates scaling with benching; the SSE collector leaks prior-run backlog; benched state carries across runs. They explain the per-run variance and why some headline RQ4 numbers are contaminated. **Per the operator's instruction these are left untouched** — fixing them would clean the *measurement*, not the control loop, and must not be conflated with reducing real failures.

---

### Appendix — what would settle the open items
- **D1's residual (pin-at-max under an inflated band): CONFIRMED by the after-batch.** The pool holds 5 containers even at idle (live post-bench: five running, none drained), so D11 (band sanitation) is required before the *scale-in* half of RQ4 is observable. The C/B 503s did not drop because **D2/D3 bind** (2 of 5 benched → 3 active), not because D1 failed — the flap is provably gone.
- **The decisive next measurement — D2 recovery driven off registry membership:** a 2-run bench with D2 landed on top of D1, checking that (a) the benched backends 1/3 return to `weight=1` within one grace window without re-flapping, (b) `active` rises from 3 toward 5, and (c) the C-phase 503s finally fall. This is the experiment that should convert D1's *held* capacity into *served* capacity; if errors drop sharply there, the coupling fix is complete.
- **D10 (latent 502):** reproduced only in pre-quorum-guard batches; a fault-injection that empties the pool on `f92632c` would confirm the fail-OPEN fix closes it.
