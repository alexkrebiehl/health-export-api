import os
from typing import Any, Protocol

from mcp.server.fastmcp import FastMCP

from health_export_api.client import HealthExportClient


class ExportQueryClient(Protocol):
    def get_latest_export(self) -> dict[str, Any]: ...

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

    def list_exports(self, limit: int) -> list[dict[str, Any]]: ...


def create_mcp_server(client: ExportQueryClient) -> FastMCP:
    server = FastMCP("Health Export API")

    @server.tool()
    def list_metrics() -> list[dict[str, str | None]]:
        """List all metric names and units available across stored exports."""
        return client.list_metrics()

    @server.tool()
    def get_metric_summary(
        metric: str,
        granularity: str = "day",
        date_range: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any]:
        """Summarize a Health metric across a flexible date range.

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

    @server.tool()
    def get_latest_export() -> dict[str, Any]:
        """Return the most recently received Apple Health export."""
        return client.get_latest_export()

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
