"""
20-line "hello world" for the SmartLoad client.

Connects to a running SmartLoad stack, reads the current operating policy,
and prints a summary.

Run against the default local stack:
    docker compose up -d
    python clients/python/examples/quickstart.py
"""

from __future__ import annotations

from smartload_client import SmartLoadClient, SmartLoadError


def main() -> int:
    with SmartLoadClient(base_url="http://localhost:8086") as c:
        try:
            policy = c.get_policy()
        except SmartLoadError as exc:
            # 404 from the live service when policy.yaml is empty / missing.
            print(f"could not read policy: {exc}")
            print("hint: POST a baseline to /api/v1/policy first")
            return 1
    print(f"operating_mode = {policy.get('operating_mode')}")
    print(f"safe_mode      = {policy.get('safe_mode')}")
    print(f"min_backends   = {policy.get('min_backends')}")
    print(f"max_backends   = {policy.get('max_backends')}")
    print(f"policy_version = {policy.get('policy_version')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
