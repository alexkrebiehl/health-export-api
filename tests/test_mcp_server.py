import asyncio
from typing import Any

from health_export_api.mcp_server import create_mcp_server


class FakeHealthExportClient:
    def get_latest_export(self) -> dict[str, Any]:
        return {"id": "latest"}

    def list_metrics(self) -> list[dict[str, str | None]]:
        return [{"metric": "step_count", "unit": "count"}]

    def get_metric_summary(self, **_: Any) -> dict[str, Any]:
        return {"metric": "step_count", "series": []}

    def list_exports(self, limit: int) -> list[dict[str, Any]]:
        return [{"id": str(limit)}]


def test_mcp_server_exposes_export_query_tools() -> None:
    server = create_mcp_server(FakeHealthExportClient())

    tools = asyncio.run(server.list_tools())

    assert {tool.name for tool in tools} == {
        "get_latest_export",
        "list_exports",
        "list_metrics",
        "get_metric_summary",
    }
