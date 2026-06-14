"""
examples/scenarios/named-strategies/named_strategies_walk.py
──────────────────────────────────────────────────────────────
Proves the named-strategies slice (#150) end-to-end.

Walks the alias endpoint: snapshots the current policy, applies a named
strategy (ai-hybrid) via POST /api/v1/policy/strategy, confirms the derived
strategy_name on the GET response, shows the reverse-map's documented
many-to-one collapse (forecast-aware -> latency-aware representative), proves
a directly-set primitive combination reports "custom", checks the audit row
carries the strategy intent, then flips to safe-fallback (the kill switch) and
restores the starting policy on the way out.

Exit code:
  0 — observed expected behaviour
  1 — connection failure, missing audit row, or assertion failure

Usage:
  python examples/scenarios/named-strategies/named_strategies_walk.py
  python examples/scenarios/named-strategies/named_strategies_walk.py \\
      --policy-url http://localhost:8086
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from smartload_client import SmartLoadClient, SmartLoadError, ValidationError


def _fail(msg: str) -> int:
    print(f"FAIL: {msg}", file=sys.stderr)
    return 1


def _await_audit_actor(client: SmartLoadClient, actor: str,
                       deadline_s: float = 5.0) -> bool:
    """Poll the policy audit until a row with the exact actor appears."""
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        for row in client.audit_policy(limit=20):
            if row.get("actor") == actor:
                return True
        time.sleep(0.2)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Named-strategies slice walkthrough")
    parser.add_argument("--policy-url",
                        default=os.environ.get("POLICY_URL", "http://localhost:8086"))
    args = parser.parse_args()

    print("== named-strategies slice walkthrough ==")
    print(f"policy-manager: {args.policy_url}")
    print()

    with SmartLoadClient(base_url=args.policy_url) as c:

        # 1. Snapshot the starting policy so we can restore it at the end.
        try:
            baseline = c.get_policy()
        except SmartLoadError as exc:
            return _fail(f"could not read policy: {exc}")
        restore = {
            k: v for k, v in baseline.items()
            if k not in ("policy_version", "strategy_name")
        }
        print(f"  ok: baseline policy v{baseline['policy_version']} "
              f"(strategy_name={baseline.get('strategy_name')!r})")

        # 2. Apply ai-hybrid via the alias endpoint.
        print("  -> set strategy ai-hybrid (actor=named_walk)")
        try:
            r = c.set_strategy("ai-hybrid", actor="named_walk")
        except SmartLoadError as exc:
            return _fail(f"set_strategy failed: {exc}")
        if r.get("status") not in ("updated", "no-op"):
            return _fail(f"unexpected status: {r}")
        if r["policy"]["operating_mode"] != "hybrid":
            return _fail(f"operating_mode {r['policy']['operating_mode']!r} != 'hybrid'")
        if "rl_mode" in r["policy"]:
            return _fail("rl_mode leaked into policy (must be a deploy-time pin only)")
        print(f"  ok: applied (operating_mode={r['policy']['operating_mode']}, "
              f"safe_mode={r['policy']['safe_mode']}, "
              f"recommended RL_MODE={r['recommended_rl_mode']!r})")

        # 3. Derived strategy_name on GET. ai-hybrid shares the hybrid+safe_mode
        #    primitive pair with latency/forecast/anomaly-aware, so the reverse
        #    map returns the representative name (latency-aware), not ai-hybrid.
        policy = c.get_policy()
        if policy.get("strategy_name") != "latency-aware":
            return _fail(
                f"strategy_name {policy.get('strategy_name')!r} != 'latency-aware' "
                f"(the representative for the hybrid+safe_mode=false pair)"
            )
        print(f"  ok: GET strategy_name={policy['strategy_name']!r} "
              f"(representative for the hybrid primitive pair)")

        # 4. Many-to-one collapse: forecast-aware also reverse-maps to the
        #    representative latency-aware.
        c.set_strategy("forecast-aware", actor="named_walk")
        policy = c.get_policy()
        if policy.get("strategy_name") != "latency-aware":
            return _fail(
                f"forecast-aware should reverse-map to latency-aware, "
                f"got {policy.get('strategy_name')!r}"
            )
        print("  ok: forecast-aware collapses to latency-aware (documented reverse map)")

        # 5. Direct primitives that match no documented strategy -> "custom".
        c.set_policy({"operating_mode": "rl-only", "safe_mode": False},
                     actor="named_walk")
        policy = c.get_policy()
        if policy.get("strategy_name") != "custom":
            return _fail(
                f"rl-only primitives should report strategy_name='custom', "
                f"got {policy.get('strategy_name')!r}"
            )
        print("  ok: rl-only primitives report strategy_name='custom'")

        # 6. Unknown strategy name is rejected.
        try:
            c.set_strategy("round_robin")  # underscore is not the canonical name
            return _fail("unknown strategy should have raised ValidationError")
        except ValidationError as exc:
            print(f"  ok: unknown strategy rejected (field={exc.field})")

        # 7. Audit row carries the strategy intent in the actor field.
        c.set_strategy("safe-fallback", actor="named_walk")
        c.set_strategy("ai-hybrid", actor="named_walk")
        if not _await_audit_actor(c, "strategy:ai-hybrid:named_walk"):
            return _fail(
                "policy_changes row with actor 'strategy:ai-hybrid:named_walk' "
                "not found within 5s"
            )
        print("  ok: audit row records strategy intent "
              "(actor='strategy:ai-hybrid:named_walk')")

        # 8. Kill switch + restore baseline.
        r = c.set_strategy("safe-fallback", actor="named_walk")
        if not r["policy"]["safe_mode"]:
            return _fail("safe-fallback should set safe_mode=true")
        print("  ok: safe-fallback engaged (safe_mode=true, classical-only)")
        try:
            c.set_policy(restore, actor="named_walk-restore")
            print("  ok: baseline policy restored")
        except SmartLoadError as exc:
            print(f"  warning: restore failed: {exc}", file=sys.stderr)

    print()
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
