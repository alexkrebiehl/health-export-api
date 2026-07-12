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
        "/v1/health/summary",
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
        "/v1/health/summary",
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

    catalog = client.get("/v1/health/metrics", headers=headers)
    missing = client.get(
        "/v1/health/summary",
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


def _sleep_payload(sessions: list[dict]) -> dict:
    """Build a health export payload containing sleep_analysis records."""
    return {
        "data": {
            "metrics": [
                {
                    "name": "sleep_analysis",
                    "units": "hr",
                    "data": sessions,
                }
            ]
        }
    }


def test_sleep_main_night_is_longest_session_on_wake_date(tmp_path: Path) -> None:
    """The longest session per wake date is classified as main sleep."""
    client = make_client(tmp_path)
    headers = {"Authorization": "Bearer test-token"}
    # One main night (7 hr) and one nap (1.5 hr), both ending on Jul 10.
    payload = _sleep_payload([
        {
            "date": "2026-07-10 00:00:00 -0400",
            "sleepStart": "2026-07-09 23:00:00 -0400",
            "sleepEnd": "2026-07-10 06:00:00 -0400",
            "totalSleep": 7.0, "deep": 1.0, "core": 4.0, "rem": 1.5, "awake": 0.5,
            "source": "Apple Watch",
        },
        {
            "date": "2026-07-10 00:00:00 -0400",
            "sleepStart": "2026-07-10 14:00:00 -0400",
            "sleepEnd": "2026-07-10 15:30:00 -0400",
            "totalSleep": 1.5, "deep": 0.0, "core": 1.2, "rem": 0.3, "awake": 0.0,
            "source": "Apple Watch",
        },
    ])
    assert client.post("/v1/exports", headers=headers, json=payload).status_code == 201

    main = client.get("/v1/health/summary", headers=headers, params={
        "metric": "sleep_analysis", "start_date": "2026-07-10", "end_date": "2026-07-10",
    }).json()
    nap = client.get("/v1/health/summary", headers=headers, params={
        "metric": "sleep_analysis_nap", "start_date": "2026-07-10", "end_date": "2026-07-10",
    }).json()
    nap_count = client.get("/v1/health/summary", headers=headers, params={
        "metric": "sleep_analysis_nap_count", "start_date": "2026-07-10", "end_date": "2026-07-10",
    }).json()

    assert main["series"] == [{"period": "2026-07-10", "sample_count": 1, "value": 7.0}]
    assert nap["series"] == [{"period": "2026-07-10", "sample_count": 1, "value": 1.5}]
    assert nap_count["series"] == [{"period": "2026-07-10", "sample_count": 1, "value": 1.0}]


def test_sleep_no_nap_when_only_one_session_per_day(tmp_path: Path) -> None:
    """Nights with only one session produce no nap entries."""
    client = make_client(tmp_path)
    headers = {"Authorization": "Bearer test-token"}
    payload = _sleep_payload([
        {
            "date": "2026-07-10 00:00:00 -0400",
            "sleepStart": "2026-07-09 23:00:00 -0400",
            "sleepEnd": "2026-07-10 06:30:00 -0400",
            "totalSleep": 7.0, "deep": 1.0, "core": 4.0, "rem": 1.5, "awake": 0.5,
            "source": "Apple Watch",
        },
    ])
    assert client.post("/v1/exports", headers=headers, json=payload).status_code == 201

    nap = client.get("/v1/health/summary", headers=headers, params={
        "metric": "sleep_analysis_nap", "start_date": "2026-07-10", "end_date": "2026-07-10",
    }).json()
    nap_count = client.get("/v1/health/summary", headers=headers, params={
        "metric": "sleep_analysis_nap_count", "start_date": "2026-07-10", "end_date": "2026-07-10",
    }).json()

    assert nap["series"] == []
    assert nap_count["series"] == []


def test_sleep_dedup_cross_file_fragments_keep_longest(tmp_path: Path) -> None:
    """Stage-transition sub-records across multiple exports are collapsed to the longest."""
    client = make_client(tmp_path)
    headers = {"Authorization": "Bearer test-token"}
    # Simulate three exports: full session (7.69 hr) + two shorter fragments sharing sleepEnd.
    full = _sleep_payload([{
        "date": "2026-07-12 00:00:00 -0400",
        "sleepStart": "2026-07-11 23:41:00 -0400",
        "sleepEnd": "2026-07-12 07:31:00 -0400",
        "totalSleep": 7.69, "deep": 0.8, "core": 4.8, "rem": 1.4, "awake": 0.4,
        "source": "Apple Watch",
    }])
    frag1 = _sleep_payload([{
        "date": "2026-07-12 00:00:00 -0400",
        "sleepStart": "2026-07-12 04:49:00 -0400",
        "sleepEnd": "2026-07-12 07:31:00 -0400",
        "totalSleep": 2.66, "deep": 0.3, "core": 1.5, "rem": 0.5, "awake": 0.1,
        "source": "Apple Watch",
    }])
    frag2 = _sleep_payload([{
        "date": "2026-07-12 00:00:00 -0400",
        "sleepStart": "2026-07-12 05:42:00 -0400",
        "sleepEnd": "2026-07-12 07:31:00 -0400",
        "totalSleep": 1.78, "deep": 0.1, "core": 1.0, "rem": 0.4, "awake": 0.1,
        "source": "Apple Watch",
    }])
    for p in [full, frag1, frag2]:
        assert client.post("/v1/exports", headers=headers, json=p).status_code == 201

    main = client.get("/v1/health/summary", headers=headers, params={
        "metric": "sleep_analysis", "start_date": "2026-07-12", "end_date": "2026-07-12",
    }).json()

    # Only the full 7.69 hr record should survive; no naps since only one unique sleepEnd.
    assert len(main["series"]) == 1
    assert abs(main["series"][0]["value"] - 7.69) < 0.01
    assert main["series"][0]["sample_count"] == 1
