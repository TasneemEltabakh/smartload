# =============================================================================
#  sync-ui-kit.ps1
# -----------------------------------------------------------------------------
#  Vendors the canonical design-system kit into both web apps.
#
#  The two apps build in separate Docker contexts and each web build stage
#  copies only its own web/ directory, so the kit cannot be a live shared
#  package. Instead the single source of truth lives in ui-kit/src/ and this
#  script mirror-copies it into each app's web/src/ui/. Run it after any edit to
#  ui-kit/src/ so both vendored copies stay in lockstep. Imports inside each app
#  stay on the local relative path "./ui/...".
#
#  Usage:  pwsh ./scripts/sync-ui-kit.ps1
# =============================================================================

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$source   = Join-Path $repoRoot "ui-kit/src"

$targets = @(
    (Join-Path $repoRoot "services/operator-ui/web/src/ui"),
    (Join-Path $repoRoot "tools/demo-ui/web/src/ui")
)

if (-not (Test-Path $source)) {
    throw "Canonical kit source not found: $source"
}

foreach ($target in $targets) {
    if (Test-Path $target) {
        Remove-Item -Recurse -Force $target
    }
    New-Item -ItemType Directory -Force -Path $target | Out-Null
    Copy-Item -Recurse -Force -Path (Join-Path $source "*") -Destination $target
    $count = (Get-ChildItem -Recurse -File $target | Measure-Object).Count
    Write-Host "synced $count files -> $target"
}

Write-Host "ui-kit sync complete."
