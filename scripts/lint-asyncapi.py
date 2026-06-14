"""
scripts/lint-asyncapi.py
────────────────────────
Anti-drift lint for the canonical AsyncAPI spec.

Greps services/ for Redis pub/sub channel literals (`"smartload.<topic>"`) —
the same discovery the channel-registry lint uses — and asserts every channel
appears as a `channels.*.address` in docs/asyncapi/smartload-v1.yaml.

Pairs with scripts/lint-redis-channels.py (channel → registry markdown) and
scripts/lint-openapi.py (HTTP route → OpenAPI spec). Permissive by default;
pass --strict for CI gating.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVICES_DIR = REPO_ROOT / "services"
SPEC = REPO_ROOT / "docs" / "asyncapi" / "smartload-v1.yaml"

CHANNEL_RE = re.compile(r"""["']smartload\.[a-z_]+["']""")

# `smartload.*` literals that are Docker label KEYS (autoscaler/cluster_client.py),
# not Redis pub/sub channels — same exclusion as lint-redis-channels.py. The
# regex can't tell them apart, so list them here to avoid a false positive.
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


def channels_in_spec() -> set[str]:
    if not SPEC.is_file():
        return set()
    text = SPEC.read_text(encoding="utf-8")
    # channels.<name>.address: smartload.<topic>
    return set(re.findall(r"address:\s*(smartload\.[a-z_]+)", text))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    source = find_channels_in_source()
    spec = channels_in_spec()

    missing = sorted(source - spec)
    if missing:
        for channel in missing:
            print(
                f"WARN: {channel} is published/consumed in services/ but is not "
                f"a channel in docs/asyncapi/smartload-v1.yaml"
            )
        if args.strict:
            return 1
    else:
        print("OK: every smartload.* channel in services/ is documented in AsyncAPI")
    return 0


if __name__ == "__main__":
    sys.exit(main())
