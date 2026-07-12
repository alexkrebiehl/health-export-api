from typing import Any

import httpx


class HealthExportClient:
    def __init__(
        self, base_url: str, api_token: str, http_client: httpx.Client | None = None
    ) -> None:
        if not api_token:
            raise ValueError("api_token must not be empty")
        self._base_url = base_url.rstrip("/")
        self._http_client = http_client or httpx.Client(timeout=30.0)
        self._headers = {"Authorization": f"Bearer {api_token}"}

    def get_latest_export(self) -> dict[str, Any]:
        response = self._http_client.get(
            f"{self._base_url}/v1/exports/latest", headers=self._headers
        )
        response.raise_for_status()
        return response.json()

    def list_exports(self, limit: int = 20) -> list[dict[str, Any]]:
        response = self._http_client.get(
            f"{self._base_url}/v1/exports",
            headers=self._headers,
            params={"limit": limit},
        )
        response.raise_for_status()
        return response.json()["exports"]
