import os
from typing import Any, Protocol

from mcp.server.fastmcp import FastMCP

from health_export_api.client import HealthExportClient


class ExportQueryClient(Protocol):
    def get_latest_export(self) -> dict[str, Any]: ...

    def list_exports(self, limit: int) -> list[dict[str, Any]]: ...


def create_mcp_server(client: ExportQueryClient) -> FastMCP:
    server = FastMCP("Health Export API")

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
