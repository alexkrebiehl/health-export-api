# JSON API

Everything under `/v1/…` that returns JSON. The HTML endpoints under
`/v1/render/…` are documented separately in [rendering.md](rendering.md).

All endpoints require:

```http
Authorization: Bearer <TOKEN>
```

## Ingestion

| Method | Path | Description |
|---|---|---|
| `GET` | `/healthz` | Unauthenticated health probe: `{"status":"ok"}`. |
| `POST` | `/v1/exports` | Persist any valid JSON body (Health Metrics or Workouts payload). Returns `{"id": "...", "received_at": "..."}`. |
| `GET` | `/v1/exports?limit=20` | List stored export records, newest first. `limit` is 1–100. |

A stored record has this envelope:

```json
{
  "id": "server-generated-id",
  "received_at": "2026-07-12T13:44:58.078184Z",
  "payload": { "the_original_auto_export_json": "is_preserved" }
}
```

## Health metrics

| Method | Path | Description |
|---|---|---|
| `GET` | `/v1/health/metrics` | List all metric names and units available across stored exports. |
| `GET` | `/v1/health/summary` | Aggregate a metric over a date range. |
| `GET` | `/v1/render/chart` | A metric's daily series with a rolling trend line, rendered for embedding. |
| `GET` | `/v1/render/stat` | A single stat tile — latest reading, or week-over-week change. |

**`GET /v1/health/summary` parameters:**

| Parameter | Required | Description |
|---|---|---|
| `metric` | yes | Metric name, e.g. `weight_body_mass`, `step_count`. See `/v1/health/metrics`. |
| `granularity` | no | `day` (default) or `month`. |
| `date_range` | no* | Natural expression: `"last 7 days"`, `"last 30 days"`, `"June 30 through July 4"`, `"2026-06-01 through 2026-06-30"`. |
| `start_date` | no* | ISO-8601 date. Must be paired with `end_date`. |
| `end_date` | no* | ISO-8601 date. Must be paired with `start_date`. |

*One of `date_range` or `start_date`/`end_date` is required.

Each series entry:

```json
{ "period": "2026-07", "sample_count": 12, "value": 201.9 }
```

Sum metrics (steps, distance, energy, etc.) return the **total** for the period. All other metrics return the **average**.

### Available sleep metrics

Sleep records from Apple Watch are parsed into seven separate queryable metrics:

| Metric | Unit | Aggregation | Description |
|---|---|---|---|
| `sleep_analysis` | hr | average | Main night's total sleep — longest sleep session per wake date. |
| `sleep_analysis_deep` | hr | average | Deep sleep during main night. |
| `sleep_analysis_core` | hr | average | Core sleep during main night. |
| `sleep_analysis_rem` | hr | average | REM sleep during main night. |
| `sleep_analysis_awake` | hr | average | Awake time during main night. |
| `sleep_analysis_nap` | hr | average | Nap duration (sessions starting noon–8 PM that end the same day). |
| `sleep_analysis_nap_count` | count | sum | Number of naps. |

**Classification rules** (applied to `sleepStart` local time, using the timezone offset embedded in the export):

- `sleepStart` in `[12:00, 20:00)` **and ends same calendar day** → **nap**
- `sleepStart` in `[12:00, 20:00)` **and crosses midnight** → **discarded** (Apple Watch artifact: a merged record spanning a daytime nap + the following overnight period)
- Everything else (including morning sleep-ins before noon) → **main sleep**

Cross-file deduplication: when multiple exports contain overlapping stage-transition sub-records of the same session (sharing the same `sleepEnd`), only the record with the maximum `totalSleep` is kept.

## Workouts

Apple Health workout sessions (Outdoor Walk, Outdoor Cycling, Paddle Sports, etc.). **Traditional Strength Training** — written to HealthKit by Hevy — is excluded by default to prevent double-counting with Hevy MCP data.

| Method | Path | Description |
|---|---|---|
| `GET` | `/v1/workouts/types` | List distinct workout types with session counts. |
| `GET` | `/v1/workouts/summary` | Aggregate workout sessions over a date range. |
| `GET` | `/v1/workouts/{workout_id}/route` | GPS route points for a single workout. |
| `GET` | `/v1/workouts/routes/geojson` | GeoJSON coverage map of every route inside a geographic box. |
| `GET` | `/v1/render/map` | The same coverage rendered as a Leaflet page, for embedding in a dashboard. |
| `GET` | `/v1/embed-token` | The derived read-only token that unlocks the [rendered pages](rendering.md). |

