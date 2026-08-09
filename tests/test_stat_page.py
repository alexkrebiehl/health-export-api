"""Tests for the stat tiles and their window maths."""
import re
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from health_export_api.app import create_app, derive_embed_token
from health_export_api.chart_page import (
    latest_reading,
    window_balance,
    window_change,
    zero_fill_today,
)
from health_export_api.stat_page import (
    render_balance_tile,
    render_change_tile,
    render_latest_tile,
)

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


def _value_sizes(html: str) -> tuple[float, float]:
    """The value rule's (height, width) budgets.

    Anchored on `.value{` — the `.label` rule has the same shape and comes
    first in the stylesheet, so an unanchored search reads the wrong one.
    """
    m = re.search(r"\.value\{[^}]*font-size:min\(([\d.]+)cqh,([\d.]+)cqw\)",
                  html, re.S)
    return float(m.group(1)), float(m.group(2))


def _budget(html: str) -> float:
    return _value_sizes(html)[1]


def _padding(html: str) -> tuple[float, float]:
    m = re.search(r"padding:([\d.]+)cqh ([\d.]+)cqw", html)
    return float(m.group(1)), float(m.group(2))


def test_margin_defaults_to_no_change() -> None:
    plain = render_latest_tile((d("2026-07-12"), 191.4), unit="lb", today=TODAY)
    zero = render_latest_tile((d("2026-07-12"), 191.4), unit="lb", today=TODAY,
                              margin=0)

    assert plain == zero
    assert _padding(plain) == (3.0, 4.0)


def test_a_margin_widens_padding_and_narrows_the_budget() -> None:
    """The coupling: padding and the text budget must move together.

    Growing the padding without shrinking the budget would push the text
    straight out of the tile, which is the whole hazard of this parameter.
    """
    tight = render_latest_tile((d("2026-07-12"), 191.4), unit="lb", today=TODAY)
    roomy = render_latest_tile((d("2026-07-12"), 191.4), unit="lb", today=TODAY,
                               margin=8)

    assert _padding(roomy) == (11.0, 12.0)
    assert _budget(roomy) < _budget(tight)

    # And the height budget shrinks too, so the column still fits vertically.
    assert _value_sizes(roomy)[0] == _value_sizes(tight)[0] - 16.0


def test_margin_is_clamped_so_the_budget_stays_positive() -> None:
    html = render_latest_tile((d("2026-07-12"), 191.4), unit="lb", today=TODAY,
                              margin=999)

    assert _budget(html) > 0
    assert _padding(html) == (23.0, 24.0)


def test_alignment_defaults_to_left_and_can_be_centred() -> None:
    left = render_latest_tile((d("2026-07-12"), 191.4), today=TODAY)
    centre = render_latest_tile((d("2026-07-12"), 191.4), today=TODAY,
                                align="center")
    right = render_latest_tile((d("2026-07-12"), 191.4), today=TODAY,
                               align="right")

    assert "align-items:flex-start;text-align:left" in left
    assert "align-items:center;text-align:center" in centre
    assert "align-items:flex-end;text-align:right" in right


def test_margin_and_alignment_reach_the_change_tile_too() -> None:
    html = render_change_tile((191.0, 193.0, -2.0), unit="lb", margin=6,
                              align="center")

    assert _padding(html) == (9.0, 10.0)
    assert "text-align:center" in html


def test_tiles_render_an_empty_state_rather_than_failing() -> None:
    assert "No readings yet" in render_latest_tile(None)
    assert "Not enough readings" in render_change_tile(None, window_days=7)


def test_large_values_are_grouped_and_lose_the_decimal() -> None:
    # A step count is five digits: "9605.0" is both noisy and hard to scan.
    assert "9,605" in render_latest_tile((d("2026-07-12"), 9605.0), today=TODAY)
    assert "9605" not in render_latest_tile((d("2026-07-12"), 9605.0), today=TODAY)
    # Weight sits below the threshold and keeps its decimal.
    assert "191.4" in render_latest_tile((d("2026-07-12"), 191.4), today=TODAY)


