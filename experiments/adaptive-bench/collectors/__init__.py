"""Async collectors used by the adaptive-bench Round 2 orchestrator.

Each collector is a single coroutine that takes an `asyncio.Event` stop
signal + an output path, runs until the event fires, and flushes its
buffer before returning. The orchestrator runs all three concurrently
under `asyncio.gather` so the artefact streams are synchronous to
within their own poll cadence.
"""
