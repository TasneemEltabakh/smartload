"""
scripts/lint-openapi.py
───────────────────────
Anti-drift lint for the canonical OpenAPI spec.

Greps services/ for Flask route decorators (`@app.route("/api/v1/...")`,
`@bp.route("/api/v1/...")`, `@app.get/post(...)`). Every route found
must appear in docs/openapi/smartload-v1.yaml.

Permissive by default. Pass --strict for CI gating.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVICES_DIR = REPO_ROOT / "services"
SPEC = REPO_ROOT / "docs" / "openapi" / "smartload-v1.yaml"

ROUTE_RE = re.compile(
    r"""@\w+\.(?:route|get|post|put|delete|patch)\s*\(\s*["'](?P<path>/api/v1/[^"'?]+)["']"""
)


def find_routes_in_source() -> set[str]:
    found = set()
    for py in SERVICES_DIR.rglob("*.py"):
        try:
            text = py.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for match in ROUTE_RE.finditer(text):
            found.add(match.group("path").rstrip("/"))
    return found


def paths_in_spec() -> set[str]:
    if not SPEC.is_file():
        return set()
    text = SPEC.read_text(encoding="utf-8")
    return set(re.findall(r"\n  (/api/v1/[^\s:]+):", text))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    source = {p.rstrip("/") for p in find_routes_in_source()}
    spec = {p.rstrip("/") for p in paths_in_spec()}

    missing = sorted(source - spec)
    if missing:
        for p in missing:
            print(f"WARN: {p} exists in services/ but not in docs/openapi/smartload-v1.yaml")
        if args.strict:
            return 1
    else:
        print("OK: every /api/v1 route in services/ is documented in OpenAPI")
    return 0


if __name__ == "__main__":
    sys.exit(main())
