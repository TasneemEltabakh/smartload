#!/usr/bin/env python3
"""
scripts/bootstrap-config.py
───────────────────────────
Render the runtime config from the single-file client bootstrap.

Reads config/smartload.yml (the externally-shared integration shape), validates
it, and writes config/policy.yaml — preserving the existing policy_version so a
re-render never rolls a live policy back. The env defaults the file implies
(POLL_INTERVAL_SECONDS, RL_MODE) are printed for the operator to merge into .env.

When config/smartload.yml is absent this is a no-op (exit 0) — the legacy
policy.yaml + .env setup keeps working, so wiring this into a pipeline is safe
before every client has adopted the single-file shape.

Usage:
    python scripts/bootstrap-config.py                 # render policy.yaml + print env
    python scripts/bootstrap-config.py --check         # validate only, no writes
    python scripts/bootstrap-config.py --print-env     # print env lines only, no writes
    python scripts/bootstrap-config.py \\
        --in config/smartload.yml --policy config/policy.yaml

Exit codes: 0 ok / no-op · 1 validation error · 2 bad invocation.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile

# Make services/shared importable whether run from the repo root or elsewhere.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SERVICES = os.path.join(_REPO_ROOT, "services")
if _SERVICES not in sys.path:
    sys.path.insert(0, _SERVICES)

from shared import config_loader as cl  # noqa: E402


def _rel(path: str) -> str:
    """Repo-relative display path, falling back to the path itself when it can't
    be made relative — os.path.relpath raises on Windows across drive letters
    (e.g. --policy on D: while the repo is on G:)."""
    try:
        return os.path.relpath(path, _REPO_ROOT)
    except ValueError:
        return path


def _atomic_write_yaml(path: str, policy: dict) -> None:
    """Write ``policy`` to ``path``, mirroring policy-manager's atomic writer so
    the rendered file is byte-compatible with what the API would produce."""
    import yaml

    parent = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".policy.", suffix=".yaml.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(policy, f, default_flow_style=False, sort_keys=True)
        try:
            os.replace(tmp, path)
        except OSError as exc:
            if exc.errno not in (16, 13):  # EBUSY (bind mount) / EACCES
                raise
            with open(path, "w", encoding="utf-8") as f:
                yaml.safe_dump(policy, f, default_flow_style=False, sort_keys=True)
            try:
                os.unlink(tmp)
            except OSError:
                pass
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_existing_policy(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    import yaml

    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Render runtime config from config/smartload.yml")
    parser.add_argument("--in", dest="src", default=None,
                        help="bootstrap file (default: config/smartload.yml at repo root)")
    parser.add_argument("--policy", dest="policy", default=None,
                        help="policy.yaml to render (default: config/policy.yaml at repo root)")
    parser.add_argument("--check", action="store_true", help="validate only; write nothing")
    parser.add_argument("--print-env", action="store_true", help="print env lines only; write nothing")
    args = parser.parse_args(argv)

    src = args.src or os.path.join(_REPO_ROOT, cl.DEFAULT_SMARTLOAD_PATH)
    policy_path = args.policy or os.path.join(_REPO_ROOT, "config", "policy.yaml")

    raw = cl.read_file(src)
    if raw is None:
        print(f"no {_rel(src)} - keeping existing policy.yaml + .env (no-op)")
        return 0

    try:
        cl.validate(raw)
    except cl.SmartLoadConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    env = cl.to_env(raw)

    if args.print_env:
        for k, v in env.items():
            print(f"{k}={v}")
        return 0

    if args.check:
        print(f"ok: {_rel(src)} is valid")
        return 0

    existing = _load_existing_policy(policy_path)
    merged = cl.merge_policy(existing, raw)
    _atomic_write_yaml(policy_path, merged)

    print(f"rendered {_rel(policy_path)} from "
          f"{_rel(src)} (policy_version={merged.get('policy_version')})")
    if env:
        print("add these to your .env (or let compose inject them):")
        for k, v in env.items():
            print(f"  {k}={v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
