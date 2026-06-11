"""
tests/integration/_template_acceptance.py
──────────────────────────────────────────
Copy this file when adding a per-task acceptance test (#117 meta-infra).

Naming: rename the file from `_template_acceptance.py` to
`test_<feature>.py`. The leading underscore is what keeps this template
from being collected as a real test — once the file is named `test_...`
pytest picks it up.

Replace every TODO and the placeholder values (`_TASK_ID`,
`_SOT_SECTION`, `_DESCRIBES`) with your task's actual context. The file
as-shipped compiles, collects under pytest, and skips with a clear
reason so a fresh copy doesn't go red on day one.

This is paired with `tests/README.md`, which explains the
two-test-per-task pattern. Read that first if this is your first
acceptance test on the project.
"""

from __future__ import annotations

import time

import pytest
import requests

from tests.integration.conftest import REDIS_URL, SERVICE_URLS  # noqa: F401  (imported so a copy has the imports already wired)


# ── task identity (REPLACE THESE) ────────────────────────────────────────────
_TASK_ID = "TODO-x.x"                 # e.g. "T2.3" or "#101" or "N2.1"
_SOT_SECTION = "TODO §x.x"            # e.g. "§22 v1.0.7ab" or "§8.3 isolation_forest"
_DESCRIBES = "TODO short description" # one-line summary of what acceptance means here


@pytest.fixture(scope="module")
def task_context():
    """Single source of truth for the task identity, surfaced in skip /
    assertion messages so a reader of a CI log knows which SOT row the
    test maps to without opening the file."""
    return {
        "task_id": _TASK_ID,
        "sot_section": _SOT_SECTION,
        "describes": _DESCRIBES,
    }


# ── preconditions ────────────────────────────────────────────────────────────

def _engine_configured(service_url: str, expected_engine: str) -> bool:
    """Return True if the service's /health reports the engine we need.

    Tests that depend on a specific engine being loaded (e.g.
    ANOMALY_ENGINE=isolation_forest) should call this before doing the
    real work and skip with a clear reason if it returns False. See
    `tests/integration/test_isolation_forest_live_stack.py` for a
    fully-worked example."""
    try:
        body = requests.get(f"{service_url}/health", timeout=5).json()
    except (requests.RequestException, ValueError):
        return False
    return body.get("engine_type") == expected_engine


# ── the acceptance test (REPLACE THE BODY) ───────────────────────────────────

def test_acceptance(stack_ready, task_context):
    """One-paragraph docstring stating what the SOT acceptance criterion
    is and how this test demonstrates it. The reader of a failure log
    should be able to map the assertion straight to the spec without
    chasing through other files.

    Example skeleton (delete and replace with your real test):

      1. Arrange — establish the preconditions the SOT row depends on
         (engine configured, policy state, backend pool composition).
         Skip-with-reason when a precondition isn't met; don't crash.
      2. Act — issue the API call / publish the envelope / drive the
         load that the acceptance criterion is written around.
      3. Wait — most live-stack assertions can't be inline; use a
         deadline + poll loop that reports last-seen state in its
         failure message (see `_wait_for_envelope` in test_t23_control_loop.py).
      4. Assert — the SOT criterion, verbatim, with the assertion
         message pointing back at the SOT row by section number.
    """
    pytest.skip(
        f"acceptance test for {task_context['task_id']!r} "
        f"(SOT {task_context['sot_section']}) not yet implemented — "
        f"described as: {task_context['describes']!r}. "
        f"Fill in the arrange/act/wait/assert block in "
        f"tests/integration/<filename>.py and remove this skip."
    )


# ── slow variant (delete if not needed) ──────────────────────────────────────

@pytest.mark.slow
def test_acceptance_slow_path(stack_ready, task_context):
    """For tasks whose acceptance needs >60 s wall-time (forecast cycles,
    autoscaler cooldown windows, model-calibrated detectors). CI's
    compose-test job selects `-m \"not slow\"` so this only runs locally
    via `pytest -m slow` or against an appropriately configured stack.

    Delete this function if your acceptance fits in the standard
    integration window."""
    deadline = time.monotonic() + 120.0
    while time.monotonic() < deadline:
        # TODO replace with the real poll loop
        time.sleep(5.0)
        break

    pytest.skip(
        f"slow acceptance test for {task_context['task_id']!r} "
        f"not yet implemented — see template's arrange/act/wait/assert "
        f"comment for the pattern."
    )
