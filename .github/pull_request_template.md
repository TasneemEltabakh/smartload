<!--
Thanks for sending a PR! Use the checklist below as a discipline reminder —
the boxes aren't gated by a hook, but reviewers will look.

Pattern reference: tests/README.md and tests/integration/_template_acceptance.py.
Existing exemplar: PR #158 (Isolation Forest).
-->

## What this PR does

<!-- 2–3 sentences. Optimise for the reviewer, not for the author. -->

## Why now

<!-- Link the SOT section / GitHub issue this PR closes or advances.
Example: "Closes #117. SOT §22 v1.0.7ac changelog row covers this." -->

## Tests

<!-- Per #117 acceptance-test pattern — two artefacts per product task. -->

- [ ] Unit test added (or task is meta-infra / docs-only)
- [ ] Live-stack acceptance test added or extended
- [ ] Test docstring cites the SOT section it maps to
- [ ] CI green (or known-flake noted in PR description)

## Docs

<!-- Three canonical docs should stay synced. Tick the ones you touched. -->

- [ ] SOT (`docs/SOURCE_OF_TRUTH.html`) — changelog row + status badge if user-visible
- [ ] `docs/PROJECT_WALKTHROUGH.md` — narrative tour follows the code
- [ ] `README.md` — touched only when an outsider-facing surface changes
- [ ] `docs/PROJECT_STATE.md` — sprint table + delta paragraph if this closes a tracked issue
- [ ] Feature manifest under `docs/features/<feature>.md` if the slice surface moves

## Out of scope

<!-- Anything reviewers might wonder why isn't in this PR. -->
