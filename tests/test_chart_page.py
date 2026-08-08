"""Tests for the metric chart page and its series maths."""
import json
import re
from datetime import date
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from health_export_api.app import create_app, derive_embed_token
from health_export_api.chart_page import (
    moving_average,
    render_chart_page,
    split_on_gaps,
)

HEADERS = {"Authorization": "Bearer test-token"}
EMBED_TOKEN = derive_embed_token("test-token")


def d(day: str) -> date:
    return date.fromisoformat(day)


# ---------------------------------------------------------------------------
# Moving average
# ---------------------------------------------------------------------------


def test_moving_average_is_a_trailing_mean() -> None:
    points = [(d(f"2026-07-{i:02d}"), float(i)) for i in range(1, 6)]

    result = moving_average(points, 3)

    # Needs two samples before it emits, then trails three days.
    assert result == [
        (d("2026-07-02"), 1.5),
        (d("2026-07-03"), 2.0),
        (d("2026-07-04"), 3.0),
        (d("2026-07-05"), 4.0),
    ]


def test_moving_average_window_is_calendar_days_not_samples() -> None:
    # A 5-day hole: the window must not reach back across it and average
    # readings a fortnight apart.
    points = [
        (d("2026-07-01"), 100.0),
        (d("2026-07-02"), 102.0),
        (d("2026-07-10"), 200.0),
        (d("2026-07-11"), 202.0),
    ]

    result = dict(moving_average(points, 3))

    assert result[d("2026-07-02")] == 101.0
    assert result[d("2026-07-11")] == 201.0  # not blended with the July 1-2 pair
    assert d("2026-07-10") not in result  # alone in its window


def test_moving_average_needs_two_samples_before_emitting() -> None:
    assert moving_average([(d("2026-07-01"), 190.0)], 7) == []


def test_a_zero_window_produces_no_average() -> None:
    points = [(d("2026-07-01"), 1.0), (d("2026-07-02"), 2.0)]

    assert moving_average(points, 0) == []


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

    for word in ("legend", "7-day avg</text>", "daily</text>"):
        assert word not in html
    # The tooltip still names the average, so the value stays reachable.
    assert "-day avg" in html


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
