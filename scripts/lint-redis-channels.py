"""
scripts/lint-redis-channels.py
──────────────────────────────
Anti-drift lint for the Redis channel registry.

Greps services/ for the regex `["']smartload\.[a-z_]+["']`. Every channel
name found in source must appear in docs/redis-channels.md.

Permissive by default. Pass --strict for CI gating.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVICES_DIR = REPO_ROOT / "services"
REGISTRY = REPO_ROOT / "docs" / "redis-channels.md"

CHANNEL_RE = re.compile(r"""["']smartload\.[a-z_]+["']""")

# `smartload.*` strings that are Docker label KEYS (autoscaler/cluster_client.py),
# not Redis pub/sub channels. The regex above can't tell them apart — both are
# quoted `smartload.<word>` literals — so list them here. Without this the lint
# false-positives, demanding these labels be documented as channels (they aren't).
NON_CHANNEL_LITERALS = {"smartload.dynamic", "smartload.role"}


def find_channels_in_source() -> set[str]:
    found = set()
    for path in SERVICES_DIR.rglob("*.py"):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for match in CHANNEL_RE.findall(text):
            found.add(match.strip("'\""))
    return found - NON_CHANNEL_LITERALS


def channels_listed_in_registry() -> set[str]:
    if not REGISTRY.is_file():
        return set()
    text = REGISTRY.read_text(encoding="utf-8")
    return set(re.findall(r"`smartload\.[a-z_]+`", text)) | set(
        m.strip("`") for m in re.findall(r"`smartload\.[a-z_]+`", text)
    ) | set(re.findall(r"smartload\.[a-z_]+", text))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    source = find_channels_in_source()
    registry = channels_listed_in_registry()

    missing = sorted(source - registry)
    if missing:
        for ch in missing:
            print(f"WARN: {ch} appears in services/ but not in docs/redis-channels.md")
        if args.strict:
            return 1
    else:
        print("OK: every Redis channel in services/ is documented")
    return 0


if __name__ == "__main__":
    sys.exit(main())
