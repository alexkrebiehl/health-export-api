import httpx

from health_export_api.client import HealthExportClient


def test_client_calls_list_exports_endpoint_with_bearer_token() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"exports": [{"id": "export-1"}]})

    client = HealthExportClient(
        base_url="https://health.example.test",
        api_token="secret-token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = client.list_exports(limit=1)

    assert result == [{"id": "export-1"}]
    assert captured[0].url.path == "/v1/exports"
    assert captured[0].headers["authorization"] == "Bearer secret-token"


def test_client_passes_limit_when_listing_exports() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["limit"] == "3"
        return httpx.Response(200, json={"exports": []})

    client = HealthExportClient(
        base_url="https://health.example.test/",
        api_token="secret-token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert client.list_exports(limit=3) == []


def test_client_requests_metric_summary_with_range_and_granularity() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/health/summary"
        assert dict(request.url.params) == {
            "metric": "step_count",
            "date_range": "last 3 days",
            "granularity": "day",
        }
        return httpx.Response(200, json={"metric": "step_count", "series": []})

    client = HealthExportClient(
        base_url="https://health.example.test",
        api_token="secret-token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert client.get_metric_summary(
        metric="step_count", date_range="last 3 days", granularity="day"
    ) == {"metric": "step_count", "series": []}


def test_client_requests_health_metrics_catalog() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/health/metrics"
        return httpx.Response(200, json={"metrics": [{"metric": "step_count", "unit": "count"}]})

    client = HealthExportClient(
        base_url="https://health.example.test",
        api_token="secret-token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert client.list_metrics() == [{"metric": "step_count", "unit": "count"}]
