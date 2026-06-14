#!/usr/bin/env bash
# =============================================================================
#  sync-ui-kit.sh
# -----------------------------------------------------------------------------
#  Vendors the canonical design-system kit into both web apps (non-Windows).
#
#  The two apps build in separate Docker contexts and each web build stage
#  copies only its own web/ directory, so the kit cannot be a live shared
#  package. Instead the single source of truth lives in ui-kit/src/ and this
#  script mirror-copies it into each app's web/src/ui/. Run it after any edit to
#  ui-kit/src/ so both vendored copies stay in lockstep. Imports inside each app
#  stay on the local relative path "./ui/...".
#
#  Usage:  bash ./scripts/sync-ui-kit.sh
# =============================================================================
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
source_dir="$repo_root/ui-kit/src"

targets=(
  "$repo_root/services/operator-ui/web/src/ui"
  "$repo_root/tools/demo-ui/web/src/ui"
)

if [ ! -d "$source_dir" ]; then
  echo "Canonical kit source not found: $source_dir" >&2
  exit 1
fi

for target in "${targets[@]}"; do
  rm -rf "$target"
  mkdir -p "$target"
  cp -R "$source_dir/." "$target/"
  count=$(find "$target" -type f | wc -l | tr -d ' ')
  echo "synced $count files -> $target"
done

echo "ui-kit sync complete."
