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


def steps(samples: list[tuple[str, float]]) -> dict:
    return {"data": {"metrics": [{
        "name": "step_count", "units": "count",
        "data": [{"date": d, "qty": q} for d, q in samples],
    }]}}


def day_total(client: TestClient, day: str = "2026-07-10") -> float | None:
    body = client.get("/v1/health/summary",
                      headers={"Authorization": "Bearer test-token"},
                      params={"metric": "step_count", "start_date": day,
                              "end_date": day, "granularity": "day"}).json()
    return next((r["value"] for r in body["series"] if r["period"] == day), None)


def post(client: TestClient, payload: dict) -> None:
    response = client.post("/v1/exports",
                           headers={"Authorization": "Bearer test-token"},
                           json=payload)
    assert response.status_code == 201, response.text


def test_a_full_reexport_replaces_rather_than_adds_to_the_same_day(
    tmp_path: Path,
) -> None:
    """Doubling every step count on the dashboard.

    The scheduled push sends a sample's real time; a manual full export sends
    the same day bucketed to the minute, with slightly different values. Both
    are complete for that day, so keying on (metric, timestamp, value) kept
    both copies and the summed total came out as their sum.
    """
    client = make_client(tmp_path)
    post(client, steps([("2026-07-10 08:05:28 -0400", 12.0),
                        ("2026-07-10 08:19:28 -0400", 40.0),
                        ("2026-07-10 09:31:28 -0400", 55.0)]))
    assert day_total(client) == 107.0

    # The same day again, re-bucketed and re-rounded, as a full export sends it.
    post(client, steps([("2026-07-10 08:05:00 -0400", 12.4),
                        ("2026-07-10 08:20:00 -0400", 41.1),
                        ("2026-07-10 09:32:00 -0400", 56.2)]))

    assert day_total(client) == 109.7, "the re-export must replace, not stack"


def test_a_sample_before_the_reexported_span_survives_it(tmp_path: Path) -> None:
    """The one place the window rule leaves a residue, pinned deliberately.

    Bucketing can move a sample *forward* over a minute boundary, so the full
    export's span starts fractionally after the reading it replaced and that
    reading is outside it. Widening the window to catch it is the worse trade:
    the scheduled pushes are back-to-back, so a wider window would delete the
    tail of the previous push — data nothing re-supplies. A payload is the
    authority for the span it covers and no more.

    The residue is one bucket at each edge of a re-export, and only when the
    producer changes granularity: 12 steps against a day's 10,620.
    """
    client = make_client(tmp_path)
    post(client, steps([("2026-07-10 08:04:28 -0400", 12.0),
                        ("2026-07-10 08:19:28 -0400", 40.0)]))

    post(client, steps([("2026-07-10 08:05:00 -0400", 12.4),
                        ("2026-07-10 08:20:00 -0400", 41.1)]))

    assert day_total(client) == 12.0 + 53.5


def test_an_exact_reexport_is_idempotent_despite_float_noise(
    tmp_path: Path,
) -> None:
    # Two floats that print the same can differ in their last bits, and the
    # UNIQUE index compares bits — which is how identical re-sends slipped
    # through. Replacing the window does not care.
    client = make_client(tmp_path)
    payload = steps([("2026-07-10 12:47:00 -0400", 78.865),
                     ("2026-07-10 12:52:00 -0400", 0.1 + 0.2)])
    post(client, payload)
    first = day_total(client)

    post(client, payload)

    assert day_total(client) == first


def test_an_incremental_push_only_replaces_its_own_window(tmp_path: Path) -> None:
    """The scheduled exports are deltas, not the whole day so far.

    So the replacement has to be the span the payload actually covers. Wiping
    the whole day would throw away every earlier push.
    """
    client = make_client(tmp_path)
    post(client, steps([("2026-07-10 08:00:00 -0400", 100.0)]))
    post(client, steps([("2026-07-10 12:00:00 -0400", 200.0)]))
    post(client, steps([("2026-07-10 20:00:00 -0400", 300.0)]))
    assert day_total(client) == 600.0

    # A re-push of the middle window alone leaves the other two untouched.
    post(client, steps([("2026-07-10 12:00:00 -0400", 250.0)]))

    assert day_total(client) == 650.0


def test_replacement_is_scoped_to_the_metric(tmp_path: Path) -> None:
    # A payload carrying steps must not disturb another metric in that span.
    client = make_client(tmp_path)
    post(client, {"data": {"metrics": [
        {"name": "step_count", "units": "count",
         "data": [{"date": "2026-07-10 08:00:00 -0400", "qty": 100.0}]},
        {"name": "flights_climbed", "units": "count",
         "data": [{"date": "2026-07-10 09:00:00 -0400", "qty": 7.0}]},
    ]}})
    post(client, steps([("2026-07-10 07:00:00 -0400", 50.0),
                        ("2026-07-10 23:00:00 -0400", 60.0)]))

    body = client.get("/v1/health/summary",
                      headers={"Authorization": "Bearer test-token"},
                      params={"metric": "flights_climbed",
                              "start_date": "2026-07-10",
                              "end_date": "2026-07-10"}).json()
    assert body["series"] == [
        {"period": "2026-07-10", "sample_count": 1, "value": 7.0}
    ]


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
