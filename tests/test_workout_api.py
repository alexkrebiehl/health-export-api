"""Tests for GET /v1/workouts/types and GET /v1/workouts/summary."""
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

from health_export_api.app import create_app

HEADERS = {"Authorization": "Bearer test-token"}

WALK_PAYLOAD = {
    "data": {
        "workouts": [
            {
                "id": "walk-1",
                "name": "Outdoor Walk",
                "start": "2026-07-10 08:00:00 -0400",
                "end":   "2026-07-10 08:45:00 -0400",
                "duration": {"qty": 2700, "units": "s"},
                "distance": {"qty": 2.5, "units": "mi"},
                "activeEnergy": {"qty": 280, "units": "kcal"},
                "avgHeartRate": {"qty": 112, "units": "count/min"},
            },
            {
                "id": "walk-2",
                "name": "Outdoor Walk",
                "start": "2026-07-11 08:00:00 -0400",
                "end":   "2026-07-11 08:30:00 -0400",
                "duration": {"qty": 1800, "units": "s"},
                "distance": {"qty": 1.5, "units": "mi"},
                "activeEnergy": {"qty": 180, "units": "kcal"},
                "avgHeartRate": {"qty": 105, "units": "count/min"},
            },
            # Hevy-sourced — should be excluded by default
            {
                "id": "hevy-1",
                "name": "Traditional Strength Training",
                "start": "2026-07-10 11:00:00 -0400",
                "end":   "2026-07-10 11:38:00 -0400",
                "duration": {"qty": 2280, "units": "s"},
                "distance": {"qty": 0, "units": "mi"},
                "activeEnergy": {"qty": 120, "units": "kcal"},
                "avgHeartRate": {"qty": 98, "units": "count/min"},
            },
        ]
    }
}


def make_client(tmp_path: Path) -> TestClient:
    return TestClient(
        create_app(storage_dir=tmp_path, api_token="test-token",
                   summary_today=date(2026, 7, 12))
    )


def test_workout_types_lists_non_hevy_types_by_default(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    client.post("/v1/exports", headers=HEADERS, json=WALK_PAYLOAD)

    resp = client.get("/v1/workouts/types", headers=HEADERS)

    assert resp.status_code == 200
    types = resp.json()["workout_types"]
    names = [t["name"] for t in types]
    assert "Outdoor Walk" in names
    assert "Traditional Strength Training" not in names


def test_workout_types_includes_hevy_when_requested(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    client.post("/v1/exports", headers=HEADERS, json=WALK_PAYLOAD)

    resp = client.get("/v1/workouts/types?include_hevy=true", headers=HEADERS)

    assert resp.status_code == 200
    names = [t["name"] for t in resp.json()["workout_types"]]
    assert "Traditional Strength Training" in names


def test_workout_summary_aggregates_daily_sessions(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    client.post("/v1/exports", headers=HEADERS, json=WALK_PAYLOAD)

    resp = client.get(
        "/v1/workouts/summary",
        headers=HEADERS,
        params={"workout_type": "Outdoor Walk", "date_range": "last 3 days", "granularity": "day"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["workout_type"] == "Outdoor Walk"
    assert body["start_date"] == "2026-07-10"
    assert body["end_date"] == "2026-07-12"
    series = body["series"]
    assert len(series) == 2  # Jul 10 and Jul 11 only (Jul 12 has no walk)
    jul10 = next(s for s in series if s["period"] == "2026-07-10")
    assert jul10["sessions"] == 1
    assert jul10["total_duration_min"] == 45.0
    assert jul10["total_distance_mi"] == 2.5
    assert jul10["avg_heart_rate"] == 112.0


def test_workout_summary_deduplicates_across_exports(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    # Post same payload twice (simulates batch re-export overlap)
    client.post("/v1/exports", headers=HEADERS, json=WALK_PAYLOAD)
    client.post("/v1/exports", headers=HEADERS, json=WALK_PAYLOAD)

    resp = client.get(
        "/v1/workouts/summary",
        headers=HEADERS,
        params={"workout_type": "Outdoor Walk", "start_date": "2026-07-10", "end_date": "2026-07-11"},
    )

    assert resp.status_code == 200
    series = resp.json()["series"]
    jul10 = next(s for s in series if s["period"] == "2026-07-10")
    assert jul10["sessions"] == 1  # deduped by workout id


def test_workout_summary_excludes_hevy_by_default(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    client.post("/v1/exports", headers=HEADERS, json=WALK_PAYLOAD)

    resp = client.get(
        "/v1/workouts/summary",
        headers=HEADERS,
        params={"workout_type": "Traditional Strength Training",
                "start_date": "2026-07-10", "end_date": "2026-07-12"},
    )

    assert resp.status_code == 200
    assert resp.json()["series"] == []  # excluded by default


def test_workout_summary_includes_hevy_when_requested(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    client.post("/v1/exports", headers=HEADERS, json=WALK_PAYLOAD)

    resp = client.get(
        "/v1/workouts/summary",
        headers=HEADERS,
        params={"workout_type": "Traditional Strength Training",
                "start_date": "2026-07-10", "end_date": "2026-07-12",
                "include_hevy": "true"},
    )

    assert resp.status_code == 200
    assert len(resp.json()["series"]) == 1


def test_workout_summary_all_types(tmp_path: Path) -> None:
    """Omitting workout_type aggregates all non-Hevy workout types together."""
    client = make_client(tmp_path)
    client.post("/v1/exports", headers=HEADERS, json=WALK_PAYLOAD)

    resp = client.get(
        "/v1/workouts/summary",
        headers=HEADERS,
        params={"start_date": "2026-07-10", "end_date": "2026-07-11"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["workout_type"] is None  # all types
    total_sessions = sum(s["sessions"] for s in body["series"])
    assert total_sessions == 2  # 2 walks, Hevy excluded


def test_workout_endpoints_require_auth(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    assert client.get("/v1/workouts/types").status_code == 401
    assert client.get("/v1/workouts/summary", params={"start_date": "2026-07-10", "end_date": "2026-07-12"}).status_code == 401
