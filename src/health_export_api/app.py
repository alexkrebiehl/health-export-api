import hashlib
import hmac
import json
import os
import secrets
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from health_export_api.map_page import render_map_page
from health_export_api.normalization import resolve_date_range
from health_export_api.store import Store

_STATIC_DIR = Path(__file__).parent / "static"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def derive_map_token(api_token: str) -> str:
    """A second, read-only token for the map page.

    The Home Assistant Webpage card cannot send an Authorization header, so the
    credential has to travel in the URL — where it ends up in the dashboard
    config and browser history. Putting the real token there would expose
    ingestion rights, so the map page gets its own, derived one:

    * nothing new to provision, and it cannot be reversed to the API token;
    * leaking it exposes the coverage map and nothing else;
    * rotating HEALTH_EXPORT_API_TOKEN rotates it too.
    """
    return hmac.new(api_token.encode(), b"route-map", hashlib.sha256).hexdigest()[:32]


def create_app(
    storage_dir: Path, api_token: str, summary_today: date | None = None
) -> FastAPI:
    if not api_token:
        raise ValueError("api_token must not be empty")

    storage_dir.mkdir(parents=True, exist_ok=True)
    db_path = storage_dir / "health_export.db"
    store = Store(db_path)
    store.backfill(storage_dir)

    app = FastAPI(title="Health Export API", version="0.5.0")

    # Stock Leaflet, served same-origin so the map page has no CDN dependency.
    # Unauthenticated: it is open-source library code, not user data.
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

    map_token = derive_map_token(api_token)

    def authorize(authorization: str | None) -> None:
        if authorization != f"Bearer {api_token}":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing or invalid bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    def authorize_map(authorization: str | None, supplied: str | None) -> None:
        """Accept either the full bearer token or the derived map token."""
        if supplied is not None and secrets.compare_digest(supplied, map_token):
            return
        authorize(authorization)

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
        return {"metrics": store.available_metrics()}

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

    @app.get("/v1/workouts/types")
    def list_workout_types(
        include_hevy: bool = Query(default=False),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        authorize(authorization)
        return {"workout_types": store.available_workout_types(include_hevy=include_hevy)}

    @app.get("/v1/workouts/summary")
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
        return store.summarize_workouts(
            start_date=range_start,
            end_date=range_end,
            granularity=granularity,
            workout_type=workout_type,
            include_hevy=include_hevy,
        )

    def _coverage(
        *,
        lat: float,
        lon: float,
        width: float,
        height: float,
        date_range: str | None,
        start_date: str | None,
        end_date: str | None,
        workout_type: list[str] | None,
        max_vertices: int,
        tolerance_m: float,
        min_count: int,
    ) -> dict[str, Any]:
        """Shared by the GeoJSON and map routes, which take the same filters."""
        # The timeframe is optional here, unlike the summary endpoints, so only
        # resolve a range when the caller actually asked for one.
        range_start = range_end = None
        if date_range or start_date or end_date:
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
        return store.route_coverage_geojson(
            lat=lat,
            lon=lon,
            width=width,
            height=height,
            start_date=range_start,
            end_date=range_end,
            workout_types=workout_type,
            max_vertices=max_vertices,
            tolerance_m=tolerance_m,
            min_count=min_count,
        )

    @app.get("/v1/map-token")
    def get_map_token(
        authorization: str | None = Header(default=None),
    ) -> dict[str, str]:
        """The derived map-page token. Requires the real bearer token."""
        authorize(authorization)
        return {"map_token": map_token}

    # Declared ahead of /{workout_id}/route so the literal path wins the match.
    @app.get("/v1/workouts/routes/geojson")
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
        return _coverage(
            lat=lat,
            lon=lon,
            width=width,
            height=height,
            date_range=date_range,
            start_date=start_date,
            end_date=end_date,
            workout_type=workout_type,
            max_vertices=max_vertices,
            tolerance_m=tolerance_m,
            min_count=min_count,
        )

    @app.get("/v1/workouts/routes/map", response_class=HTMLResponse)
    def get_route_coverage_map(
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
        refresh_minutes: int = Query(default=30, ge=1, le=1440),
        zoom_control: bool = Query(default=False),
        attribution: bool = Query(default=True),
        map_token: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ) -> HTMLResponse:
        """Rendered coverage map, for embedding in a Home Assistant iframe."""
        authorize_map(authorization, map_token)
        collection = _coverage(
            lat=lat,
            lon=lon,
            width=width,
            height=height,
            date_range=date_range,
            start_date=start_date,
            end_date=end_date,
            workout_type=workout_type,
            max_vertices=max_vertices,
            tolerance_m=tolerance_m,
            min_count=min_count,
        )
        return HTMLResponse(
            render_map_page(
                collection,
                refresh_minutes=refresh_minutes,
                zoom_control=zoom_control,
                attribution=attribution,
            )
        )

    @app.get("/v1/workouts/{workout_id}/route")
    def get_workout_route(
        workout_id: str,
        max_points: int | None = Query(default=None, ge=1, le=10000),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        authorize(authorization)
        return store.get_workout_route(workout_id, max_points=max_points)

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
