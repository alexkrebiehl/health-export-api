# MCP server

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

## MCP tools

| Tool | Description |
|---|---|
| `list_metrics` | List all health metric names and units available across stored exports. |
| `get_metric_summary` | Aggregate a health metric over a date range (`metric`, `granularity`, `date_range` or `start_date`/`end_date`). |
| `list_workout_types` | List distinct Apple Health workout types with session counts (`include_hevy`). |
| `get_workout_summary` | Aggregate workout sessions (`workout_type`, `granularity`, `date_range`, `include_hevy`). |
| `get_workout_route` | GPS route points for one workout (`workout_id`, `max_points`). |
| `get_route_coverage_geojson` | GeoJSON coverage map of all routes in a box (`lat`, `lon`, `width`, `height` in metres, `date_range`, `workout_type`, `max_vertices`, `tolerance_m`). |
| `list_exports` | List raw stored export records, newest first (`limit` 1–100). |

---

[← Documentation index](../README.md#documentation)
