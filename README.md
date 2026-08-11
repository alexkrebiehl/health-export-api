# Health Export API

A container-ready, authenticated receiver for JSON exported by **Health Auto Export**, with query endpoints over the result, server-rendered dashboard cards, and a stdio [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) interface.

The service deliberately preserves the Health Auto Export JSON unchanged on ingestion. The export schema varies by selected HealthKit metrics and exporter version; keeping the raw payload makes ingestion reliable, permits later normalization without data loss, and means the stored files can always rebuild the database.

## Components

| Component | Purpose |
|---|---|
| FastAPI service | Receives and persists authenticated `POST` requests. |
| File storage | One JSON record per received export in `/data/exports`; mount as persistent storage. |
| SQLite store | Normalized metric samples, workouts and GPS routes, rebuilt from the files on demand. |
| Render layer | Self-contained HTML pages — map, chart, stat tile — for embedding in a dashboard. |
| MCP server | Exposes query tools over stdio. |
| Container/Kubernetes assets | `Dockerfile`, `compose.yaml`, and `k8s/health-export-api.yaml`. |

## Documentation

| Document | Covers |
|---|---|
| [docs/api.md](docs/api.md) | The JSON API: ingestion, health metrics, sleep, workouts, route GeoJSON. |
| [docs/rendering.md](docs/rendering.md) | The `/v1/render/…` pages, their shared options, and the design rules behind them. |
| [docs/health-auto-export.md](docs/health-auto-export.md) | Setting up the exporter, how its two payload shapes differ, and what to check when data is missing. |
| [docs/mcp.md](docs/mcp.md) | The MCP server and its tools. |
| [docs/deployment.md](docs/deployment.md) | Docker Compose, Kubernetes, and development. |

## The two halves of the surface

All `/v1` endpoints require:

```http
Authorization: Bearer <TOKEN>
```

| Prefix | Serves | Auth |
|---|---|---|
| `/v1/…` | JSON from storage | bearer token |
| `/v1/render/…` | HTML pages built from that JSON | bearer token **or** `embed_token` |

The render layer is a presentation tier over the data endpoints, not a peer of them: it reaches storage only through a provider whose returned payloads are the *same bodies* the data endpoints serve — `/v1/render/map` embeds exactly what `/v1/workouts/routes/geojson` returns, and `/v1/render/chart` renders exactly what `/v1/health/summary` returns. The render modules import no storage code at all, and a test enforces that.

They share a process because the render tier is pure templating with no I/O; splitting it out would add a second credential, a second cache, and an extra transfer of the 1.4 MB coverage payload for no benefit at this size. If that changes — a second consumer, independent scaling, or heavy render dependencies — the provider is the only thing that would need replacing, with an HTTP client returning the same shapes.

## Quick start

```bash
uv sync --dev
uv run pytest

HEALTH_EXPORT_API_TOKEN=$(openssl rand -hex 32) \
  uv run uvicorn --factory health_export_api.app:create_app_from_env --port 8000
```

Then point a Health Auto Export automation at `POST /v1/exports` — see [docs/health-auto-export.md](docs/health-auto-export.md).
