"""Tests for the stat tiles and their window maths."""
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from health_export_api.app import create_app, derive_embed_token
from health_export_api.chart_page import latest_reading, window_change
from health_export_api.stat_page import render_change_tile, render_latest_tile

HEADERS = {"Authorization": "Bearer test-token"}
EMBED_TOKEN = derive_embed_token("test-token")
TODAY = date(2026, 7, 12)


def d(day: str) -> date:
    return date.fromisoformat(day)


# ---------------------------------------------------------------------------
# window_change
# ---------------------------------------------------------------------------


def test_window_change_compares_two_equal_adjacent_weeks() -> None:
    # Recent week averages 100, the week before averages 110.
    points = [(d(f"2026-07-{i:02d}"), 110.0) for i in range(1, 8)]  # 1..7  (prior)
    points += [(d(f"2026-07-{i:02d}"), 100.0) for i in range(8, 15)]  # 8..14 (recent)

    result = window_change(points, 7, d("2026-07-14"))

    assert result is not None
    recent, prior, delta = result
    assert recent == pytest.approx(100.0)
    assert prior == pytest.approx(110.0)
    assert delta == pytest.approx(-10.0)


def test_window_change_windows_are_calendar_days_not_point_counts() -> None:
    # Only three readings in the recent week and two in the prior one; the
    # means must use what is there, not stretch the window to fill a quota.
    points = [
        (d("2026-07-01"), 200.0),
        (d("2026-07-02"), 204.0),
        (d("2026-07-10"), 100.0),
        (d("2026-07-12"), 104.0),
        (d("2026-07-14"), 108.0),
    ]

    recent, prior, delta = window_change(points, 7, d("2026-07-14"))

    assert recent == pytest.approx(104.0)  # 10th, 12th, 14th
    assert prior == pytest.approx(202.0)  # 1st, 2nd
    assert delta == pytest.approx(-98.0)


def test_window_change_is_none_when_a_window_is_empty() -> None:
    only_recent = [(d("2026-07-14"), 100.0)]

    assert window_change(only_recent, 7, d("2026-07-14")) is None
    assert window_change([], 7, d("2026-07-14")) is None


def test_window_change_excludes_readings_older_than_both_windows() -> None:
    points = [
        (d("2026-06-01"), 999.0),  # ancient, must not enter either mean
        (d("2026-07-02"), 110.0),
        (d("2026-07-10"), 100.0),
    ]

    recent, prior, _ = window_change(points, 7, d("2026-07-14"))

    assert recent == pytest.approx(100.0)
    assert prior == pytest.approx(110.0)


def test_latest_reading_picks_the_most_recent() -> None:
    points = [(d("2026-07-01"), 1.0), (d("2026-07-14"), 2.0), (d("2026-07-08"), 3.0)]

    assert latest_reading(points) == (d("2026-07-14"), 2.0)
    assert latest_reading([]) is None


# ---------------------------------------------------------------------------
# Tile rendering
# ---------------------------------------------------------------------------


def test_latest_tile_shows_value_unit_and_freshness() -> None:
    html = render_latest_tile((d("2026-07-12"), 191.4), unit="lb", today=TODAY)

    assert "191.4" in html and "lb" in html
    assert "Today" in html
    # A standalone figure uses proportional figures; tabular pads each digit to
    # a zero's width and reads loose at display sizes. (Matching the property,
    # not the word — the stylesheet has a comment explaining the choice.)
    assert "font-variant-numeric" not in html


def test_latest_tile_says_how_stale_an_old_reading_is() -> None:
    assert "Yesterday" in render_latest_tile((d("2026-07-11"), 190.0), today=TODAY)
    assert "5 days ago" in render_latest_tile((d("2026-07-07"), 190.0), today=TODAY)


def test_change_tile_always_shows_direction_without_colour() -> None:
    falling = render_change_tile((191.0, 193.0, -2.0), unit="lb")

    assert "↓" in falling and "2" in falling
    # No good_direction given, so no colour is claimed.
    assert 'class="value good"' not in falling


def test_change_tile_colours_only_the_direction_declared_good() -> None:
    losing = render_change_tile((191.0, 193.0, -2.0), unit="lb", good_direction="down")
    gaining = render_change_tile((193.0, 191.0, 2.0), unit="lb", good_direction="down")

    assert 'class="value good"' in losing
    assert "↓" in losing
    assert 'class="value good"' not in gaining
    assert "↑" in gaining  # direction still readable without colour


def test_tiles_render_an_empty_state_rather_than_failing() -> None:
    assert "No readings yet" in render_latest_tile(None)
    assert "Not enough readings" in render_change_tile(None, window_days=7)


def test_whole_numbers_drop_the_trailing_decimal() -> None:
    assert "2 lb" in render_change_tile(
        (191.0, 193.0, -2.0), unit=" lb"
    ).replace("<span class=\"unit\">", "").replace("</span>", "")


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


def make_client(tmp_path: Path) -> TestClient:
    return TestClient(
        create_app(storage_dir=tmp_path, api_token="test-token", summary_today=TODAY)
    )


def ingest_weight(client: TestClient, readings: dict[str, float]) -> None:
    response = client.post("/v1/exports", headers=HEADERS, json={"data": {"metrics": [{
        "name": "weight_body_mass", "units": "lb",
        "data": [{"date": f"{day}T07:00:00-04:00", "qty": qty}
                 for day, qty in readings.items()],
    }]}})
    assert response.status_code == 201, response.text


def stat(client: TestClient, **params: Any):
    return client.get("/v1/health/stat",
                      params={"metric": "weight_body_mass",
                              "embed_token": EMBED_TOKEN, **params})


def test_stat_endpoint_requires_a_token(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    assert client.get("/v1/health/stat",
                      params={"metric": "weight_body_mass"}).status_code == 401
    assert stat(client).status_code == 200


def test_latest_stat_reports_the_most_recent_reading(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    ingest_weight(client, {"2026-07-10": 193.0, "2026-07-12": 191.4})

    html = stat(client, stat="latest").text

    assert "191.4" in html and "lb" in html
    assert "Today" in html


def test_change_stat_reports_the_week_over_week_delta(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    # Prior week (Jun 29 - Jul 5) at 200, recent week (Jul 6 - 12) at 196.
    readings = {f"2026-06-{i:02d}": 200.0 for i in range(29, 31)}
    readings |= {f"2026-07-{i:02d}": 200.0 for i in range(1, 6)}
    readings |= {f"2026-07-{i:02d}": 196.0 for i in range(6, 13)}
    ingest_weight(client, readings)

    html = stat(client, stat="change", good_direction="down").text

    assert "↓" in html
    assert "4" in html
    assert 'class="value good"' in html


def test_change_stat_without_enough_history_says_so(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    ingest_weight(client, {"2026-07-12": 191.4})  # nothing in the prior week

    assert "Not enough readings" in stat(client, stat="change").text


def test_an_unknown_metric_renders_an_empty_tile(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    response = client.get("/v1/health/stat",
                          params={"metric": "nope", "embed_token": EMBED_TOKEN})

    assert response.status_code == 200
    assert "No readings yet" in response.text


def test_bad_parameters_are_rejected(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    assert stat(client, stat="sideways").status_code == 422
    assert stat(client, good_direction="left").status_code == 422
    assert stat(client, window=0).status_code == 422
