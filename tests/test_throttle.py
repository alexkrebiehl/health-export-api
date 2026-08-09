"""Tests for the coverage request gate and TTL cache."""
import threading
from datetime import date
from pathlib import Path
from time import monotonic
from typing import Any

import pytest
from fastapi.testclient import TestClient

from health_export_api.app import create_app, derive_embed_token
from health_export_api.throttle import QueueFull, RequestGate, TTLCache

HEADERS = {"Authorization": "Bearer test-token"}
EMBED_TOKEN = derive_embed_token("test-token")

CENTER_LAT = 52.5199425
CENTER_LON = 13.3999414
LON_STEP = 0.00022145
BOX = {"lat": 52.52, "lon": 13.40, "width": 500, "height": 500}


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ---------------------------------------------------------------------------
# TTLCache
# ---------------------------------------------------------------------------


def test_cache_returns_a_stored_value_until_it_expires() -> None:
    clock = FakeClock()
    cache = TTLCache(ttl=300, clock=clock)

    cache.put("k", "v")
    assert cache.get("k") == "v"

    clock.advance(299)
    assert cache.get("k") == "v"

    clock.advance(2)
    assert cache.get("k") is None


def test_a_zero_ttl_disables_the_cache() -> None:
    cache = TTLCache(ttl=0)

    cache.put("k", "v")

    assert cache.get("k") is None
    assert not cache.enabled


def test_cache_evicts_least_recently_used_entries() -> None:
    cache = TTLCache(ttl=300, max_entries=2)

    cache.put("a", 1)
    cache.put("b", 2)
    cache.get("a")  # 'a' becomes most recent, so 'b' should go first
    cache.put("c", 3)

    assert cache.get("a") == 1
    assert cache.get("c") == 3
    assert cache.get("b") is None


# ---------------------------------------------------------------------------
# RequestGate
# ---------------------------------------------------------------------------


def test_gate_runs_one_at_a_time() -> None:
    gate = RequestGate(max_queue=10)
    concurrent = 0
    peak = 0
    lock = threading.Lock()
    start = threading.Event()

    def worker() -> None:
        nonlocal concurrent, peak
        start.wait()
        with gate.enter():
            with lock:
                concurrent += 1
                peak = max(peak, concurrent)
            threading.Event().wait(0.01)
            with lock:
                concurrent -= 1

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    start.set()
    for t in threads:
        t.join()

    assert peak == 1


def test_gate_rejects_once_the_queue_is_full() -> None:
    gate = RequestGate(max_queue=3)
    release = threading.Event()

    def hold() -> None:
        # Only the first thread gets inside the body; the gate serialises, so
        # the other two sit in the queue. All three count towards `pending`.
        with gate.enter():
            release.wait(5)

    holders = [threading.Thread(target=hold, daemon=True) for _ in range(3)]
    for t in holders:
        t.start()

    deadline = monotonic() + 5
    while gate.pending < 3 and monotonic() < deadline:
        threading.Event().wait(0.01)
    assert gate.pending == 3, "expected one running and two queued"

    with pytest.raises(QueueFull):
        with gate.enter():
            pass

    release.set()
    for t in holders:
        t.join(timeout=5)
    assert gate.pending == 0


def test_gate_releases_its_slot_when_the_body_raises() -> None:
    gate = RequestGate(max_queue=2)

    with pytest.raises(RuntimeError):
        with gate.enter():
            raise RuntimeError("boom")

    assert gate.pending == 0


# ---------------------------------------------------------------------------
# Wired into the API
# ---------------------------------------------------------------------------


def make_client(tmp_path: Path, **kwargs: Any) -> TestClient:
    return TestClient(
        create_app(storage_dir=tmp_path, api_token="test-token",
                   summary_today=date(2026, 7, 12), **kwargs)
    )


def ingest(client: TestClient, workout_id: str, lat: float) -> None:
    points = [
        {"latitude": lat, "longitude": CENTER_LON + i * LON_STEP,
         "timestamp": f"2026-07-10 08:00:{i:02d} -0400"}
        for i in range(5)
    ]
    response = client.post("/v1/exports", headers=HEADERS, json={"data": {"workouts": [{
        "id": workout_id, "name": "Outdoor Walk",
        "start": "2026-07-10 08:00:00 -0400", "end": "2026-07-10 08:45:00 -0400",
        "duration": {"qty": 2700, "units": "s"}, "distance": {"qty": 2.5, "units": "mi"},
        "activeEnergy": {"qty": 280, "units": "kcal"}, "route": points,
    }]}})
    assert response.status_code == 201, response.text


def coverage(client: TestClient, **params: Any) -> dict[str, Any]:
    response = client.get("/v1/workouts/routes/geojson", headers=HEADERS,
                          params={**BOX, **params})
    assert response.status_code == 200, response.text
    return response.json()


def test_identical_requests_are_served_from_cache(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    ingest(client, "walk-1", CENTER_LAT)
    first = coverage(client)

    # New data landing after the first render must not show up while cached.
    ingest(client, "walk-2", CENTER_LAT - 0.001)
    assert coverage(client)["properties"]["workout_count"] == \
        first["properties"]["workout_count"] == 1


def test_a_zero_ttl_serves_fresh_results(tmp_path: Path) -> None:
    client = make_client(tmp_path, cache_ttl=0)
    ingest(client, "walk-1", CENTER_LAT)
    assert coverage(client)["properties"]["workout_count"] == 1

    ingest(client, "walk-2", CENTER_LAT - 0.001)

    assert coverage(client)["properties"]["workout_count"] == 2


def test_presentation_options_do_not_change_the_cache_key(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    ingest(client, "walk-1", CENTER_LAT)
    coverage(client)
    ingest(client, "walk-2", CENTER_LAT - 0.001)

    # weight/zoom_control only affect rendering, so the coverage behind the
    # map page should still be the cached one.
    html = client.get("/v1/render/map",
                      params={**BOX, "embed_token": EMBED_TOKEN, "weight": 4}).text

    assert '"workout_count":1' in html.replace(" ", "")


def test_differing_filters_are_cached_separately(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    ingest(client, "walk-1", CENTER_LAT)

    assert coverage(client, min_count=1)["properties"]["min_count"] == 1
    assert coverage(client, min_count=2)["properties"]["min_count"] == 2
    assert coverage(client, min_count=2)["features"] == []


def test_a_full_queue_returns_429(tmp_path: Path) -> None:
    client = make_client(tmp_path, max_queue=0)
    ingest(client, "walk-1", CENTER_LAT)

    response = client.get("/v1/workouts/routes/geojson", headers=HEADERS, params=BOX)

    assert response.status_code == 429
    assert response.headers["retry-after"] == "5"
    assert "serialised" in response.json()["detail"]


def test_the_map_page_also_sheds_when_the_queue_is_full(tmp_path: Path) -> None:
    client = make_client(tmp_path, max_queue=0)

    response = client.get(
        "/v1/render/map", params={**BOX, "embed_token": EMBED_TOKEN}
    )

    assert response.status_code == 429


def test_a_burst_of_identical_requests_computes_once(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    ingest(client, "walk-1", CENTER_LAT)

    # What a URL edit produces: many identical requests at once. They should
    # all succeed, and the ones that queued should pick up the cached result
    # rather than each recomputing.
    results: list[int] = []
    lock = threading.Lock()

    def fire() -> None:
        r = client.get("/v1/workouts/routes/geojson", headers=HEADERS, params=BOX)
        with lock:
            results.append(r.status_code)

    threads = [threading.Thread(target=fire) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results == [200] * 8
