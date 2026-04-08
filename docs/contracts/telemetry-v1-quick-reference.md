# SmartLoad Telemetry Schema v1 — Quick Reference

**Status:** ✅ FROZEN — No changes permitted  
**Valid from:** March 28, 2026  

---

## TL;DR — Three Key Rules

1. **Timestamp:** ISO 8601 UTC, millisecond precision → `2026-03-28T14:23:45.123Z`
2. **Naming:** `smartload.<domain>.<metric_name>` → `smartload.request.latency_ms`
3. **Server ID:** Three identifiers together → `service_name` + `instance_id` + `node_id` (UUID)

---

## Required Metrics (Every Record)

You MUST include all three:

```json
{
  "timestamp": "2026-03-28T14:23:45.123Z",
  "service_name": "api-backend",
  "instance_id": "i-0a1b2c3d",
  "node_id": "a0000000-0000-0000-0000-000000000001",
  "metrics": {
    "smartload.request.latency_ms": 87.5,      // [0, 10000]
    "smartload.request.count": 42,             // >= 0
    "smartload.error.rate": 0.024              // [0.0, 1.0]
  },
  "attributes": {
    "source": "real"
  }
}
```

Missing ANY of these three = **record rejected**.

---

## Optional Metrics (Recommended)

Add for better autoscaling and anomaly detection:

```json
{
  "metrics": {
    "smartload.backend.cpu_usage": 0.52,           // [0.0, 1.0]
    "smartload.backend.memory_usage": 0.68,        // [0.0, 1.0]
    "smartload.routing.decision_engine": "rl_driven"
  }
}
```

If absent: CPU/memory are imputed from node history; routing engine field ignored.

---

## Data Source Attribute

Tag every record with one of:

| Source | Use case |
|--------|----------|
| `real` | Production backend data (primary) |
| `synthetic` | Injected/test data |
| `azure`, `bitbrains`, `planetlab`, `borg`, `alibaba` | Historical datasets |
| `nab`, `yahoo_smd` | Labeled anomaly datasets |

**Rule:** ML modules filter by source — don't mix without deliberate validation.

---

## Timestamp Format — Common Mistakes

✅ **Correct:**
- `2026-03-28T14:23:45.123Z`
- `2026-03-28T00:00:00.000Z`

❌ **Wrong:**
- `2026-03-28T14:23:45Z` (missing milliseconds)
- `2026-03-28T14:23:45.123` (missing Z)
- `2026-03-28T14:23:45.123456Z` (too precise — nanoseconds)
- `2026-03-28T14:23:45.123+02:00` (non-UTC timezone)

**Fix:** Convert all timestamps to UTC and append `Z`. Always include `.sss` (3 digits).

---

## Server Identifiers — Examples

| Use Case | Service | Instance | Node ID | Full Path |
|----------|---------|----------|---------|-----------|
| AWS backend | `api-backend` | `i-0a1b2c3d` | `a000...0001` | `api-backend/i-0a1b2c3d/a000...0001` |
| Kubernetes pod | `db-worker` | `pod-xyz789` | `b111...1111` | `db-worker/pod-xyz789/b111...1111` |
| Docker container | `cache-layer` | `container-abc` | `c222...2222` | `cache-layer/container-abc/c222...2222` |

**Rule:** `node_id` must exist in `backend_nodes` table or record is rejected.

---

## Metric Ranges & Validation

| Metric | Type | Range | Out-of-range? |
|--------|------|-------|---------------|
| `smartload.request.latency_ms` | Float | [0, 10000] | Warn if > 10000; always accept |
| `smartload.request.count` | Integer | [0, ∞) | Reject if < 0 |
| `smartload.error.rate` | Float | [0.0, 1.0] | **Hard reject** if out of bounds |
| `smartload.backend.cpu_usage` | Float | [0.0, 1.0] | **Hard reject** if out of bounds |
| `smartload.backend.memory_usage` | Float | [0.0, 1.0] | **Hard reject** if out of bounds |

**Strict enforcement:** Any violation triggers rejection and logs to validation table.

---

## Metric Naming Pattern

```
smartload.<domain>.<metric_name>
         ^^^^^^^^   ^^^^^^^^^^^^
         (fixed)    (unit in suffix)
```

**Domain list:**
- `request` → request-level metrics (latency, count)
- `error` → error-related (rate, count)
- `backend` → node-level (cpu, memory, disk)
- `routing` → routing decisions (engine, score)
- `scaling` → autoscaling (action, trigger)
- `system` → platform-level (availability, queue_depth)

**Unit suffix:**
- `_ms` = milliseconds
- `_sec` = seconds
- `_pct` = percent
- `_count` = unitless count

**Examples:**
- ✅ `smartload.request.latency_ms`
- ✅ `smartload.backend.cpu_usage`
- ❌ `smartload.request.latency` (missing unit)
- ❌ `smartload.latency_ms` (missing domain)

---

## Null Handling

| Metric | Null allowed? | If null | Action |
|--------|---|---|---|
| `smartload.request.latency_ms` | ❌ No | Record rejected | Log to validation table |
| `smartload.request.count` | ❌ No | Record rejected | Log to validation table |
| `smartload.error.rate` | ❌ No | Record rejected | Log to validation table |
| `smartload.backend.cpu_usage` | ✅ Yes | Impute with 7-day median | Proceed |
| `smartload.backend.memory_usage` | ✅ Yes | Impute with 7-day median | Proceed |
| `smartload.routing.decision_engine` | ✅ Yes | Accept null | Proceed |

---

## Aggregation Tiers

SmartLoad automatically creates these:

| Granularity | Interval | Table | Retention | Query with |
|---|---|---|---|---|
| Raw | Sub-minute | `telemetry_metrics` | 7 days | Recent debugging, anomalies |
| 1-min | 1 minute | `telemetry_1min` | 30 days | Dashboard, alerting, forecasting |
| 1-hour | 1 hour | `telemetry_hourly` | 60 days | Trends, ML training |

**Rule:** Never write to aggregate tables — they're computed automatically from raw data.

---

## Integration Checklist

- [ ] Backend emits metrics via OTLP/gRPC or HTTP
- [ ] All three required metrics are present
- [ ] Timestamp is ISO 8601 UTC with milliseconds
- [ ] Metric names follow `smartload.<domain>.<metric>`
- [ ] `service_name`, `instance_id`, `node_id` are populated
- [ ] Metric values are within specified ranges
- [ ] `source` attribute is set (e.g., "real", "synthetic")
- [ ] Null handling follows specification (don't send nulls for required fields)
- [ ] Tested with sample records before production push

---

## Getting Help

- **Full specification:** Read `/docs/contracts/telemetry-v1.md`
- **Machine-readable schema:** See `/docs/contracts/telemetry-v1.schema.yaml`
- **Collector setup:** Check `/docs/collector-config.yaml`
- **Data contracts:** See `/docs/data-contracts.md` (ML requirements)
- **Question?** Ask the Data Engineering team — this is FROZEN, so clarification only.

---

## Schema Lock Commitment

This schema is **FROZEN for v1.0**. 

- No breaking changes
- New optional metrics may be added only in minor bumps (v1.1, v1.2)
- Any breaking change requires v2.0 and 30-day notice
- All downstream systems (dashboards, ML, alerts) depend on this specification

**Don't invent new metrics or fields.** If something is missing, request a formal v1.1 amendment.

---

Generated: 2026-03-28  
Status: ✅ READY FOR IMPLEMENTATION
