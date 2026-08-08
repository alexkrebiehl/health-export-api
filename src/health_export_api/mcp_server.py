import os
from typing import Any, Protocol

from mcp.server.fastmcp import FastMCP

from health_export_api.client import HealthExportClient


class ExportQueryClient(Protocol):
    def list_exports(self, limit: int) -> list[dict[str, Any]]: ...

    # Health metrics
    def list_metrics(self) -> list[dict[str, str | None]]: ...

    def get_metric_summary(
        self,
        *,
        metric: str,
        granularity: str,
        date_range: str | None,
        start_date: str | None,
        end_date: str | None,
    ) -> dict[str, Any]: ...

    # Workouts
    def list_workout_types(self, *, include_hevy: bool) -> list[dict[str, Any]]: ...

    def get_workout_summary(
        self,
        *,
        granularity: str,
        workout_type: str | None,
        date_range: str | None,
        start_date: str | None,
        end_date: str | None,
        include_hevy: bool,
    ) -> dict[str, Any]: ...

    def get_workout_route(
        self, workout_id: str, *, max_points: int | None
    ) -> dict[str, Any]: ...


def create_mcp_server(client: ExportQueryClient) -> FastMCP:
    server = FastMCP("Health Export API")

    # -------------------------------------------------------------------------
    # Health metrics
    # -------------------------------------------------------------------------

    @server.tool()
    def list_metrics() -> list[dict[str, str | None]]:
        """List all health metric names and units available across stored exports."""
        return client.list_metrics()

    @server.tool()
    def get_metric_summary(
        metric: str,
        granularity: str = "day",
        date_range: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any]:
        """Summarize a health metric (step_count, weight_body_mass, resting_heart_rate, etc.)
        across a flexible date range.

        metric: metric name (e.g. "step_count", "weight_body_mass")
        granularity: "day" or "month"
        date_range: natural expression – "last 3 days", "last 7 days",
                    "June 30 through July 4", "2026-06-01 through 2026-06-30"
        start_date / end_date: ISO-8601 date strings (alternative to date_range)
        """
        return client.get_metric_summary(
            metric=metric,
            granularity=granularity,
            date_range=date_range,
            start_date=start_date,
            end_date=end_date,
        )

    # -------------------------------------------------------------------------
    # Workouts (Outdoor Walk, Running, Cycling, etc.)
    # "Traditional Strength Training" (written by Hevy) is excluded by default.
    # Use include_hevy=True only when you explicitly want to see Hevy sessions
    # from the Apple Health side — prefer the Hevy MCP tools for strength data.
    # -------------------------------------------------------------------------

    @server.tool()
    def list_workout_types(include_hevy: bool = False) -> list[dict[str, Any]]:
        """List distinct Apple Health workout types (Outdoor Walk, Running, etc.)
        with session counts. Excludes Hevy-sourced strength sessions by default.
        """
        return client.list_workout_types(include_hevy=include_hevy)

    @server.tool()
    def get_workout_summary(
        granularity: str = "day",
        workout_type: str | None = None,
        date_range: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        include_hevy: bool = False,
    ) -> dict[str, Any]:
        """Summarize Apple Health workout sessions (walks, runs, cycling, etc.)
        across a flexible date range.

        Each period returns: sessions count, total duration (min), total distance (mi),
        total active energy (kcal), and avg heart rate (bpm).

        workout_type: filter to a specific type, e.g. "Outdoor Walk". Omit for all types.
        granularity: "day" or "month"
        date_range: e.g. "last 7 days", "last 21 days", "June 30 through July 4"
        start_date / end_date: ISO-8601 alternative to date_range
        include_hevy: set True to include "Traditional Strength Training" sessions
                      written by Hevy — avoid double-counting with Hevy MCP data.
        """
        return client.get_workout_summary(
            granularity=granularity,
            workout_type=workout_type,
            date_range=date_range,
            start_date=start_date,
            end_date=end_date,
            include_hevy=include_hevy,
        )

    @server.tool()
    def get_workout_route(
        workout_id: str, max_points: int | None = None
    ) -> dict[str, Any]:
        """Get GPS route data for a specific workout.
        
        Returns workout metadata plus GPS coordinates, altitude, speed, and course
        for each point along the route. Useful for mapping, analyzing pace/elevation,
        or exporting to GPX.
        
        workout_id: HealthKit workout UUID (from workout summary or list_exports)
        max_points: optional limit on number of points returned (default: all points)
        """
        if max_points is not None and not 1 <= max_points <= 10000:
            raise ValueError("max_points must be between 1 and 10000")
        return client.get_workout_route(workout_id, max_points=max_points)

    @server.tool()
    def get_route_coverage_geojson(
        lat: float,
        lon: float,
        width: float,
        height: float,
        date_range: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        workout_type: list[str] | None = None,
        max_vertices: int = 2000,
        tolerance_m: float = 15.0,
        min_count: int = 1,
    ) -> dict[str, Any]:
        """Get a GeoJSON map of every route travelled inside a geographic box.

        Merges all matching workout routes into one set of paths, so a street
        walked many times is a single line carrying a "count" of how many
        sessions used it. Useful for street-coverage or completion maps.

        lat / lon: centre of the box
        width / height: box size in metres, e.g. 2000 for a 2km span
        date_range: e.g. "last 30 days", "June 30 through July 4". Omit for all time.
        start_date / end_date: ISO-8601 alternative to date_range
        workout_type: list of types to include, e.g. ["Outdoor Walk"]. Omit for all.
        max_vertices: ceiling on coordinates returned; the tolerance is coarsened
                      automatically to fit. Keep this small — GeoJSON is verbose.
        tolerance_m: paths closer together than this merge into one, e.g. the two
                     sides of a street. Default 15.
        min_count: drop paths used fewer than this many times. Default 1 (keep
                   everything). Raise it to cut one-off detours and GPS scatter
                   and leave only regular routes — 2 or 3 thins the map a lot.
        """
        if not 1 <= max_vertices <= 10000:
            raise ValueError("max_vertices must be between 1 and 10000")
        return client.get_route_coverage_geojson(
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

    # -------------------------------------------------------------------------
    # Raw exports
    # -------------------------------------------------------------------------

    @server.tool()
    def list_exports(limit: int = 20) -> list[dict[str, Any]]:
        """List received Apple Health exports, newest first (up to 100)."""
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        return client.list_exports(limit=limit)

    return server


def create_mcp_server_from_env() -> FastMCP:
    base_url = os.environ.get("HEALTH_EXPORT_API_URL")
    api_token = os.environ.get("HEALTH_EXPORT_API_TOKEN")
    if not base_url or not api_token:
        raise RuntimeError(
            "HEALTH_EXPORT_API_URL and HEALTH_EXPORT_API_TOKEN must be configured"
        )
    return create_mcp_server(HealthExportClient(base_url=base_url, api_token=api_token))


def main() -> None:
    create_mcp_server_from_env().run(transport="stdio")


if __name__ == "__main__":
    main()
