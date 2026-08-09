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

from datetime import date, timedelta
from typing import Callable

from fastapi import APIRouter, Header, Query
from fastapi.responses import HTMLResponse

from health_export_api.chart_page import (
    is_whole_unit,
    latest_reading,
    parse_series,
    render_chart_page,
    window_balance,
    window_change,
    zero_fill_today,
)
from health_export_api.map_page import render_map_page
from health_export_api.provider import DataProvider
from health_export_api.routers import domain_errors
from health_export_api.page_shell import PageOptions
from health_export_api.routers.options import PageDep, SpanDep, Timeframe
from health_export_api.stat_page import (
    render_balance_tile,
    render_change_tile,
    render_latest_tile,
)

AuthorizeEmbed = Callable[[str | None, str | None], None]


def _with_zero_today(summary: dict, today: date) -> list[dict]:
    """The summary's series with today added at zero when it is missing.

    The dict-shaped counterpart of :func:`zero_fill_today`, for the chart —
    which is handed summaries rather than parsed points. Same rule: a daily
    total with nothing logged yet today is zero so far.
    """
    series = list(summary.get("series") or [])
    stamp = today.isoformat()
    if any(row.get("period") == stamp for row in series):
        return series
    return [*series, {"period": stamp, "samples": 0, "value": 0.0}]


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
        workout_type: list[str] | None = Query(default=None),
        max_vertices: int = Query(default=50_000, ge=100, le=200_000),
        tolerance_m: float = Query(default=15.0, ge=1, le=1000),
        min_count: int = Query(default=1, ge=1, le=1000),
        zoom_control: bool = Query(default=False),
        attribution: bool = Query(default=True),
        interactive: bool = Query(default=False),
        weight: float | None = Query(default=None, gt=0, le=20),
        embed_token: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
        page: PageOptions = PageDep,
        span: Timeframe = SpanDep,
    ) -> HTMLResponse:
        """Rendered coverage map, for embedding in a Home Assistant iframe."""
        authorize_embed(authorization, embed_token)
        with domain_errors():
            range_start, range_end = provider.resolve_range(
                date_range=span.date_range, start_date=span.start_date,
                end_date=span.end_date,
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
                options=page,
                zoom_control=zoom_control,
                attribution=attribution,
                interactive=interactive,
                weight=weight,
            )
        )

    @router.get("/chart", response_class=HTMLResponse)
    def render_chart(
        metric: list[str] = Query(default=...),
        label: list[str] | None = Query(default=None),
        unit: list[str] | None = Query(default=None),
        stack: list[str] | None = Query(default=None),
        window: int = Query(default=7, ge=0, le=365),
        kind: str = Query(default="line", pattern="^(line|bar)$"),
        layout: str = Query(default="grouped", pattern="^(grouped|overlay)$"),
        legend: bool | None = Query(default=None),
        baseline: float | None = Query(default=None),
        embed_token: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
        page: PageOptions = PageDep,
        span: Timeframe = SpanDep,
    ) -> HTMLResponse:
        """Daily series with a rolling trend line, for embedding.

        ``metric`` is repeatable: each one becomes its own stacked panel with
        its own y-axis. Measures in different units cannot honestly share a
        y-scale, so they get a panel each rather than a second axis. ``label``
        and ``unit`` are repeatable alongside it, one per metric in order.

        ``kind=bar`` suits a discrete daily total — a step count has no value
        between Tuesday and Wednesday for a line to interpolate to. ``baseline``
        pins the y-axis floor; unset, it zooms to the data.

        ``stack`` is repeatable alongside ``metric`` and names the stack each
        one belongs to. Supplying it puts every metric in a single panel on one
        shared y-axis, with same-named metrics drawn as segments of one bar —
        so only group measures that share a unit. ``layout`` then arranges the
        stacks: ``grouped`` side by side, ``overlay`` bars then lines.
        """
        authorize_embed(authorization, embed_token)
        with domain_errors():
            # Unlike the summary endpoints the timeframe is optional here, so
            # the card URL can stay short; three months is the useful default.
            range_start, range_end = provider.resolve_range(
                date_range=span.date_range,
                start_date=span.start_date,
                end_date=span.end_date,
                default_range="last 90 days",
            )
            summaries = [
                provider.metric_summary(
                    metric=name, start_date=range_start, end_date=range_end
                )
                for name in metric
            ]
        # A daily total with nothing recorded today is zero so far, not a gap.
        # Only summed metrics: an averaged one has no reading, not a zero one.
        for summary in summaries:
            if summary.get("aggregation") == "sum":
                summary["series"] = _with_zero_today(summary, provider.today)

        names = [name.replace("_", " ").title() for name in metric]
        return HTMLResponse(
            render_chart_page(
                summaries,
                title=names[0],
                series_labels=list(label or []) or names,
                series_units=list(unit or []),
                series_stacks=list(stack or []),
                window=window,
                kind=kind,
                layout=layout,
                legend=legend,
                baseline=baseline,
                options=page,
            )
        )

    @router.get("/stat", response_class=HTMLResponse)
    def render_stat(
        metric: str,
        stat: str = Query(default="latest", pattern="^(latest|change|balance)$"),
        minus: list[str] | None = Query(default=None),
        window: int = Query(default=7, ge=1, le=365),
        label: str | None = Query(default=None),
        good_direction: str = Query(default="none", pattern="^(up|down|none)$"),
        unit: str | None = Query(default=None),
        align: str = Query(default="left", pattern="^(left|center|right)$"),
        embed_token: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
        page: PageOptions = PageDep,
    ) -> HTMLResponse:
        """A single stat tile for a metric, for embedding beside the chart.

        ``stat=balance`` subtracts one or more ``minus`` metrics from
        ``metric`` across the window — energy in against energy out. Only
        days with a ``metric`` reading count; see :func:`window_balance`.
        """
        authorize_embed(authorization, embed_token)
        today = provider.today

        # Two adjacent windows of `window` days each, so the range covers both.
        span_start = today - timedelta(days=2 * window - 1)
        summary = provider.metric_summary(
            metric=metric, start_date=span_start, end_date=today
        )

        points = parse_series(summary.get("series") or [])
        # A daily total with nothing recorded today is zero so far, not stale.
        # Averaged metrics keep their last reading — you do not weigh nothing
        # because you skipped the scale.
        summed = summary.get("aggregation") == "sum"
        if summed:
            points = zero_fill_today(points, today)
        # `unit=` (empty) suppresses it: step_count's stored unit is the
        # literal string "count", which reads as noise beside the number.
        label_unit = summary.get("unit") or "" if unit is None else unit
        # Read from the stored unit, so blanking the displayed one above still
        # formats a tally as a whole number.
        integral = is_whole_unit(summary.get("unit"))

        if stat == "balance":
            spend = [
                parse_series(
                    provider.metric_summary(
                        metric=name, start_date=span_start, end_date=today
                    ).get("series") or []
                )
                for name in (minus or [])
            ]
            return HTMLResponse(
                render_balance_tile(
                    window_balance(points, spend, window, today),
                    unit=label_unit,
                    label=label or f"{window}-day balance",
                    window_days=window,
                    align=align,  # type: ignore[arg-type]
                    integral=integral,
                    options=page,
                )
            )

        if stat == "change":
            return HTMLResponse(
                render_change_tile(
                    window_change(points, window, today),
                    unit=label_unit,
                    label=label or "Weekly trend",
                    window_days=window,
                    good_direction=good_direction,  # type: ignore[arg-type]
                    align=align,  # type: ignore[arg-type]
                    integral=integral,
                    options=page,
                )
            )
        return HTMLResponse(
            render_latest_tile(
                latest_reading(points),
                unit=label_unit,
                label=label or "Current",
                today=today,
                align=align,  # type: ignore[arg-type]
                integral=integral,
                options=page,
            )
        )

    return router
