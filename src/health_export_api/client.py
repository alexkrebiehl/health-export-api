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

    # -------------------------------------------------------------------------
    # Ingestion
    # -------------------------------------------------------------------------

    def list_exports(self, limit: int = 20) -> list[dict[str, Any]]:
        response = self._http_client.get(
            f"{self._base_url}/v1/exports",
            headers=self._headers,
            params={"limit": limit},
        )
        response.raise_for_status()
        return response.json()["exports"]

    # -------------------------------------------------------------------------
    # Workouts — /v1/workouts/
    # -------------------------------------------------------------------------

    def list_workout_types(self, *, include_hevy: bool = False) -> list[dict[str, Any]]:
        """Return distinct workout types seen across stored exports."""
        response = self._http_client.get(
            f"{self._base_url}/v1/workouts/types",
            headers=self._headers,
            params={"include_hevy": str(include_hevy).lower()},
        )
        response.raise_for_status()
        return response.json()["workout_types"]

    def get_workout_summary(
        self,
        *,
        granularity: str = "day",
        workout_type: str | None = None,
        date_range: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        include_hevy: bool = False,
    ) -> dict[str, Any]:
        """Aggregate workout sessions (walks, runs, cycling, etc.) over a date range."""
        params: dict[str, str] = {"granularity": granularity,
                                  "include_hevy": str(include_hevy).lower()}
        if workout_type:
            params["workout_type"] = workout_type
        if date_range:
            params["date_range"] = date_range
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date
        response = self._http_client.get(
            f"{self._base_url}/v1/workouts/summary",
            headers=self._headers,
            params=params,
        )
        response.raise_for_status()
        return response.json()

    # -------------------------------------------------------------------------
    # Health metrics — /v1/health/
    # -------------------------------------------------------------------------

    def list_metrics(self) -> list[dict[str, str | None]]:
        response = self._http_client.get(
            f"{self._base_url}/v1/health/metrics", headers=self._headers
        )
        response.raise_for_status()
        return response.json()["metrics"]

    def get_metric_summary(
        self,
        *,
        metric: str,
        granularity: str = "day",
        date_range: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, str] = {"metric": metric, "granularity": granularity}
        if date_range:
            params["date_range"] = date_range
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date
        response = self._http_client.get(
            f"{self._base_url}/v1/health/summary", headers=self._headers, params=params
        )
        response.raise_for_status()
        return response.json()
