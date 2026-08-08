# Health Export API

A container-ready, authenticated receiver for JSON exported by **Health Auto Export**, plus a stdio [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) interface that lets Hermes query received exports.

The service deliberately preserves the Health Auto Export JSON unchanged on ingestion. The export schema can vary by selected HealthKit metrics and exporter version; keeping the raw payload makes ingestion reliable and permits later normalization without data loss.

## Components

| Component | Purpose |
|---|---|
| FastAPI service | Receives and persists authenticated `POST` requests. |
| File storage | One JSON record per received export in `/data/exports`; mount as persistent storage. |
| Normalization layer | Parses raw exports into structured metric samples and workout sessions for the query endpoints. |
| MCP server | Exposes query tools to Hermes through stdio. |
| Container/Kubernetes assets | `Dockerfile`, `compose.yaml`, and `k8s/health-export-api.yaml`. |

## API reference

All `/v1` endpoints require:

```http
Authorization: Bearer <TOKEN>
```

### Ingestion

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

### Health metrics

| Method | Path | Description |
|---|---|---|
| `GET` | `/v1/health/metrics` | List all metric names and units available across stored exports. |
| `GET` | `/v1/health/summary` | Aggregate a metric over a date range. |

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

#### Available sleep metrics

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

### Workouts

Apple Health workout sessions (Outdoor Walk, Outdoor Cycling, Paddle Sports, etc.). **Traditional Strength Training** — written to HealthKit by Hevy — is excluded by default to prevent double-counting with Hevy MCP data.

| Method | Path | Description |
|---|---|---|
| `GET` | `/v1/workouts/types` | List distinct workout types with session counts. |
| `GET` | `/v1/workouts/summary` | Aggregate workout sessions over a date range. |
| `GET` | `/v1/workouts/{workout_id}/route` | GPS route points for a single workout. |
| `GET` | `/v1/workouts/routes/geojson` | GeoJSON coverage map of every route inside a geographic box. |

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

#### Route coverage map

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

> ⚠️ **Hevy double-count warning:** Hevy writes completed workouts back to HealthKit as `Traditional Strength Training`. Apple Health metrics like `active_energy` and `apple_exercise_time` already include those sessions. Do not add Apple Health exercise totals to Hevy session totals — they overlap. Use the Hevy MCP tools for structured strength-session detail.

## Health Auto Export setup

Two separate automations are recommended — Health Auto Export only allows one data type per automation:

**Automation 1 — Health Metrics:**

```text
Method:       POST
URL:          https://health-export.<your-domain>/v1/exports
Content-Type: application/json
Header:       Authorization: Bearer <token>
Data Type:    Health Metrics
Metrics:      weight_body_mass, body_fat_percentage, step_count,
              walking_running_distance, resting_heart_rate,
              heart_rate_variability, active_energy, apple_exercise_time,
              vo2_max, sleep_analysis, apple_sleeping_wrist_temperature
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

Both automations share the same `/v1/exports` ingestion endpoint. Run **Export Now** on each after setup and verify via the API or MCP before scheduling.

## MCP server

The MCP server is a separate stdio process that Hermes starts locally; it calls the API over HTTP. This works during local development and continues to work after the API moves into Kubernetes.

After `uv sync --dev`, add to `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  health_export:
    command: /home/alex/projects/health-export-api/.venv/bin/python
    args: ["-m", "health_export_api.mcp_server"]
    env:
      HEALTH_EXPORT_API_URL: "https://health-export.<your-domain>"
      HEALTH_EXPORT_API_TOKEN: "YOUR_TOKEN"
    timeout: 30
    connect_timeout: 30
```

Hermes filters environment variables passed to stdio MCP servers, so the two variables must be explicitly supplied here. Restart Hermes after adding the server.

### MCP tools

| Tool | Description |
|---|---|
| `list_metrics` | List all health metric names and units available across stored exports. |
| `get_metric_summary` | Aggregate a health metric over a date range (`metric`, `granularity`, `date_range` or `start_date`/`end_date`). |
| `list_workout_types` | List distinct Apple Health workout types with session counts (`include_hevy`). |
| `get_workout_summary` | Aggregate workout sessions (`workout_type`, `granularity`, `date_range`, `include_hevy`). |
| `get_workout_route` | GPS route points for one workout (`workout_id`, `max_points`). |
| `get_route_coverage_geojson` | GeoJSON coverage map of all routes in a box (`lat`, `lon`, `width`, `height` in metres, `date_range`, `workout_type`, `max_vertices`, `tolerance_m`). |
| `list_exports` | List raw stored export records, newest first (`limit` 1–100). |

## Run locally with Docker Compose

1. Generate a token and configure `.env`:

   ```bash
   cd /home/alex/projects/health-export-api
   cp .env.example .env
   openssl rand -hex 32
   # Paste the output after HEALTH_EXPORT_API_TOKEN= in .env
   ```

2. Start the service:

   ```bash
   docker compose up --build -d
   curl http://127.0.0.1:8000/healthz
   ```

3. Test ingestion (substitute your token):

   ```bash
   curl -X POST http://127.0.0.1:8000/v1/exports \
     -H 'Authorization: Bearer ***' \
     -H 'Content-Type: application/json' \
     --data '{"data":{"metrics":[{"name":"step_count","units":"count","data":[{"date":"2026-07-12 08:00:00 -0400","qty":8432}]}]}}'
   ```

4. Query it back:

   ```bash
   curl "http://127.0.0.1:8000/v1/health/summary?metric=step_count&date_range=last+7+days&granularity=day" \
     -H 'Authorization: Bearer ***'
   ```

Compose runs as your host UID/GID (defaults `1000:1000`). Set `PUID` and `PGID` in `.env` if your host differs.

> **Important:** the Compose endpoint is HTTP on the local machine only. Do not configure Health Auto Export to send to this address over the public internet. Use the Kubernetes/Ingress deployment with a real HTTPS hostname and valid TLS certificate for iPhone uploads.

## Kubernetes deployment

`k8s/health-export-api.yaml` contains a one-replica Deployment, Service, persistent volume claim, security context, probes, and resource bounds.

Before applying:

1. Replace `ghcr.io/REPLACE_ME/health-export-api:latest` with your published image.
2. Create the token secret without committing its value:

   ```bash
   kubectl create secret generic health-export-api \
     --from-literal=api-token="$(openssl rand -hex 32)"
   ```

3. Review the PVC storage class and add your TLS Ingress or Gateway resource.
4. Apply:

   ```bash
   kubectl apply -f k8s/health-export-api.yaml
   ```

The Deployment uses non-root UID/GID `10001`, a read-only root filesystem, dropped Linux capabilities, and an `fsGroup` so the mounted PVC is writable.

## Development

```bash
uv sync --dev
uv run pytest

docker build --tag health-export-api:test .
```

The image supports a read-only root filesystem; only `/data` needs persistent writable storage.
