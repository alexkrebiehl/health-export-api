import json
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import Body, FastAPI, Header, HTTPException, Query, status


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def create_app(storage_dir: Path, api_token: str) -> FastAPI:
    if not api_token:
        raise ValueError("api_token must not be empty")

    storage_dir.mkdir(parents=True, exist_ok=True)
    app = FastAPI(title="Health Export API", version="0.1.0")

    def authorize(authorization: str | None) -> None:
        if authorization != f"Bearer {api_token}":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing or invalid bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/exports", status_code=status.HTTP_201_CREATED)
    def create_export(
        payload: Annotated[Any, Body()], authorization: str | None = Header(default=None)
    ) -> dict[str, str]:
        authorize(authorization)
        export_id = secrets.token_urlsafe(18)
        received_at = _utc_now()
        record = {"id": export_id, "received_at": received_at, "payload": payload}
        destination = storage_dir / f"{export_id}.json"
        temporary = destination.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(record, separators=(",", ":")), encoding="utf-8")
        temporary.replace(destination)
        return {"id": export_id, "received_at": received_at}

    def load_exports() -> list[dict[str, Any]]:
        records = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in storage_dir.glob("*.json")
        ]
        return sorted(records, key=lambda record: record["received_at"], reverse=True)

    @app.get("/v1/exports")
    def list_exports(
        limit: int = Query(default=20, ge=1, le=100),
        authorization: str | None = Header(default=None),
    ) -> dict[str, list[dict[str, Any]]]:
        authorize(authorization)
        return {"exports": load_exports()[:limit]}

    @app.get("/v1/exports/latest")
    def get_latest_export(
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        authorize(authorization)
        records = load_exports()
        if not records:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No exports found")
        return records[0]

    return app


def create_app_from_env() -> FastAPI:
    api_token = os.environ.get("HEALTH_EXPORT_API_TOKEN")
    if not api_token:
        raise RuntimeError("HEALTH_EXPORT_API_TOKEN must be configured")
    return create_app(
        storage_dir=Path(os.environ.get("HEALTH_EXPORT_STORAGE_DIR", "/data/exports")),
        api_token=api_token,
    )
