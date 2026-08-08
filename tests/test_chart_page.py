"""Tests for the metric chart page and its series maths."""
import json
import re
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from health_export_api.app import create_app, derive_embed_token
from health_export_api.chart_page import (
    render_chart_page,
    rolling_trend,
    split_on_gaps,
)

HEADERS = {"Authorization": "Bearer test-token"}
EMBED_TOKEN = derive_embed_token("test-token")


def d(day: str) -> date:
    return date.fromisoformat(day)


# ---------------------------------------------------------------------------
# Moving average
# ---------------------------------------------------------------------------


def test_rolling_trend_follows_a_straight_line_exactly() -> None:
    # Perfectly linear data: the fit is that line, so the trend sits on top of
    # the readings rather than lagging behind them the way a mean would.
    points = [(d(f"2026-07-{i:02d}"), 100.0 + 2 * i) for i in range(1, 8)]

    result = rolling_trend(points, 3)

    assert [day for day, _ in result] == [d(f"2026-07-{i:02d}") for i in range(3, 8)]
    for day, value in result:
        assert value == pytest.approx(100.0 + 2 * day.day)


def test_rolling_trend_leads_a_moving_average_on_a_ramp() -> None:
    # The point of a fit over a mean: on a steady climb the trailing mean sits
    # below the latest reading, while the fit projects the slope to it.
    points = [(d(f"2026-07-{i:02d}"), 100.0 + 2 * i) for i in range(1, 8)]
    last_day, fitted = rolling_trend(points, 5)[-1]

    window = [v for day, v in points if (last_day - day).days < 5]
    mean = sum(window) / len(window)

    assert fitted == pytest.approx(114.0)  # the actual reading on the 7th
    assert mean < fitted


def test_rolling_trend_window_is_calendar_days_not_points() -> None:
    # A hole: the window must not reach back over it and fit a line across
    # two clusters a fortnight apart.
    points = [
        (d("2026-07-01"), 100.0),
        (d("2026-07-02"), 102.0),
        (d("2026-07-03"), 104.0),
        (d("2026-07-20"), 200.0),
        (d("2026-07-21"), 202.0),
        (d("2026-07-22"), 204.0),
    ]

    result = dict(rolling_trend(points, 3))

    assert result[d("2026-07-03")] == pytest.approx(104.0)
    assert result[d("2026-07-22")] == pytest.approx(204.0)
    # Nothing spans the gap: the 20th has only itself in its window.
    assert d("2026-07-20") not in result


def test_rolling_trend_needs_three_points_to_fit() -> None:
    # Two points define a line exactly, which would just redraw the raw data.
    two = [(d("2026-07-01"), 190.0), (d("2026-07-02"), 191.0)]

    assert rolling_trend(two, 7) == []
    assert len(rolling_trend(two + [(d("2026-07-03"), 192.0)], 7)) == 1


def test_readings_on_a_single_day_fall_back_to_the_level() -> None:
    # Zero variance in x: a slope is undefined, so it must not divide by zero.
    points = [(d("2026-07-01"), 190.0)] * 3

    result = rolling_trend(points, 7)

    assert result[-1][1] == pytest.approx(190.0)


def test_a_zero_window_produces_no_trend() -> None:
    points = [(d(f"2026-07-{i:02d}"), float(i)) for i in range(1, 5)]

    assert rolling_trend(points, 0) == []


# ---------------------------------------------------------------------------
# Gap handling
# ---------------------------------------------------------------------------


def test_the_line_holds_across_short_gaps_and_breaks_across_long_ones() -> None:
    points = [
        (d("2026-07-01"), 1.0),
        (d("2026-07-04"), 2.0),  # 3-day gap: still one run
        (d("2026-07-09"), 3.0),  # 5-day gap: new run
        (d("2026-07-10"), 4.0),
    ]

    runs = split_on_gaps(points, max_gap_days=3)

    assert [len(r) for r in runs] == [2, 2]
    assert runs[0][0][0] == d("2026-07-01")
    assert runs[1][0][0] == d("2026-07-09")


def test_an_unbroken_series_is_a_single_run() -> None:
    points = [(d(f"2026-07-{i:02d}"), 1.0) for i in range(1, 6)]

    assert len(split_on_gaps(points, max_gap_days=3)) == 1


# ---------------------------------------------------------------------------
# Page rendering
# ---------------------------------------------------------------------------


