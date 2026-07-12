from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

from health_export_api.app import create_app


def make_client(tmp_path: Path) -> TestClient:
    return TestClient(
        create_app(
            storage_dir=tmp_path,
            api_token="test-token",
            summary_today=date(2026, 7, 12),
        )
    )


def test_daily_summary_parses_last_n_days_and_deduplicates_reexported_samples(
    tmp_path: Path,
) -> None:
    client = make_client(tmp_path)
    headers = {"Authorization": "Bearer test-token"}
    payload = {
        "data": {
            "metrics": [
                {
                    "name": "step_count",
                    "units": "count",
                    "data": [
                        {"date": "2026-07-10 08:00:00 -0400", "qty": 1200},
                        {"date": "2026-07-11 08:00:00 -0400", "qty": 2300},
                    ],
                }
            ]
        }
    }
    assert client.post("/v1/exports", headers=headers, json=payload).status_code == 201
    assert client.post("/v1/exports", headers=headers, json=payload).status_code == 201

    response = client.get(
        "/v1/summary",
        headers=headers,
        params={
            "metric": "step_count",
            "date_range": "last 3 days",
            "granularity": "day",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "metric": "step_count",
        "unit": "count",
        "aggregation": "sum",
        "granularity": "day",
        "start_date": "2026-07-10",
        "end_date": "2026-07-12",
        "metric_found": True,
        "series": [
            {"period": "2026-07-10", "sample_count": 1, "value": 1200},
            {"period": "2026-07-11", "sample_count": 1, "value": 2300},
        ],
    }


def test_month_summary_supports_named_date_ranges_and_averages_measurements(
    tmp_path: Path,
) -> None:
    client = make_client(tmp_path)
    headers = {"Authorization": "Bearer test-token"}
    payload = {
        "data": {
            "metrics": [
                {
                    "name": "weight_body_mass",
                    "units": "lb",
                    "data": [
                        {"date": "2026-06-30 07:00:00 -0400", "qty": 180},
                        {"date": "2026-07-01 07:00:00 -0400", "qty": 182},
                        {"date": "2026-07-04 07:00:00 -0400", "qty": 184},
                    ],
                }
            ]
        }
    }
    assert client.post("/v1/exports", headers=headers, json=payload).status_code == 201

    response = client.get(
        "/v1/summary",
        headers=headers,
        params={
            "metric": "weight_body_mass",
            "date_range": "June 30 through July 4",
            "granularity": "month",
        },
    )

    assert response.status_code == 200
    assert response.json()["aggregation"] == "average"
    assert response.json()["start_date"] == "2026-06-30"
    assert response.json()["end_date"] == "2026-07-04"
    assert response.json()["series"] == [
        {"period": "2026-06", "sample_count": 1, "value": 180},
        {"period": "2026-07", "sample_count": 2, "value": 183},
    ]


def test_metric_catalog_and_missing_metrics_handle_export_schema_changes(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    headers = {"Authorization": "Bearer test-token"}
    payload = {
        "data": {
            "metrics": [
                {
                    "name": "future_metric",
                    "units": "widgets",
                    "data": [
                        {"date": "2026-07-12 09:00:00 -0400", "qty": 4},
                        {"date": "invalid-date", "qty": 5},
                    ],
                }
            ]
        }
    }
    assert client.post("/v1/exports", headers=headers, json=payload).status_code == 201

    catalog = client.get("/v1/metrics", headers=headers)
    missing = client.get(
        "/v1/summary",
        headers=headers,
        params={
            "metric": "removed_metric",
            "start_date": "2026-07-10",
            "end_date": "2026-07-12",
        },
    )

    assert catalog.status_code == 200
    assert catalog.json() == {"metrics": [{"metric": "future_metric", "unit": "widgets"}]}
    assert missing.status_code == 200
    assert missing.json()["metric_found"] is False
    assert missing.json()["series"] == []
