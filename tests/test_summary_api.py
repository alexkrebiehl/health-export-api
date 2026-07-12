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
    """sleepStart before 12:00 or at/after 20:00 → main sleep; 12:00–20:00 same-day end → nap."""
    client = make_client(tmp_path)
    headers = {"Authorization": "Bearer test-token"}
    # Main night starts just before midnight (23:00), nap starts at 14:00 same wake date.
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


def test_sleep_morning_sleep_in_is_main_sleep(tmp_path: Path) -> None:
    """A session starting between midnight and noon (e.g. 09:00) is main sleep, not a nap."""
    client = make_client(tmp_path)
    headers = {"Authorization": "Bearer test-token"}
    payload = _sleep_payload([
        {
            "date": "2026-07-10 00:00:00 -0400",
            "sleepStart": "2026-07-10 09:00:00 -0400",
            "sleepEnd": "2026-07-10 11:00:00 -0400",
            "totalSleep": 2.0, "deep": 0.3, "core": 1.2, "rem": 0.4, "awake": 0.1,
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

    assert len(main["series"]) == 1
    assert main["series"][0]["value"] == 2.0
    assert nap["series"] == []


def test_sleep_evening_start_crossing_midnight_discarded_as_artifact(tmp_path: Path) -> None:
    """sleepStart in [08:00, 20:00) that crosses midnight is an Apple Watch artifact and discarded."""
    client = make_client(tmp_path)
    headers = {"Authorization": "Bearer test-token"}
    # The real nap (same-day end) and the artifact (crosses midnight from same start)
    payload = _sleep_payload([
        {
            # Real short nap: 17:42 → 18:23 same day
            "date": "2026-05-04 00:00:00 -0400",
            "sleepStart": "2026-05-04 17:42:00 -0400",
            "sleepEnd": "2026-05-04 18:23:00 -0400",
            "totalSleep": 0.69, "deep": 0.0, "core": 0.5, "rem": 0.1, "awake": 0.0,
            "source": "Apple Watch",
        },
        {
            # Artifact: same start 17:42, end next morning — should be discarded
            "date": "2026-05-05 00:00:00 -0400",
            "sleepStart": "2026-05-04 17:42:00 -0400",
            "sleepEnd": "2026-05-05 07:49:00 -0400",
            "totalSleep": 7.03, "deep": 0.8, "core": 4.0, "rem": 1.5, "awake": 0.3,
            "source": "Apple Watch",
        },
    ])
    assert client.post("/v1/exports", headers=headers, json=payload).status_code == 201

    main = client.get("/v1/health/summary", headers=headers, params={
        "metric": "sleep_analysis",
        "start_date": "2026-05-04", "end_date": "2026-05-05",
    }).json()
    nap = client.get("/v1/health/summary", headers=headers, params={
        "metric": "sleep_analysis_nap",
        "start_date": "2026-05-04", "end_date": "2026-05-05",
    }).json()

    # Artifact is discarded; no main sleep in this window; nap appears on its end date.
    assert main["series"] == []
    assert len(nap["series"]) == 1
    assert abs(nap["series"][0]["value"] - 0.69) < 0.01


def test_sleep_classification_uses_embedded_timezone_not_server_timezone(tmp_path: Path) -> None:
    """Hour comparison uses the offset in the timestamp string, not the server's local timezone.

    A sleepStart of '04:49 -0400' is 4 AM EDT. It must remain classified as main sleep
    (start_hour=4, outside [8,20)) even when the server runs in UTC (where 04:49-0400 = 08:49),
    which would incorrectly fall inside the daytime window.
    """
    client = make_client(tmp_path)
    headers = {"Authorization": "Bearer test-token"}
    payload = _sleep_payload([
        {
            # Full overnight session: 23:41 EDT → 07:31 EDT next day — main sleep
            "date": "2026-07-12 00:00:00 -0400",
            "sleepStart": "2026-07-11 23:41:00 -0400",
            "sleepEnd": "2026-07-12 07:31:00 -0400",
            "totalSleep": 7.69, "deep": 0.8, "core": 4.8, "rem": 1.4, "awake": 0.4,
            "source": "Apple Watch",
        },
        {
            # Sub-record: 04:49 EDT → 07:31 EDT — also main sleep (start_hour=4),
            # deduplicated away by max-value logic (7.69 > 2.66).
            "date": "2026-07-12 00:00:00 -0400",
            "sleepStart": "2026-07-12 04:49:00 -0400",
            "sleepEnd": "2026-07-12 07:31:00 -0400",
            "totalSleep": 2.66, "deep": 0.3, "core": 1.5, "rem": 0.5, "awake": 0.1,
            "source": "Apple Watch",
        },
    ])
    assert client.post("/v1/exports", headers=headers, json=payload).status_code == 201

    main = client.get("/v1/health/summary", headers=headers, params={
        "metric": "sleep_analysis", "start_date": "2026-07-12", "end_date": "2026-07-12",
    }).json()
    nap = client.get("/v1/health/summary", headers=headers, params={
        "metric": "sleep_analysis_nap", "start_date": "2026-07-12", "end_date": "2026-07-12",
    }).json()

    # 7.69 hr main sleep survives; sub-record deduped out; no naps
    assert len(main["series"]) == 1
    assert abs(main["series"][0]["value"] - 7.69) < 0.01
    assert nap["series"] == []


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