def test_a_counted_thing_never_shows_a_decimal() -> None:
    """"373.8 steps" appeared on the dashboard early one morning.

    Grouping above 1,000 already hid the decimal on a normal day's total, so
    this only surfaced once a partial day fell under the threshold.
    """
    assert "374" in render_latest_tile((d("2026-07-12"), 373.8), today=TODAY,
                                       integral=True)
    assert "373.8" not in render_latest_tile((d("2026-07-12"), 373.8),
                                             today=TODAY, integral=True)
    # And on the change tile, which shares the formatter.
    assert "12" in render_change_tile((9600.0, 9588.4, 11.6), integral=True)
    assert "11.6" not in render_change_tile((9600.0, 9588.4, 11.6), integral=True)
    # A measured quantity is untouched.
    assert "191.4" in render_latest_tile((d("2026-07-12"), 191.4), today=TODAY)


def test_whole_numbers_drop_the_trailing_decimal() -> None:
    assert "2 lb" in render_change_tile(
        (191.0, 193.0, -2.0), unit=" lb"
    ).replace("<span class=\"unit\">", "").replace("</span>", "")


# ---------------------------------------------------------------------------
# Energy balance
# ---------------------------------------------------------------------------


def days(values: dict[str, float]) -> list[tuple[date, float]]:
    return [(d(k), v) for k, v in values.items()]


def test_window_balance_is_intake_minus_everything_spent() -> None:
    eaten = days({"2026-07-10": 2000.0, "2026-07-11": 1800.0, "2026-07-12": 1900.0})
    resting = days({"2026-07-10": 2100.0, "2026-07-11": 2100.0, "2026-07-12": 2100.0})
    active = days({"2026-07-10": 900.0, "2026-07-11": 1000.0, "2026-07-12": 800.0})

    net, counted = window_balance(eaten, [resting, active], 7, TODAY)

    assert counted == 3
    assert net == pytest.approx(5700 - (6300 + 2700))


def test_a_day_with_no_intake_logged_is_left_out_entirely() -> None:
    """The bug this guards against reads as a deficit but is a missing meal.

    Burn is recorded continuously by the watch, so at 9am there is a partial
    day of spend against nothing eaten yet. Counting it would report several
    hundred calories of deficit that are really an unlogged breakfast.
    """
    eaten = days({"2026-07-10": 2000.0, "2026-07-11": 1800.0})
    # Today has burn but no intake — the watch has been running since midnight.
    burn = days({"2026-07-10": 3000.0, "2026-07-11": 3000.0, "2026-07-12": 700.0})

    net, counted = window_balance(eaten, [burn], 7, TODAY)

    assert counted == 2, "today has no intake, so it is not a day of data"
    assert net == pytest.approx(3800 - 6000)


def test_window_balance_is_none_when_nothing_was_logged() -> None:
    assert window_balance([], [days({"2026-07-12": 700.0})], 7, TODAY) is None


def test_window_balance_ignores_days_outside_the_window() -> None:
    eaten = days({"2026-07-05": 9999.0, "2026-07-12": 2000.0})
    burn = days({"2026-07-05": 1.0, "2026-07-12": 3000.0})

    net, counted = window_balance(eaten, [burn], 3, TODAY)

    assert counted == 1
    assert net == pytest.approx(2000 - 3000)


def test_today_reads_as_zero_when_nothing_has_been_logged_yet() -> None:
    """A day in progress with nothing recorded has eaten nothing *so far*.

    Reporting yesterday's total under a "Today" label is the wrong answer; zero
    is a number and it is the right one.
    """
    logged = days({"2026-07-10": 2000.0, "2026-07-11": 1800.0})

    filled = zero_fill_today(logged, TODAY)

    assert filled[-1] == (TODAY, 0.0)
    assert latest_reading(filled) == (TODAY, 0.0)
    # And it leaves a day that *does* have a reading alone.
    already = days({"2026-07-11": 1800.0, "2026-07-12": 900.0})
    assert zero_fill_today(already, TODAY) == already


