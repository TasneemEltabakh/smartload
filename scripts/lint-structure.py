"""
scripts/lint-structure.py
─────────────────────────
The CI guardrail that makes SmartLoad's repository structure a contract
instead of decoration.

Default mode is PERMISSIVE — violations are reported as warnings and the
script exits 0. Pass --strict to flip to enforcing mode (exit non-zero on
any violation). CI flips the flag once the team has settled the convention.

Checks:
  1. Every services/<name>/ has README.md
  2. Every services/<svc>/engines/<engine>/ has README.md (test_*.py warned)
  3. Every services/<svc>/policies/<policy>/ has README.md (test_*.py warned)
  4. Every services/shared/lb_adapters/<adapter>/ has README.md
     (base.py is excluded)
  5. Every tests/e2e/<feature>/ has a sibling at:
        docs/features/<feature>.md
        examples/scenarios/<feature>.py
  6. Every examples/scenarios/<feature>.py is runnable
     (Python syntax check; no actual stack required at lint time)

Out of scope here (separate lints):
  - scripts/lint-redis-channels.py  (anti-drift for docs/redis-channels.md)
  - scripts/lint-openapi.py         (anti-drift for docs/openapi/smartload-v1.yaml)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

violations: list[str] = []


def warn(msg: str) -> None:
    violations.append(msg)
    print(f"WARN: {msg}")


def check_services_have_readmes() -> None:
    services_dir = REPO_ROOT / "services"
    if not services_dir.is_dir():
        return
    for svc in sorted(services_dir.iterdir()):
        if not svc.is_dir() or svc.name in {"shared", "__pycache__"}:
            continue
        if not (svc / "README.md").is_file():
            warn(f"missing README.md in services/{svc.name}/")


def check_plugin_folders(parent_relpath: str, plugin_kind: str) -> None:
    parent = REPO_ROOT / parent_relpath
    if not parent.is_dir():
        return
    for plugin in sorted(parent.iterdir()):
        if not plugin.is_dir() or plugin.name.startswith("__"):
            continue
        if not (plugin / "README.md").is_file():
            warn(f"missing README.md in {parent_relpath}/{plugin.name}/")
        has_test = any(p.name.startswith("test_") and p.suffix == ".py" for p in plugin.iterdir())
        if not has_test:
            warn(f"no test_*.py in {parent_relpath}/{plugin.name}/ ({plugin_kind} plugin)")


def check_engines_and_policies() -> None:
    services_dir = REPO_ROOT / "services"
    if not services_dir.is_dir():
        return
    for svc in sorted(services_dir.iterdir()):
        if not svc.is_dir():
            continue
        engines_dir = svc / "engines"
        if engines_dir.is_dir():
            check_plugin_folders(f"services/{svc.name}/engines", "engine")
        policies_dir = svc / "policies"
        if policies_dir.is_dir():
            check_plugin_folders(f"services/{svc.name}/policies", "policy")


def check_lb_adapters() -> None:
    adapters = REPO_ROOT / "services" / "shared" / "lb_adapters"
    if not adapters.is_dir():
        return
    for sub in sorted(adapters.iterdir()):
        if not sub.is_dir() or sub.name.startswith("__"):
            continue
        if not (sub / "README.md").is_file():
            warn(f"missing README.md in services/shared/lb_adapters/{sub.name}/")


def check_e2e_siblings() -> None:
    e2e_dir = REPO_ROOT / "tests" / "e2e"
    features_dir = REPO_ROOT / "docs" / "features"
    scenarios_dir = REPO_ROOT / "examples" / "scenarios"
    if not e2e_dir.is_dir():
        return
    for feat in sorted(e2e_dir.iterdir()):
        if not feat.is_dir() or feat.name.startswith("__"):
            continue
        if not (features_dir / f"{feat.name}.md").is_file():
            warn(f"tests/e2e/{feat.name}/ exists but docs/features/{feat.name}.md does not")
        scenario_folder = scenarios_dir / feat.name
        scenario_file = scenarios_dir / f"{feat.name}.py"
        has_folder = scenario_folder.is_dir() and any(scenario_folder.glob("*.py"))
        has_file = scenario_file.is_file()
        if not (has_folder or has_file):
            warn(
                f"tests/e2e/{feat.name}/ exists but no scenarios at "
                f"examples/scenarios/{feat.name}/ or examples/scenarios/{feat.name}.py"
            )


def check_scenarios_syntax() -> None:
    scenarios_dir = REPO_ROOT / "examples" / "scenarios"
    if not scenarios_dir.is_dir():
        return
    import py_compile
    for script in sorted(scenarios_dir.glob("*.py")):
        try:
            py_compile.compile(str(script), doraise=True)
        except py_compile.PyCompileError as exc:
            warn(f"syntax error in examples/scenarios/{script.name}: {exc.msg}")


def main() -> int:
    parser = argparse.ArgumentParser(description="SmartLoad structure lint")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero on any violation",
    )
    args = parser.parse_args()

    check_services_have_readmes()
    check_engines_and_policies()
    check_lb_adapters()
    check_e2e_siblings()
    check_scenarios_syntax()

    if not violations:
        print("OK: structure lint passed with no warnings")
        return 0

    print()
    print(f"{len(violations)} violations")
    if args.strict:
        print("FAIL: --strict enabled, exiting non-zero")
        return 1
    print("permissive mode; flip to --strict in CI once warnings are resolved")
    return 0


if __name__ == "__main__":
    sys.exit(main())
