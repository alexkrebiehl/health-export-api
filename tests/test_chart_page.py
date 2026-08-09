"""Tests for the metric chart page and its series maths."""
import json
import re
from datetime import date, timedelta
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


def test_the_plot_fills_the_frame_without_distorting_ink() -> None:
    html = render_chart_page(
        summary({f"2026-07-{i:02d}": 190.0 + i for i in range(1, 15)})
    )

    # Stretching to fill is the point; the ink must not stretch with it.
    assert 'preserveAspectRatio="none"' in html
    assert "vector-effect:non-scaling-stroke" in html
    # Tick text sits in HTML, positioned by percentage, so it is never scaled.
    assert "<text" not in html
    assert re.search(r'class="tick ytick" style="left:max\(var\(--ygut\),[\d.]+%\);'
                     r'top:[\d.]+%"', html)


def steps_and_distance() -> list[dict[str, Any]]:
    """Two metrics whose magnitudes differ by ~1,800x, as steps and miles do."""
    steps = summary({f"2026-07-{i:02d}": 9000 + i * 200 for i in range(1, 15)},
                    unit="count")
    dist = summary({f"2026-07-{i:02d}": 4.5 + i * 0.1 for i in range(1, 15)},
                   unit="mi")
    return [steps, dist]


def test_two_metrics_get_a_panel_each_not_a_second_axis() -> None:
    """Different units cannot honestly share a y-scale.

    A dual axis can be slid until either series appears to lead, so each
    measure gets its own panel with its own scale instead.
    """
    html = render_chart_page(steps_and_distance(), series_labels=["Steps", "Distance"])

    data = embedded(html)
    assert [s["unit"] for s in data["series"]] == ["count", "mi"]
    assert [s["label"] for s in data["series"]] == ["Steps", "Distance"]

    # Two panels: two baselines, two raw lines, two trend lines, two dots.
    assert html.count('class="raw"') == 2
    assert html.count('class="trend"') == 2
    assert html.count('class="hdot"') == 2

    # Each point carries one entry per panel.
    assert all(len(p["v"]) == 2 for p in data["points"])
    first = data["points"][0]
    assert first["v"][0]["rv"] == "9,200"   # grouped
    assert first["v"][1]["rv"] == "4.6"


def test_readouts_drop_meaningless_precision_at_step_scale() -> None:
    # The real series carries fractions of a step (9162.4667). Two decimals on
    # a five-digit count is noise, and it has to match the stat tile beside it.
    steps = summary({"2026-07-01": 9162.4667, "2026-07-02": 10000.0,
                     "2026-07-03": 11111.11}, unit="count")
    weight = summary({"2026-07-01": 191.44, "2026-07-02": 190.0})
    tiny = summary({"2026-07-01": 0.0421, "2026-07-02": 0.0533}, unit="ratio")

    grouped = [p["v"][0]["rv"] for p in embedded(render_chart_page(steps))["points"]]
    assert grouped == ["9,162", "10,000", "11,111"]

    # Weight-scale numbers keep their decimal and lose a bare ".0".
    assert [p["v"][0]["rv"] for p in embedded(render_chart_page(weight))["points"]] == [
        "191.4", "190"
    ]
    # Sub-unit metrics would round away entirely at one decimal.
    assert [p["v"][0]["rv"] for p in embedded(render_chart_page(tiny))["points"]] == [
        "0.0421", "0.0533"
    ]


def ygut(html: str) -> float:
    match = re.search(r"--ygut:([\d.]+)px", html)
    assert match, "no y-gutter floor in the page"
    return float(match.group(1))


def test_the_y_gutter_grows_to_fit_the_widest_tick_label() -> None:
    """A fixed gutter clipped "15,000" to "5,000" on the live steps card.

    The gutter is a percentage of width with a pixel floor; the floor has to
    come from the labels, because a five-digit grouped tick needs half again
    the room a three-digit weight does.
    """
    weight = render_chart_page(
        summary({f"2026-07-{i:02d}": 190.0 + i for i in range(1, 15)})
    )
    steps = render_chart_page(steps_and_distance())

    # The steps panel's ticks run to "15,000" — six characters against three.
    assert ygut(steps) > ygut(weight)
    # Wide enough for the label it actually draws, at 12px tabular figures.
    assert ygut(steps) >= 6 * 7.0

    # A short label never shrinks the gutter below what the layout assumes.
    assert ygut(weight) >= 34.0


def test_a_units_override_can_blank_a_noisy_stored_unit() -> None:
    html = render_chart_page(steps_and_distance(), series_units=["", "mi"])

    assert [s["unit"] for s in embedded(html)["series"]] == ["", "mi"]
    # Omitted, the stored unit stands.
    assert embedded(render_chart_page(steps_and_distance()))["series"][0]["unit"] == (
        "count"
    )


def test_the_panels_occupy_separate_vertical_bands() -> None:
    html = render_chart_page(steps_and_distance())
    data = embedded(html)

    tops = [p["v"][0]["ry"] for p in data["points"]]
    bottoms = [p["v"][1]["ry"] for p in data["points"]]

    # Every point of the upper panel sits above every point of the lower one:
    # the bands do not overlap, which is what makes two scales readable.
    assert max(tops) < min(bottoms)


