"""JSON endpoints: ingestion and read access to stored health data."""

from __future__ import annotations

import json
import logging
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Header, Query, Request, status

from health_export_api.provider import DataProvider
from health_export_api.routers import domain_errors
from health_export_api.store import Store

logger = logging.getLogger(__name__)

Authorize = Callable[[str | None], None]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def build_data_router(
    *,
    store: Store,
    provider: DataProvider,
    authorize: Authorize,
    storage_dir: Path,
) -> APIRouter:
    router = APIRouter()

    def _load_exports() -> list[dict[str, Any]]:
        # A truncated file is skipped rather than raised. An upload that dies
        # mid-write leaves one behind, and listing every *other* export is
        # still the useful answer — one bad file used to 500 the endpoint.
        records = []
        for path in storage_dir.glob("*.json"):
            try:
                records.append(json.loads(path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                logger.warning("skipping unreadable export %s", path.name)
        return sorted(records, key=lambda r: r.get("received_at") or "",
                      reverse=True)

    # -------------------------------------------------------------------------
    # Ingestion — shared by all export types (health metrics, workouts, etc.)
    # -------------------------------------------------------------------------

    @router.post("/v1/exports", status_code=status.HTTP_201_CREATED)
    async def create_export(
        request: Request, authorization: str | None = Header(default=None)
    ) -> dict[str, str]:
        authorize(authorization)
        export_id = secrets.token_urlsafe(18)
        received_at = _utc_now()
        destination = storage_dir / f"{export_id}.json"
        temporary = destination.with_suffix(".json.tmp")

        # Stream body directly to disk — never buffer the full payload in RAM.
        body_bytes = bytearray()
        prefix = (
            f'{{"id":{json.dumps(export_id)},'
            f'"received_at":{json.dumps(received_at)},'
            f'"payload":'
        ).encode()
        with temporary.open("wb") as fh:
            fh.write(prefix)
            async for chunk in request.stream():
                fh.write(chunk)
                body_bytes.extend(chunk)
            fh.write(b"}")
        temporary.replace(destination)

        # Parse the body we already have in memory and ingest into SQLite.
        try:
            payload = json.loads(body_bytes)
        except Exception:
            payload = None  # malformed JSON; file is saved, ingest skipped
        store.ingest(export_id, received_at, payload)

        return {"id": export_id, "received_at": received_at}

    @router.get("/v1/exports")
    def list_exports(
        limit: int = Query(default=20, ge=1, le=100),
        authorization: str | None = Header(default=None),
    ) -> dict[str, list[dict[str, Any]]]:
        authorize(authorization)
        return {"exports": _load_exports()[:limit]}

    # -------------------------------------------------------------------------
    # Health metrics — /v1/health/
    # -------------------------------------------------------------------------

    @router.get("/v1/health/metrics")
    def list_metrics(
        authorization: str | None = Header(default=None),
    ) -> dict[str, list[dict[str, str | None]]]:
        authorize(authorization)
        return {"metrics": store.available_metrics()}

    @router.get("/v1/health/summary")
    def get_summary(
        metric: str,
        date_range: str | None = Query(default=None),
        start_date: str | None = Query(default=None),
        end_date: str | None = Query(default=None),
        granularity: str = Query(default="day", pattern="^(day|month)$"),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        authorize(authorization)
        with domain_errors():
            range_start, range_end = provider.resolve_range(
                date_range=date_range, start_date=start_date, end_date=end_date
            )
            if range_start is None or range_end is None:
                raise ValueError("provide date_range or start_date/end_date")
            return store.summarize_metric(
                metric=metric,
                start_date=range_start,
                end_date=range_end,
                granularity=granularity,
            )

    # -------------------------------------------------------------------------
    # Workouts — /v1/workouts/
    # "Traditional Strength Training" is written by Hevy to HealthKit and is
    # excluded by default to avoid double-counting with Hevy MCP data.
    # -------------------------------------------------------------------------

    @router.get("/v1/workouts/types")
    def list_workout_types(
        include_hevy: bool = Query(default=False),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        authorize(authorization)
        return {"workout_types": store.available_workout_types(include_hevy=include_hevy)}

    @router.get("/v1/workouts/summary")
    def get_workout_summary(
        workout_type: str | None = Query(default=None),
        date_range: str | None = Query(default=None),
        start_date: str | None = Query(default=None),
        end_date: str | None = Query(default=None),
        granularity: str = Query(default="day", pattern="^(day|month)$"),
        include_hevy: bool = Query(default=False),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        authorize(authorization)
        with domain_errors():
            range_start, range_end = provider.resolve_range(
                date_range=date_range, start_date=start_date, end_date=end_date
            )
            if range_start is None or range_end is None:
                raise ValueError("provide date_range or start_date/end_date")
            return store.summarize_workouts(
                start_date=range_start,
                end_date=range_end,
                granularity=granularity,
                workout_type=workout_type,
                include_hevy=include_hevy,
            )

    # Declared ahead of /{workout_id}/route so the literal path wins the match.
    @router.get("/v1/workouts/routes/geojson")
    def get_route_coverage_geojson(
        lat: float = Query(default=..., ge=-90, le=90),
        lon: float = Query(default=..., ge=-180, le=180),
        width: float = Query(default=..., gt=0),
        height: float = Query(default=..., gt=0),
        date_range: str | None = Query(default=None),
        start_date: str | None = Query(default=None),
        end_date: str | None = Query(default=None),
        workout_type: list[str] | None = Query(default=None),
        max_vertices: int = Query(default=50_000, ge=100, le=200_000),
        tolerance_m: float = Query(default=15.0, ge=1, le=1000),
        min_count: int = Query(default=1, ge=1, le=1000),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        authorize(authorization)
        with domain_errors():
            range_start, range_end = provider.resolve_range(
                date_range=date_range, start_date=start_date, end_date=end_date
            )
            return provider.coverage(
                lat=lat,
                lon=lon,
                width=width,
                height=height,
                start_date=range_start,
                end_date=range_end,
                workout_type=workout_type,
                max_vertices=max_vertices,
                tolerance_m=tolerance_m,
                min_count=min_count,
            )

    @router.get("/v1/workouts/{workout_id}/route")
    def get_workout_route(
        workout_id: str,
        max_points: int | None = Query(default=None, ge=1, le=10000),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        authorize(authorization)
        return store.get_workout_route(workout_id, max_points=max_points)

    return router
