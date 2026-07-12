# Health Export API

A container-ready, authenticated receiver for JSON exported by **Health Auto Export**, plus a stdio [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) interface that lets Hermes query received exports.

The service deliberately preserves the Health Auto Export JSON unchanged. The app's export schema can vary by selected HealthKit metrics and exporter version; keeping the raw payload makes ingestion reliable and permits later normalization/trend analysis without data loss.

## What is included

| Component | Purpose |
|---|---|
| FastAPI service | Receives and persists authenticated `POST` requests. |
| File storage | One JSON record per received export in `/data/exports`; mount this as persistent storage. |
| MCP server | Exposes `get_latest_export` and `list_exports` to Hermes through stdio. |
| Container/Kubernetes assets | `Dockerfile`, `compose.yaml`, and `k8s/health-export-api.yaml`. |

## API contract

All `/v1` endpoints require:

```http
Authorization: Bearer <HEALTH_EXPORT_API_TOKEN>
```

| Method | Path | Result |
|---|---|---|
| `GET` | `/healthz` | Unauthenticated health probe: `{"status":"ok"}`. |
| `POST` | `/v1/exports` | Persist any valid JSON object or array; returns its ID and receive timestamp. |
| `GET` | `/v1/exports/latest` | Return the most recently received full export record. |
| `GET` | `/v1/exports?limit=20` | List stored full export records, newest first; `limit` is 1–100. |

A stored record has this envelope:

```json
{
  "id": "server-generated-id",
  "received_at": "2026-07-12T13:44:58.078184Z",
  "payload": { "the_original_auto_export_json": "is_preserved" }
}
```

## Run locally with Docker Compose

1. Generate a strong token locally. Do not send it in chat or commit it.

   ```bash
   cd /home/alex/services/health-export-api
   cp .env.example .env
   openssl rand -hex 32
   # Put the generated value after HEALTH_EXPORT_API_TOKEN= in .env
   ```

2. Start the API:

   ```bash
   docker compose up --build -d
   curl http://127.0.0.1:8000/healthz
   ```

3. Test an authenticated submission (substitute your local token):

   ```bash
   curl -X POST http://127.0.0.1:8000/v1/exports \
     -H 'Authorization: Bearer YOUR_TOKEN' \
     -H 'Content-Type: application/json' \
     --data '{"data":{"steps":8432,"weightKg":81.2}}'
   ```

Compose runs as your host UID/GID (defaults `1000:1000`) so its `./storage` bind mount remains writable without making the API process root. Set `PUID` and `PGID` in `.env` if your host differs.

> **Important:** the Compose endpoint is HTTP on the local machine only. Do not configure your iPhone to send health data over the public internet until the Kubernetes/Ingress step supplies a real HTTPS hostname and valid TLS certificate.

## Configure Health Auto Export after TLS deployment

In Health Auto Export, create an HTTP/REST export automation with:

```text
Method:       POST
URL:          https://health-export.<your-domain>/v1/exports
Content-Type: application/json
Header:       Authorization: Bearer <your generated token>
Payload:      JSON export / full export body
Schedule:     Daily (for example 10:30 PM America/New_York)
```

Start with daily summaries for weight, step count, walking/running distance, workouts, and—if desired—resting heart rate and HRV. Run **Export Now** once and inspect the latest export through the API or MCP before scheduling it.

## Hermes MCP configuration

The MCP server is intentionally separate from the HTTP container endpoint: Hermes starts it as a local stdio subprocess, and it calls the API over HTTP. This works now and continues to work when the API moves into Kubernetes.

After `uv sync --dev` in this project, add this to `~/.hermes/config.yaml` (use the eventual HTTPS API URL):

```yaml
mcp_servers:
  health_export:
    command: /home/alex/services/health-export-api/.venv/bin/python
    args: ["-m", "health_export_api.mcp_server"]
    env:
      HEALTH_EXPORT_API_URL: "https://health-export.<your-domain>"
      HEALTH_EXPORT_API_TOKEN: "YOUR_TOKEN"
    timeout: 30
    connect_timeout: 30
```

Hermes deliberately filters environment variables passed to stdio MCP servers, so the two variables must be explicitly supplied in the MCP configuration. Restart Hermes after adding the server. Its tools will be named:

```text
mcp_health_export_get_latest_export
mcp_health_export_list_exports
```

## Kubernetes handoff

`k8s/health-export-api.yaml` contains a one-replica Deployment, Service, persistent volume claim, security context, probes, and resource bounds. Before applying it:

1. Replace `ghcr.io/REPLACE_ME/health-export-api:latest` with the image you publish.
2. Create the referenced secret without putting its value in Git:

   ```bash
   kubectl create secret generic health-export-api \
     --from-literal=api-token="$(openssl rand -hex 32)"
   ```

3. Review the PVC storage class and add your chosen TLS Ingress/Gateway resource.
4. Apply the manifest:

   ```bash
   kubectl apply -f k8s/health-export-api.yaml
   ```

The Deployment uses non-root UID/GID `10001`, a read-only root filesystem, dropped Linux capabilities, and an `fsGroup` so the mounted PVC is writable. The manifest is intentionally not exposed publicly yet; add ingress only alongside TLS and appropriate network access controls.

## Development and verification

```bash
uv sync --dev
uv run pytest

docker build --tag health-export-api:test .
```

The image supports a read-only root filesystem; only `/data` needs persistent writable storage.
