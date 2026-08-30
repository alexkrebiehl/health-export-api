"""Heart rate: parsing, interpolation onto a route, and the backfill.

The series arrives at roughly a sample a minute against a route point a second,
so almost every point sits between two samples rather than on one. Everything
here is about that gap being filled honestly.
"""
import json
from pathlib import Path
from typing import Any

from health_export_api.store import Store
from health_export_api.workout_normalization import (
    _iter_heart_rate_samples,
    interpolate_heart_rate,
)


def _record(workout_id: str, series: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": "export-1",
        "received_at": "2026-07-10T09:00:00-04:00",
        "payload": {"data": {"workouts": [
            {"id": workout_id, "name": "Outdoor Walk",
             "start": "2026-07-10 08:00:00 -0400",
             "heartRateData": series},
        ]}},
    }


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_the_series_is_read_from_the_capitalised_avg_field() -> None:
    """`Avg` is capitalised here, unlike every other field on the workout."""
    samples = list(_iter_heart_rate_samples([_record("w1", [
        {"date": "2026-07-10 08:00:00 -0400", "Avg": 112.5, "Max": 117, "Min": 105},
    ])]))

    assert len(samples) == 1
    assert samples[0].workout_id == "w1"
    assert samples[0].bpm == 112.5


def test_unusable_samples_are_skipped_not_zeroed() -> None:
    """A zero or missing reading is absence, and must not read as a low pulse."""
    samples = list(_iter_heart_rate_samples([_record("w1", [
        {"date": "2026-07-10 08:00:00 -0400", "Avg": 0},
        {"date": "2026-07-10 08:01:00 -0400", "Avg": None},
        {"date": "2026-07-10 08:02:00 -0400"},
        {"date": "not a date", "Avg": 120},
        {"date": "2026-07-10 08:04:00 -0400", "Avg": 120},
    ])]))

    assert [s.bpm for s in samples] == [120]


# ---------------------------------------------------------------------------
# Interpolation
# ---------------------------------------------------------------------------


def test_a_point_between_samples_is_weighted_by_where_it_falls() -> None:
    values = interpolate_heart_rate([0, 15, 30, 45, 60], [(0, 100.0), (60, 160.0)])

    assert values == [100.0, 115.0, 130.0, 145.0, 160.0]


def test_outside_the_sampled_span_the_value_is_held_not_extrapolated() -> None:
    """A route usually runs a little past the last sample — ~3% of points here.

    Continuing the last slope would invent a trend the watch never recorded.
    """
    values = interpolate_heart_rate([-600, 0, 60, 600], [(0, 100.0), (60, 160.0)])

    assert values == [100.0, 100.0, 160.0, 160.0]


def test_one_sample_colours_the_whole_route_flat() -> None:
    assert interpolate_heart_rate([0, 10, 20], [(5, 120.0)]) == [120.0, 120.0, 120.0]


def test_no_samples_yields_no_values_rather_than_zeros() -> None:
    """None is "not measured"; 0 would be a reading, and a wrong one."""
    assert interpolate_heart_rate([0, 1, 2], []) == [None, None, None]


def test_duplicate_timestamps_do_not_divide_by_zero() -> None:
    values = interpolate_heart_rate([0, 5, 10], [(5, 100.0), (5, 140.0)])

    assert all(v is not None for v in values)


def test_the_walk_is_linear_in_the_number_of_points() -> None:
    """The cursor only moves forward — a search per point would not scale."""
    samples = [(float(i * 60), 100.0 + i) for i in range(30)]
    points = [float(i) for i in range(30 * 60)]

    values = interpolate_heart_rate(points, samples)

    assert len(values) == len(points)
    assert values[0] == 100.0
    assert values[-1] == samples[-1][1]


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------


def _write(directory: Path, name: str, received_at: str, record: dict) -> None:
    record = {**record, "id": name, "received_at": received_at}
    (directory / f"{name}.json").write_text(json.dumps(record))