def test_a_single_metric_still_renders_one_full_height_panel() -> None:
    html = render_chart_page(
        summary({f"2026-07-{i:02d}": 190.0 + i for i in range(1, 15)})
    )

    assert html.count('class="raw"') == 1
    assert html.count('class="hdot"') == 1
    # The panel spans the full plot area, so its baseline is the frame's.
    assert 'class="axis" x1="46" y1="294.0" x2="986" y2="294.0"' in html


def test_x_ticks_are_drawn_once_for_all_panels() -> None:
    html = render_chart_page(steps_and_distance())

    labels = re.findall(r'class="tick xtick"[^>]*>([^<]*)</div>', html)
    assert labels == sorted(set(labels), key=labels.index)  # no repeats
    assert len(labels) >= 2


def test_x_ticks_suit_the_span() -> None:
    # A month of data would collapse to a single month label, so a short span
    # gets day markers instead.
    short = render_chart_page(
        summary({f"2026-07-{i:02d}": 190.0 + i for i in range(1, 15)})
    )
    assert re.search(r'class="tick xtick"[^>]*>\d+ Jul</div>', short)

    long_span = summary(
        {(date(2026, 4, 20) + timedelta(days=i)).isoformat(): 190.0 + (i % 4)
         for i in range(84)}
    )
    assert re.search(r'class="tick xtick"[^>]*>May</div>',
                     render_chart_page(long_span))


def test_y_ticks_are_comma_grouped_when_large() -> None:
    html = render_chart_page(
        summary({f"2026-07-{i:02d}": 9000 + i * 400 for i in range(1, 15)},
                unit="count")
    )

    ticks = re.findall(r'class="tick ytick"[^>]*>([\d,.]+)</div>', html)
    assert any("," in t for t in ticks), ticks


def test_the_tile_can_never_scroll() -> None:
    html = render_chart_page(
        summary({f"2026-07-{i:02d}": 190.0 + i for i in range(1, 15)})
    )

    assert "overflow:hidden" in html


def test_the_tooltip_is_clamped_rather_than_centred() -> None:
    """A centring transform put half the box past the right edge.

    At the last reading (x ~ 986/1000) that overflowed the page and raised a
    horizontal scrollbar, so the placement is computed and clamped in JS
    instead. Asserted so the transform cannot quietly come back.
    """
    html = render_chart_page(
        summary({f"2026-07-{i:02d}": 190.0 + i for i in range(1, 15)})
    )

    tip_rule = re.search(r"#tip\{[^}]*\}", html, re.S).group(0)
    assert "translate(" not in tip_rule

    # Clamped horizontally against the frame, and flipped below the point when
    # there is no room above it.
    assert "Math.min(Math.max(px - tw / 2" in html
    assert "frame.width - tw" in html
    assert "no room above" in html


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

    ticks = [float(t)
             for t in re.findall(r'class="tick ytick"[^>]*>([\d.]+)</div>', html)]
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

    unauthorised = client.get("/v1/render/chart",
                              params={"metric": "weight_body_mass"})
    authorised = client.get("/v1/render/chart", headers=HEADERS,
                            params={"metric": "weight_body_mass"})
    embedded_ok = client.get("/v1/render/chart",
                             params={"metric": "weight_body_mass",
                                     "embed_token": EMBED_TOKEN})

    assert unauthorised.status_code == 401
    assert authorised.status_code == 200
    assert embedded_ok.status_code == 200
    assert embedded_ok.headers["content-type"].startswith("text/html")


def test_chart_endpoint_plots_ingested_readings(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    ingest_weight(client, {f"2026-07-{i:02d}": 190.0 + i * 0.2 for i in range(1, 11)})

    html = client.get("/v1/render/chart",
                      params={"metric": "weight_body_mass",
                              "embed_token": EMBED_TOKEN}).text

    data = embedded(html)
    assert len(data["points"]) == 10
    assert data["series"][0]["unit"] == "lb"
    assert data["window"] == 7
    # Later points carry a trend value; the first cannot. `v` holds one entry
    # per panel, so a single-metric chart reads index 0.
    assert data["points"][0]["v"][0]["ty"] is None
    assert data["points"][-1]["v"][0]["ty"] is not None


def test_window_zero_omits_the_average(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    ingest_weight(client, {f"2026-07-{i:02d}": 190.0 + i for i in range(1, 11)})

    html = client.get("/v1/render/chart",
                      params={"metric": "weight_body_mass", "window": 0,
                              "embed_token": EMBED_TOKEN}).text

    assert 'class="trend"' not in html
    assert all(p["v"][0]["ty"] is None for p in embedded(html)["points"])


def test_an_unknown_metric_renders_an_empty_state(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    response = client.get("/v1/render/chart",
                          params={"metric": "not_a_metric",
                                  "embed_token": EMBED_TOKEN})

    assert response.status_code == 200
    assert "No readings in this period." in response.text


def test_a_half_specified_date_range_is_rejected(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    response = client.get("/v1/render/chart",
                          params={"metric": "weight_body_mass",
                                  "start_date": "2026-07-01",
                                  "embed_token": EMBED_TOKEN})

    assert response.status_code == 422
