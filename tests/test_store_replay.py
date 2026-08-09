"""Replay order, which became load-bearing when ingestion started replacing.

`backfill` runs at startup over every file not yet recorded in
`processed_exports`. While ingestion only ever appended, the order it visited
them in did not matter. Now that a payload replaces the window it covers,
replaying an older export after a newer one overwrites good data with stale
data — and the files are named with random tokens, so filename order is
effectively shuffled.
"""
import json
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

from health_export_api.app import create_app
from health_export_api.store import Store, _received_at_of

HEADERS = {"Authorization": "Bearer test-token"}


def write_export(directory: Path, name: str, received_at: str, qty: float) -> None:
    """An export file as the ingestion path writes them."""
    (directory / f"{name}.json").write_text(json.dumps({
        "id": name,
        "received_at": received_at,
        "payload": {"data": {"metrics": [{
            "name": "step_count", "units": "count",
            "data": [{"date": "2026-07-10 08:00:00 -0400", "qty": qty}],
        }]}},
    }))


def total(client: TestClient) -> float | None:
    body = client.get("/v1/health/summary", headers=HEADERS,
                      params={"metric": "step_count", "start_date": "2026-07-10",
                              "end_date": "2026-07-10"}).json()
    return next((r["value"] for r in body["series"]), None)


def test_backfill_replays_oldest_first_regardless_of_filename(
    tmp_path: Path,
) -> None:
    # Names chosen so alphabetical order is the reverse of arrival order, which
    # is what a random token gives you half the time.
    write_export(tmp_path, "zzz_first", "2026-07-10T09:00:00Z", 100.0)
    write_export(tmp_path, "aaa_second", "2026-07-10T18:00:00Z", 250.0)

    client = TestClient(create_app(storage_dir=tmp_path, api_token="test-token",
                                   summary_today=date(2026, 7, 12)))

    # The later export is the authority for that window.
    assert total(client) == 250.0


def test_received_at_is_read_from_the_header_not_the_whole_file(
    tmp_path: Path,
) -> None:
    # The archive is hundreds of megabytes; parsing all of it just to sort
    # once OOM-killed this service before.
    write_export(tmp_path, "one", "2026-07-10T09:00:00Z", 1.0)
    path = tmp_path / "one.json"

    assert _received_at_of(path) == "2026-07-10T09:00:00Z"


def test_a_headerless_file_falls_back_to_its_mtime(tmp_path: Path) -> None:
    # A truncated file must not sort to the front and replay first.
    broken = tmp_path / "broken.json"
    broken.write_text("{")

    stamp = _received_at_of(broken)

    assert stamp and stamp.startswith("20")


def test_backfill_skips_what_it_has_already_ingested(tmp_path: Path) -> None:
    write_export(tmp_path, "one", "2026-07-10T09:00:00Z", 100.0)
    db = tmp_path / "health_export.db"
    store = Store(db)
    store.backfill(tmp_path)

    # A second pass must be a no-op, not a replay that re-replaces windows.
    store.backfill(tmp_path)

    client = TestClient(create_app(storage_dir=tmp_path, api_token="test-token",
                                   summary_today=date(2026, 7, 12)))
    assert total(client) == 100.0