def test_backfill_populates_from_files_already_ingested(tmp_path: Path) -> None:
    """`backfill` skips processed exports, so a new table needs its own pass."""
    _write(tmp_path, "a", "2026-07-10T09:00:00-04:00", _record("w1", [
        {"date": "2026-07-10 08:00:00 -0400", "Avg": 110},
        {"date": "2026-07-10 08:01:00 -0400", "Avg": 130},
    ]))
    store = Store(tmp_path / "db.sqlite")
    store.backfill(tmp_path)  # sessions first, as create_app does

    store.backfill_heart_rate(tmp_path)

    con = store._connect()
    try:
        rows = con.execute(
            "SELECT bpm FROM workout_heart_rate ORDER BY sampled_iso"
        ).fetchall()
    finally:
        con.close()
    assert [r["bpm"] for r in rows] == [110.0, 130.0]


def test_backfill_is_a_no_op_once_the_table_has_rows(tmp_path: Path) -> None:
    """It runs on every start, so re-running must cost nothing and change nothing."""
    _write(tmp_path, "a", "2026-07-10T09:00:00-04:00", _record("w1", [
        {"date": "2026-07-10 08:00:00 -0400", "Avg": 110},
    ]))
    store = Store(tmp_path / "db.sqlite")
    store.backfill(tmp_path)
    store.backfill_heart_rate(tmp_path)

    # A later file that a second pass would pick up, if it ran at all.
    _write(tmp_path, "b", "2026-07-11T09:00:00-04:00", _record("w2", [
        {"date": "2026-07-11 08:00:00 -0400", "Avg": 150},
    ]))
    store.backfill_heart_rate(tmp_path)

    con = store._connect()
    try:
        count = con.execute("SELECT COUNT(*) FROM workout_heart_rate").fetchone()[0]
    finally:
        con.close()
    assert count == 1


def test_a_resend_replaces_a_workouts_series_rather_than_doubling_it(
    tmp_path: Path,
) -> None:
    """Re-sends carry the same workout with different values.

    Accumulating both would average one against the other, so a workout's
    series is replaced wholesale — the same contract the rest of ingest keeps.
    """
    store = Store(tmp_path / "db.sqlite")
    store.ingest("e1", "2026-07-10T09:00:00-04:00", _record("w1", [
        {"date": "2026-07-10 08:00:00 -0400", "Avg": 110},
        {"date": "2026-07-10 08:01:00 -0400", "Avg": 130},
    ])["payload"])
    store.ingest("e2", "2026-07-10T10:00:00-04:00", _record("w1", [
        {"date": "2026-07-10 08:00:00 -0400", "Avg": 115},
    ])["payload"])

    con = store._connect()
    try:
        rows = con.execute(
            "SELECT bpm FROM workout_heart_rate WHERE workout_id = 'w1'"
        ).fetchall()
    finally:
        con.close()
    assert [r["bpm"] for r in rows] == [115.0]


def test_a_series_without_a_session_is_dropped_not_fatal(tmp_path: Path) -> None:
    """One unusable workout must not cost the whole file its heart rate.

    A workout missing a name or with an unparseable start produces no session
    row, and the foreign key would reject its samples.
    """
    store = Store(tmp_path / "db.sqlite")
    payload = {"data": {"workouts": [
        {"id": "orphan",  # no name/start, so no session row
         "heartRateData": [{"date": "2026-07-10 08:00:00 -0400", "Avg": 99}]},
        {"id": "good", "name": "Outdoor Walk",
         "start": "2026-07-10 08:00:00 -0400",
         "heartRateData": [{"date": "2026-07-10 08:00:00 -0400", "Avg": 120}]},
    ]}}

    store.ingest("e1", "2026-07-10T09:00:00-04:00", payload)

    con = store._connect()
    try:
        rows = con.execute(
            "SELECT workout_id, bpm FROM workout_heart_rate"
        ).fetchall()
    finally:
        con.close()
    assert [(r["workout_id"], r["bpm"]) for r in rows] == [("good", 120.0)]