def test_a_past_day_with_nothing_logged_is_not_called_zero() -> None:
    """The half of the rule that has to stay a gap.

    A finished day with no intake is a missing log, not a fast — two such days
    in the last sixty, against a lowest real day of 1,317 kcal. Calling them
    zero would invent a 2,200 kcal deficit each.
    """
    gap = days({"2026-07-08": 2000.0, "2026-07-10": 1800.0})  # the 9th missing

    filled = zero_fill_today(gap, TODAY)

    assert d("2026-07-09") not in [day for day, _ in filled]
    # The balance ignores it too, rather than scoring it as a fasting day.
    burn = days({"2026-07-08": 3000.0, "2026-07-09": 3000.0, "2026-07-10": 3000.0})
    net, counted = window_balance(gap, [burn], 7, TODAY)
    assert counted == 2
    assert net == pytest.approx(3800 - 6000)


def test_the_balance_counts_today_once_it_has_been_zero_filled() -> None:
    eaten = days({"2026-07-11": 1800.0})
    burn = days({"2026-07-11": 3000.0, "2026-07-12": 700.0})

    without = window_balance(eaten, [burn], 7, TODAY)
    with_today = window_balance(zero_fill_today(eaten, TODAY), [burn], 7, TODAY)

    assert without == (pytest.approx(-1200.0), 1)
    # Today's partial burn now counts against nothing eaten yet.
    assert with_today == (pytest.approx(-1900.0), 2)


def test_the_balance_tile_colours_both_directions_but_never_alone() -> None:
    deficit = render_balance_tile((-8351.0, 7), unit="kcal")
    surplus = render_balance_tile((1200.0, 7), unit="kcal")

    assert 'class="value good"' in deficit and "↓" in deficit and "deficit" in deficit
    assert 'class="value bad"' in surplus and "↑" in surplus and "surplus" in surplus
    # The word and the arrow carry it without colour, which is the requirement
    # the change tile meets by staying neutral and this one meets by saying so.
    assert "8,351" in deficit and "1,200" in surplus


def test_the_balance_tile_says_when_the_window_came_up_short() -> None:
    # Three logged days out of seven is a different claim from a full week.
    short = render_balance_tile((-3000.0, 3), window_days=7)

    assert "over 3 days of 7" in short
    assert "of 7" not in render_balance_tile((-3000.0, 7), window_days=7)


def test_a_one_day_balance_describes_today_rather_than_counting_days() -> None:
    # "over 1 day" is a clumsy way to say "today", and the day is still being
    # lived — the burn is partial and so is whatever has been eaten.
    today = render_balance_tile((-757.0, 1), unit="kcal", window_days=1)

    assert "so far today" in today
    assert "over 1 day" not in today
    assert 'class="value good"' in today and "757" in today

    # A surplus by the evening reads the other way, still with the word.
    over = render_balance_tile((320.0, 1), unit="kcal", window_days=1)
    assert 'class="value bad"' in over and "surplus" in over


def test_the_balance_tile_has_an_empty_state() -> None:
    assert "No days with intake logged" in render_balance_tile(None)


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
    return client.get("/v1/render/stat",
                      params={"metric": "weight_body_mass",
                              "embed_token": EMBED_TOKEN, **params})