**`GET /v1/workouts/types` parameters:**

| Parameter | Default | Description |
|---|---|---|
| `include_hevy` | `false` | Include `Traditional Strength Training` sessions written by Hevy. |

**`GET /v1/workouts/summary` parameters:**

| Parameter | Required | Description |
|---|---|---|
| `workout_type` | no | Filter to a specific type, e.g. `Outdoor Walk`. Omit for all types. |
| `granularity` | no | `day` (default) or `month`. |
| `date_range` | no* | Same syntax as health summary. |
| `start_date` / `end_date` | no* | ISO-8601 alternative to `date_range`. |
| `include_hevy` | no | Default `false`. Set `true` to include Hevy sessions. |

Each series entry:

```json
{
  "period": "2026-07",
  "sessions": 35,
  "total_duration_min": 788.5,
  "total_distance_mi": 39.2,
  "total_active_energy_kcal": 0.0,
  "avg_heart_rate": 104.8
}
```

**`GET /v1/workouts/{workout_id}/route` parameters:**

| Parameter | Required | Description |
|---|---|---|
| `max_points` | no | Cap on route points returned, 1–10000. Omit for the whole route. |

Returns workout metadata plus a `route_points` array of `{index, timestamp, latitude, longitude, altitude, horizontal_accuracy, vertical_accuracy, speed, speed_accuracy, course, course_accuracy}`. Workouts recorded without GPS return `"has_route": false` and an empty array.

### Route coverage map

`GET /v1/workouts/routes/geojson` merges *all* matching routes inside a box into a single set of paths — a "which streets have I covered?" view rather than one line per session. Paths closer together than `tolerance_m` collapse into one, so the two sides of a street, or a route walked in both directions, count once and carry a traversal `count`.

| Parameter | Required | Description |
|---|---|---|
| `lat` / `lon` | yes | Centre of the box. |
| `width` / `height` | yes | Box size **in metres**, e.g. `2000` for a 2 km span. |
| `date_range` | no | Same syntax as health summary. Omit for all time. |
| `start_date` / `end_date` | no | ISO-8601 alternative to `date_range`. |
| `workout_type` | no | Repeatable, e.g. `?workout_type=Outdoor+Walk&workout_type=Outdoor+Run`. Omit for all types. |
| `max_vertices` | no | Ceiling on coordinates returned, 100–200000. Default `50000`. |
| `tolerance_m` | no | Merge distance in metres, 1–1000. Default `15` — roughly a street width. |
| `min_count` | no | Drop paths used fewer than this many times, 1–1000. Default `1` (keep everything). |

The result is a GeoJSON `FeatureCollection` that can be dropped straight into a map renderer:

```json
{
  "type": "FeatureCollection",
  "bbox": [13.385237, 52.511017, 13.414763, 52.528983],
  "features": [
    {
      "type": "Feature",
      "geometry": {"type": "LineString", "coordinates": [[13.399941, 52.519943], [13.400163, 52.519943]]},
      "properties": {
        "count": 7,
        "workout_types": ["Outdoor Walk"],
        "first_seen": "2025-01-04",
        "last_seen": "2026-07-30"
      }
    }
  ],
  "properties": {
    "tolerance_m": 15.0,
    "min_count": 1,
    "vertex_count": 12043,
    "feature_count": 812,
    "workout_count": 96,
    "start_date": null,
    "end_date": null,
    "workout_types": null
  }
}
```

Notes:

- `count` is how many *sessions* used that path, so it can be used to weight or colour lines by frequency. Pacing back and forth within one session still counts once.
- Snapping a heavily-used street does not yield one clean line. GPS scatter spreads each pass across neighbouring cells and one-off detours cross-link them, so a real neighbourhood comes back as a braid with far more junctions than the street map has. `min_count` is the lever for this: raising it to `2` or `3` drops single-pass edges and roughly halves the result, leaving the routes actually travelled regularly. Use `count` to style what remains.
- Timeframe filtering is on the workout's **start date**, not on individual point timestamps.
- If the result would exceed `max_vertices`, `tolerance_m` is raised automatically until it fits; the value actually used is reported in the top-level `properties`. A box that is large *and* densely covered therefore comes back at a coarser resolution than requested.
- A box spanning a pole or the ±180° meridian is clamped, not wrapped.

---

[← Documentation index](../README.md#documentation)
