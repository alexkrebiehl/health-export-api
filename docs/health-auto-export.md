# Health Auto Export setup

Two separate automations are required — Health Auto Export allows one data type per automation, and **Health Metrics and Workouts are independent**. A manual sync of one does not touch the other, which is a common source of "why is my walk missing".

**Automation 1 — Health Metrics:**

```text
Method:       POST
URL:          https://health-export.<your-domain>/v1/exports
Content-Type: application/json
Header:       Authorization: Bearer <token>
Data Type:    Health Metrics
Metrics:      weight_body_mass, body_fat_percentage, step_count,
              walking_running_distance, resting_heart_rate,
              heart_rate_variability, active_energy, basal_energy_burned,
              dietary_energy, apple_exercise_time, vo2_max, sleep_analysis,
              apple_sleeping_wrist_temperature
              (add others as desired)
Schedule:     Daily (e.g. 11:00 PM America/New_York)
```

**Automation 2 — Workouts:**

```text
Method:       POST
URL:          https://health-export.<your-domain>/v1/exports
Data Type:    Workouts
Schedule:     Daily (same time or offset by a few minutes)
```

Both automations post to the same `/v1/exports` endpoint. Run **Export Now** on each after setup and verify via the [API](api.md) or [MCP](mcp.md) before scheduling.

## How the two export shapes differ

The service receives two quite different payload shapes from the same app, and the difference explains most surprises:

| | Scheduled push | Manual full export |
|---|---|---|
| Covers | a **delta** — the interval since the last push | one file **per day** across the chosen range |
| Sample timestamps | the sample's real time (`08:18:28`) | bucketed to the minute (`08:19:00`) |
| Values | as recorded | re-aggregated, so they differ slightly |

One day's step count came to 9,902 incrementally and 10,620 in the full export. Neither is wrong; they are the same day summed at different granularities.

## Re-exporting is safe

Ingestion treats a payload as the **authority for the span it covers** and replaces whatever is stored there, rather than trying to match individual rows. Re-running an export that overlaps data you already have does not double-count it, however the producer buckets or rounds — so a full export is a safe repair for any gap, as often as you like.

This matters because row-level matching cannot work here: the two shapes above disagree on timestamps *and* values, and even an identical re-send can differ in float noise below printed precision.

## When something is missing

Check the raw payloads before suspecting ingestion — the files on disk are unmodified, so they settle it either way:

```bash
# Did anything arrive at all today, and what shape was it?
kubectl -n health-export-api exec deploy/health-export-api -- /app/.venv/bin/python -c "
import json, pathlib, datetime, collections
d = pathlib.Path('/data/exports')
start = datetime.datetime.now().replace(hour=0, minute=0, second=0).timestamp()
shapes = collections.Counter()
for p in d.glob('*.json'):
    if p.stat().st_mtime < start: continue
    data = (json.loads(p.read_text()).get('payload') or {}).get('data') or {}
    shapes[tuple(sorted(k for k, v in data.items() if v))] += 1
print(shapes)
"
```

`{('metrics',): 141}` means 141 metric pushes and **no workout export at all** — the Workouts automation has not run or is not reaching this host.

**Backdated entries fall through the gaps.** The incremental query window only moves forward, and it is keyed on the *sample's* timestamp. A food-logging app that writes a meal backdated to `08:59` after the window has passed `09:03` will never be picked up by a later delta — the window starts after the timestamp. Observed lags between logging a meal and it arriving ranged from 1 minute to 9 hours for this reason; it is not latency, it is hit-or-miss.

A manual full export sweeps the range and recovers anything skipped. If it happens often, widening the automation's lookback (where the app allows it) fixes it at source; otherwise a periodic full export over the last few days is the reliable pattern, and costs nothing thanks to replace-by-window ingestion.

---

[← Documentation index](../README.md#documentation)
