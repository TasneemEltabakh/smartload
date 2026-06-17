-- Real autoscaler scale events captured from a live docker-compose adaptive-bench
-- run on 2026-06-17 (batch 20260617T050342Z, run-01). The all-in-one container
-- can't run the autoscaler (no Docker inside it), so we replay these REAL events
-- into its TimescaleDB so the Scaling/Forecast dashboards show genuine measured
-- adaptive behaviour (pool drained 5->4->3 at idle, then scaled 3->4->5 under the
-- forecast burst). Re-applied on every launch by run-live.sh.
--
-- Timestamps are shifted so the newest event lands ~20 min before now() — recent
-- enough to appear in a "last 1h" view, but old enough that each event's
-- "5-minutes-after" forecast-accuracy window predates live traffic (so no
-- spurious predicted-vs-actual comparison against the idle demo load).
--
-- Idempotent: the all-in-one autoscaler never writes scaling_events itself, so
-- clearing the table first keeps re-runs clean.

DELETE FROM scaling_events;

INSERT INTO scaling_events (time, action, instance_count, reason)
SELECT (now() - interval '20 minutes') + (off_secs || ' seconds')::interval,
       action, instance_count, reason
FROM (VALUES
  (0::int,   'scale_in',  4, 'forecast demand 0 rps needs 1 backends (have 5); scaling in -1 [stop]'),
  (30,       'scale_in',  3, 'forecast demand 3 rps needs 1 backends (have 4); scaling in -1 [stop]'),
  (115,      'scale_out', 4, 'forecast demand 288 rps needs 4 backends (have 3); scaling out +1 [start]'),
  (130,      'scale_out', 5, 'forecast demand 431 rps needs 5 backends (have 4); scaling out +1 [start]')
) AS ev(off_secs, action, instance_count, reason);