def test_stat_endpoint_requires_a_token(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    assert client.get("/v1/render/stat",
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

    response = client.get("/v1/render/stat",
                          params={"metric": "nope", "embed_token": EMBED_TOKEN})

    assert response.status_code == 200
    assert "No readings yet" in response.text


def test_bad_parameters_are_rejected(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    assert stat(client, stat="sideways").status_code == 422
    assert stat(client, good_direction="left").status_code == 422
    assert stat(client, window=0).status_code == 422
    assert stat(client, align="middle").status_code == 422
    assert stat(client, margin=-1).status_code == 422
    assert stat(client, margin=50).status_code == 422


def test_an_explicit_unit_overrides_the_stored_one(tmp_path: Path) -> None:
    # step_count's stored unit is the literal string "count", which reads as
    # noise beside the number, so an empty `unit=` has to be able to drop it.
    client = make_client(tmp_path)
    ingest_weight(client, {"2026-07-12": 191.4})

    blanked = stat(client, unit="").text
    overridden = stat(client, unit="kg").text

    assert "191.4" in blanked and ">lb<" not in blanked
    assert "191.4" in overridden and ">kg<" in overridden
    # Omitting the param still falls back to what the store recorded.
    assert ">lb<" in stat(client).text


def test_the_endpoint_reads_integral_from_the_stored_unit(tmp_path: Path) -> None:
    # The decision comes from what the store recorded, not from what is shown:
    # the steps card blanks the unit with `unit=`, and must still round.
    client = make_client(tmp_path)
    response = client.post("/v1/exports", headers=HEADERS, json={"data": {"metrics": [{
        "name": "step_count", "units": "count",
        "data": [{"date": "2026-07-12T09:00:00-04:00", "qty": 373.8}],
    }]}})
    assert response.status_code == 201, response.text

    html = client.get("/v1/render/stat",
                      params={"metric": "step_count", "unit": "",
                              "embed_token": EMBED_TOKEN}).text

    assert "374" in html and "373.8" not in html


def test_the_balance_endpoint_subtracts_every_minus_metric(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    payload = {"data": {"metrics": [
        {"name": "dietary_energy", "units": "kcal", "data": [
            {"date": "2026-07-11T12:00:00-04:00", "qty": 2000.0},
            {"date": "2026-07-12T12:00:00-04:00", "qty": 1800.0}]},
        {"name": "basal_energy_burned", "units": "kcal", "data": [
            {"date": "2026-07-11T12:00:00-04:00", "qty": 2100.0},
            {"date": "2026-07-12T12:00:00-04:00", "qty": 2100.0}]},
        {"name": "active_energy", "units": "kcal", "data": [
            {"date": "2026-07-11T12:00:00-04:00", "qty": 900.0},
            {"date": "2026-07-12T12:00:00-04:00", "qty": 800.0}]},
    ]}}
    assert client.post("/v1/exports", headers=HEADERS, json=payload).status_code == 201

    html = client.get("/v1/render/stat", params=[
        ("metric", "dietary_energy"), ("stat", "balance"),
        ("minus", "basal_energy_burned"), ("minus", "active_energy"),
        ("window", 7), ("embed_token", EMBED_TOKEN)]).text

    # 3,800 eaten against 5,900 burned.
    assert "2,100" in html
    assert 'class="value good"' in html and "deficit" in html
    assert "over 2 days of 7" in html


def test_zero_fill_reaches_the_endpoint_for_totals_but_not_for_levels(
    tmp_path: Path,
) -> None:
    """Summed metrics only. You do not weigh nothing because you skipped the
    scale, so the weight tile must keep falling back to its last reading."""
    client = make_client(tmp_path)
    assert client.post("/v1/exports", headers=HEADERS, json={"data": {"metrics": [
        {"name": "dietary_energy", "units": "kcal",
         "data": [{"date": "2026-07-11T12:00:00-04:00", "qty": 1800.0}]},
        {"name": "weight_body_mass", "units": "lb",
         "data": [{"date": "2026-07-11T07:00:00-04:00", "qty": 191.4}]},
    ]}}).status_code == 201

    eaten = client.get("/v1/render/stat", params={
        "metric": "dietary_energy", "embed_token": EMBED_TOKEN}).text
    weight = client.get("/v1/render/stat", params={
        "metric": "weight_body_mass", "embed_token": EMBED_TOKEN}).text

    # Nothing logged today: zero so far, dated today.
    assert ">0<" in eaten.replace('<span class="unit">kcal</span>', "")
    assert "Today · 12 Jul" in eaten
    # A level keeps its last reading and says how stale it is.
    assert "191.4" in weight and "Yesterday · 11 Jul" in weight


def test_margin_and_align_reach_the_endpoint(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    ingest_weight(client, {"2026-07-12": 191.4})

    html = stat(client, margin=10, align="center").text

    assert "padding:13.0cqh 14.0cqw" in html
    assert "align-items:center;text-align:center" in html