def summary(values: dict[str, float], unit: str = "lb") -> dict[str, Any]:
    return {
        "metric": "weight_body_mass",
        "unit": unit,
        "aggregation": "average",
        "metric_found": bool(values),
        "series": [{"period": k, "samples": 1, "value": v} for k, v in values.items()],
    }


def embedded(html: str) -> dict[str, Any]:
    match = re.search(
        r'<script type="application/json" id="data">(.*?)</script>', html, re.S
    )
    assert match, "no embedded chart data"
    return json.loads(match.group(1))


def test_render_draws_both_series() -> None:
    html = render_chart_page(
        summary({f"2026-07-{i:02d}": 190.0 + i for i in range(1, 15)})
    )

    assert 'class="raw"' in html
    assert 'class="trend"' in html
    assert len(embedded(html)["points"]) == 14


def test_render_has_no_legend_or_series_labels() -> None:
    html = render_chart_page(
        summary({f"2026-07-{i:02d}": 190.0 + i for i in range(1, 15)})
    )

    for word in ("legend", "7-day trend</text>", "daily</text>"):
        assert word not in html
    # The tooltip still names the trend, so the value stays reachable.
    assert "-day trend" in html


def test_an_empty_series_renders_an_empty_state() -> None:
    html = render_chart_page(summary({}))

    assert "No readings in this period." in html
    assert embedded(html)["points"] == []


def test_y_axis_is_zoomed_to_the_data_not_zero_based() -> None:
    html = render_chart_page(
        summary({f"2026-07-{i:02d}": 190.0 + (i % 3) for i in range(1, 15)})
    )

    ticks = [float(t) for t in re.findall(r'class="tick"[^>]*>([\d.]+)</text>', html)]
    assert ticks, "expected y ticks"
    assert min(ticks) > 150, f"axis should hug the data, got {ticks}"


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


def make_client(tmp_path: Path) -> TestClient:
    return TestClient(
        create_app(storage_dir=tmp_path, api_token="test-token",
                   summary_today=date(2026, 7, 12))
    )


def ingest_weight(client: TestClient, readings: dict[str, float]) -> None:
    metrics = [{
        "name": "weight_body_mass",
        "units": "lb",
        "data": [{"date": f"{day} 07:00:00 -0400", "qty": qty}
                 for day, qty in readings.items()],
    }]
    response = client.post("/v1/exports", headers=HEADERS,
                           json={"data": {"metrics": metrics}})
    assert response.status_code == 201, response.text


def test_chart_endpoint_requires_a_token(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    unauthorised = client.get("/v1/health/chart",
                              params={"metric": "weight_body_mass"})
    authorised = client.get("/v1/health/chart", headers=HEADERS,
                            params={"metric": "weight_body_mass"})
    embedded_ok = client.get("/v1/health/chart",
                             params={"metric": "weight_body_mass",
                                     "embed_token": EMBED_TOKEN})

    assert unauthorised.status_code == 401
    assert authorised.status_code == 200
    assert embedded_ok.status_code == 200
    assert embedded_ok.headers["content-type"].startswith("text/html")


def test_chart_endpoint_plots_ingested_readings(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    ingest_weight(client, {f"2026-07-{i:02d}": 190.0 + i * 0.2 for i in range(1, 11)})

    html = client.get("/v1/health/chart",
                      params={"metric": "weight_body_mass",
                              "embed_token": EMBED_TOKEN}).text

    data = embedded(html)
    assert len(data["points"]) == 10
    assert data["unit"] == "lb"
    assert data["window"] == 7
    # Later points carry a trend value; the first cannot.
    assert data["points"][0]["ty"] is None
    assert data["points"][-1]["ty"] is not None


def test_window_zero_omits_the_average(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    ingest_weight(client, {f"2026-07-{i:02d}": 190.0 + i for i in range(1, 11)})

    html = client.get("/v1/health/chart",
                      params={"metric": "weight_body_mass", "window": 0,
                              "embed_token": EMBED_TOKEN}).text

    assert 'class="trend"' not in html
    assert all(p["ty"] is None for p in embedded(html)["points"])


def test_an_unknown_metric_renders_an_empty_state(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    response = client.get("/v1/health/chart",
                          params={"metric": "not_a_metric",
                                  "embed_token": EMBED_TOKEN})

    assert response.status_code == 200
    assert "No readings in this period." in response.text


def test_a_half_specified_date_range_is_rejected(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    response = client.get("/v1/health/chart",
                          params={"metric": "weight_body_mass",
                                  "start_date": "2026-07-01",
                                  "embed_token": EMBED_TOKEN})

    assert response.status_code == 422
