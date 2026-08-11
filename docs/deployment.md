# Running and deploying

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

> **Important:** the Compose endpoint is HTTP on the local machine only. Do not configure Health Auto Export to send to this address over the public internet. Use the Kubernetes/Ingress deployment with a real HTTPS hostname and valid TLS certificate for iPhone uploads — see [health-auto-export.md](health-auto-export.md).

## Kubernetes

`k8s/health-export-api.yaml` is a self-contained manifest — one-replica Deployment, Service, PVC, security context, probes and resource bounds — for running this service on any cluster.

Before applying:

1. Replace `ghcr.io/REPLACE_ME/health-export-api:latest` with your published image.
2. Create the token secret without committing its value:

   ```bash
   kubectl create secret generic health-export-api \
     --from-literal=api-token="$(openssl rand -hex 32)"
   ```

3. Set the PVC storage class and add your TLS Ingress or Gateway resource.
4. Apply:

   ```bash
   kubectl apply -f k8s/health-export-api.yaml
   ```

The Deployment runs as non-root UID/GID `10001` with a read-only root filesystem and all Linux capabilities dropped; only `/data` needs to be writable.

### Settings that are not obvious

These are in the manifest with comments, and are worth knowing before you change them:

| Setting | Why |
|---|---|
| `strategy: Recreate` | The volume is ReadWriteOnce and SQLite wants a single writer. A rolling update would start the new pod before the old one released the volume. |
| No CPU limit | Route-coverage rendering is a burst of pure-Python work. A 500m quota throttled 2,538 of 3,091 scheduling periods — 82% — and stalled back-to-back requests. Memory stays capped, because that limit protects the node; a CPU limit only slows this pod down. |
| `memory: 768Mi` | The archive is read in bulk during a rebuild. 256Mi is not enough. |
| `TZ` | The service resolves "today" from the container clock, so under UTC an evening reading looks like yesterday's from 20:00 Eastern onwards. |
| `fsGroupChangePolicy: OnRootMismatch` | Skips a recursive chown of a large export archive on every restart. |

### GitOps

The cluster this was built for does not use `kubectl apply`. A separate private repository holds the same resources — plus an Ingress and an ExternalSecret that pulls the API token from a cluster secret store — and [Flux](https://fluxcd.io/) reconciles them.

The image is built and pushed by [`.github/workflows`](../.github/workflows) on every push to `main`, tagged `sha-<short>`. Deploying is then a one-line change in the GitOps repository:

```yaml
image: ghcr.io/<owner>/health-export-api:sha-<short>
```

followed by `flux reconcile kustomization health-export-api --with-source`. The tag is pinned by hand rather than tracked as `latest`, so what is running is always identifiable from the manifest, and a rollback is the previous SHA.

## Development

```bash
uv sync --dev
uv run pytest

docker build --tag health-export-api:test .
```

The image supports a read-only root filesystem; only `/data` needs persistent writable storage.

---

[← Documentation index](../README.md#documentation)
