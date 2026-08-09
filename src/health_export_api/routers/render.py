"""HTML endpoints: the visualisations, under ``/v1/render``.

These are a presentation layer over the JSON the data endpoints serve. The
module imports no storage code — everything it needs comes through
:class:`~health_export_api.provider.DataProvider`, whose returned dicts are the
same bodies ``/v1/health/summary`` and ``/v1/workouts/routes/geojson`` return.

That is the point of the separation: if the rendering ever moves to its own
service, only the provider is replaced — by an HTTP client returning the same
shapes — and nothing in this file changes. A test asserts the boundary holds.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Callable

from fastapi import APIRouter, Header, Query
from fastapi.responses import HTMLResponse

from health_export_api.chart_page import (
    latest_reading,
    parse_series,
    render_chart_page,
    window_change,
)
from health_export_api.map_page import render_map_page
from health_export_api.provider import DataProvider
from health_export_api.routers import domain_errors
from health_export_api.stat_page import (
    MAX_MARGIN,
    render_change_tile,
    render_latest_tile,
)

AuthorizeEmbed = Callable[[str | None, str | None], None]


def build_render_router(
    *, provider: DataProvider, authorize_embed: AuthorizeEmbed
) -> APIRouter:
    router = APIRouter(prefix="/v1/render")

    @router.get("/map", response_class=HTMLResponse)
    def render_map(
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
        interactive: bool = Query(default=False),
        weight: float | None = Query(default=None, gt=0, le=20),
        embed_token: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ) -> HTMLResponse:
        """Rendered coverage map, for embedding in a Home Assistant iframe."""
        authorize_embed(authorization, embed_token)
        with domain_errors():
            range_start, range_end = provider.resolve_range(
                date_range=date_range, start_date=start_date, end_date=end_date
            )
            collection = provider.coverage(
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
        return HTMLResponse(
            render_map_page(
                collection,
                refresh_minutes=refresh_minutes,
                zoom_control=zoom_control,
                attribution=attribution,
                interactive=interactive,
                weight=weight,
            )
        )

    @router.get("/chart", response_class=HTMLResponse)
    def render_chart(
        metric: str,
        date_range: str | None = Query(default=None),
        start_date: str | None = Query(default=None),
        end_date: str | None = Query(default=None),
        window: int = Query(default=7, ge=0, le=365),
        title: str | None = Query(default=None),
        refresh_minutes: int = Query(default=30, ge=1, le=1440),
        embed_token: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ) -> HTMLResponse:
        """A metric's daily series with a rolling trend line, for embedding."""
        authorize_embed(authorization, embed_token)
        with domain_errors():
            # Unlike the summary endpoints the timeframe is optional here, so
            # the card URL can stay short; three months is the useful default.
            range_start, range_end = provider.resolve_range(
                date_range=date_range,
                start_date=start_date,
                end_date=end_date,
                default_range="last 90 days",
            )
            summary = provider.metric_summary(
                metric=metric, start_date=range_start, end_date=range_end
            )
        return HTMLResponse(
            render_chart_page(
                summary,
                title=title or metric.replace("_", " ").title(),
                window=window,
                refresh_minutes=refresh_minutes,
            )
        )

    @router.get("/stat", response_class=HTMLResponse)
    def render_stat(
        metric: str,
        stat: str = Query(default="latest", pattern="^(latest|change)$"),
        window: int = Query(default=7, ge=1, le=365),
        label: str | None = Query(default=None),
        good_direction: str = Query(default="none", pattern="^(up|down|none)$"),
        margin: float = Query(default=0.0, ge=0, le=MAX_MARGIN),
        align: str = Query(default="left", pattern="^(left|center|right)$"),
        refresh_minutes: int = Query(default=30, ge=1, le=1440),
        embed_token: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ) -> HTMLResponse:
        """A single stat tile for a metric, for embedding beside the chart."""
        authorize_embed(authorization, embed_token)
        today = provider.today

        # Two adjacent windows of `window` days each, so the range covers both.
        span_start = today - timedelta(days=2 * window - 1)
        summary = provider.metric_summary(
            metric=metric, start_date=span_start, end_date=today
        )

        points = parse_series(summary.get("series") or [])
        unit = summary.get("unit") or ""

        if stat == "change":
            return HTMLResponse(
                render_change_tile(
                    window_change(points, window, today),
                    unit=unit,
                    label=label or "Weekly trend",
                    window_days=window,
                    good_direction=good_direction,  # type: ignore[arg-type]
                    refresh_minutes=refresh_minutes,
                    margin=margin,
                    align=align,  # type: ignore[arg-type]
                )
            )
        return HTMLResponse(
            render_latest_tile(
                latest_reading(points),
                unit=unit,
                label=label or "Current",
                refresh_minutes=refresh_minutes,
                today=today,
                margin=margin,
                align=align,  # type: ignore[arg-type]
            )
        )

    return router
