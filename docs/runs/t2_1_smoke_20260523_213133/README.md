# T2.1 Smoke Run — 2026-05-23T21:31:33

**RESULT: PASSED** — All smoke steps completed successfully.

Artifacts proving the T2.1 lb-sidecar end-to-end pipeline:

| File | Contents |
|---|---|
| `shadow_envelope.json` | One captured `smartload.routing` envelope in `mode=shadow`, proving rl-engine publishes and Redis delivers |
| `upstream_conf_shadow.txt` | `upstream.conf` snapshot after shadow envelope received, proving lb-sidecar did NOT update weights (shadow gate) |
| `upstream_conf_active.txt` | `upstream.conf` after Step 6 equal-weight restore; Step 5 confirmed backend-1=99 weight during actuation |
| `walk_output.txt` | Full output of `lb_sidecar_walk.py` — all 6 steps passed |
| `lb_sidecar.log` | lb-sidecar container logs from the smoke run |
| `lb_sidecar_final.log` | lb-sidecar container logs at end of run, showing `[lb-sidecar] routing applied (5 backends)` |
| `rl_engine.log` | rl-engine container logs from the smoke run |
| `compose_ps.txt` | `docker compose ps` output — all 21 services running |

## What this proves

1. **RL publish** — rl-engine generates `RoutingRecommendation` envelopes when `RL_RUNLOOP_ENABLED=true`
2. **Redis transport** — envelope arrives on `smartload.routing` channel
3. **Sidecar consume** — lb-sidecar subscribes and receives the envelope
4. **Shadow gate** — lb-sidecar does NOT rewrite `upstream.conf` for `mode=shadow` envelopes
5. **Active actuation** — manual `mode=active` envelope via Redis causes lb-sidecar to apply PPO-derived weights (backend-1=99 from score=0.99); `nginx -s reload` triggered via docker exec
6. **NGINX serves traffic** — 20/20 HTTP 200 from `curl localhost:8080/` throughout the test
7. **Clean error scan** — no ERROR/TRACEBACK/EXCEPTION in lb-sidecar, rl-engine, or load-balancer logs
