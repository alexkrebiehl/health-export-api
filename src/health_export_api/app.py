import json
import os
import secrets
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query, Request, status

from health_export_api.normalization import available_metrics, resolve_date_range, summarize_metric


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def create_app(
    storage_dir: Path, api_token: str, summary_today: date | None = None
) -> FastAPI:
    if not api_token:
        raise ValueError("api_token must not be empty")

    storage_dir.mkdir(parents=True, exist_ok=True)
    app = FastAPI(title="Health Export API", version="0.3.0")

    def authorize(authorization: str | None) -> None:
        if authorization != f"Bearer {api_token}":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing or invalid bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    # -------------------------------------------------------------------------
    # Health check
    # -------------------------------------------------------------------------

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # -------------------------------------------------------------------------
    # Ingestion — shared by all export types (health metrics, workouts, etc.)
    # -------------------------------------------------------------------------

    @app.post("/v1/exports", status_code=status.HTTP_201_CREATED)
    async def create_export(
        request: Request, authorization: str | None = Header(default=None)
    ) -> dict[str, str]:
        authorize(authorization)
        export_id = secrets.token_urlsafe(18)
        received_at = _utc_now()
        destination = storage_dir / f"{export_id}.json"
        temporary = destination.with_suffix(".json.tmp")
        # Stream body directly to disk — never buffer the full payload in RAM.
        with temporary.open("wb") as fh:
            prefix = (
                f'{{"id":{json.dumps(export_id)},'
                f'"received_at":{json.dumps(received_at)},'
                f'"payload":'
            ).encode()
            fh.write(prefix)
            async for chunk in request.stream():
                fh.write(chunk)
            fh.write(b"}")
        temporary.replace(destination)
        return {"id": export_id, "received_at": received_at}

    @app.get("/v1/exports")
    def list_exports(
        limit: int = Query(default=20, ge=1, le=100),
        authorization: str | None = Header(default=None),
    ) -> dict[str, list[dict[str, Any]]]:
        authorize(authorization)
        return {"exports": _load_exports()[:limit]}

    # -------------------------------------------------------------------------
    # Health metrics — /v1/health/
    # -------------------------------------------------------------------------

    @app.get("/v1/health/metrics")
    def list_metrics(
        authorization: str | None = Header(default=None),
    ) -> dict[str, list[dict[str, str | None]]]:
        authorize(authorization)
        return {"metrics": available_metrics(_load_exports())}

    @app.get("/v1/health/summary")
    def get_summary(
        metric: str,
        date_range: str | None = Query(default=None),
        start_date: str | None = Query(default=None),
        end_date: str | None = Query(default=None),
        granularity: str = Query(default="day", pattern="^(day|month)$"),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        authorize(authorization)
        try:
            range_start, range_end = resolve_date_range(
                date_range=date_range,
                start_date=start_date,
                end_date=end_date,
                today=summary_today or date.today(),
            )
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
            )
        return summarize_metric(
            _load_exports(),
            metric=metric,
            start_date=range_start,
            end_date=range_end,
            granularity=granularity,
        )

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    def _load_exports() -> list[dict[str, Any]]:
        records = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in storage_dir.glob("*.json")
        ]
        return sorted(records, key=lambda r: r["received_at"], reverse=True)

    return app


def create_app_from_env() -> FastAPI:
    api_token = os.environ.get("HEALTH_EXPORT_API_TOKEN")
    if not api_token:
        raise RuntimeError("HEALTH_EXPORT_API_TOKEN must be configured")
    return create_app(
        storage_dir=Path(os.environ.get("HEALTH_EXPORT_STORAGE_DIR", "/data/exports")),
        api_token=api_token,
    )
