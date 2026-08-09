"""Options that every render endpoint shares, and the shell that applies them.

The point of the abstraction is that a new endpoint declares one dependency
rather than copying three params and a `<head>`. These tests pin that: the same
options reach all three endpoints and mean the same thing on each.
"""
import re
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from health_export_api.app import create_app, derive_embed_token
from health_export_api.page_shell import MAX_MARGIN, PageOptions, render_page
from health_export_api.routers.options import page_options, timeframe

EMBED_TOKEN = derive_embed_token("test-token")
HEADERS = {"Authorization": "Bearer test-token"}
BOX = {"lat": 52.52, "lon": 13.40, "width": 500, "height": 500}


def make_client(tmp_path: Path) -> TestClient:
    return TestClient(
        create_app(storage_dir=tmp_path, api_token="test-token",
                   summary_today=date(2026, 7, 12))
    )


def seed(client: TestClient) -> None:
    lat, lon, step = 52.5199425, 13.3999414, 0.00022145
    client.post("/v1/exports", headers=HEADERS, json={"data": {
        "metrics": [{"name": "weight_body_mass", "units": "lb", "data": [
            {"date": f"2026-07-{i:02d}T07:00:00-04:00", "qty": 190.0 + i}
            for i in range(1, 12)]}],
        "workouts": [{
            "id": "w1", "name": "Outdoor Walk",
            "start": "2026-07-10 08:00:00 -0400", "end": "2026-07-10 08:45:00 -0400",
            "duration": {"qty": 2700, "units": "s"},
            "distance": {"qty": 2.5, "units": "mi"},
            "activeEnergy": {"qty": 280, "units": "kcal"},
            "route": [{"latitude": lat, "longitude": lon + i * step,
                       "timestamp": f"2026-07-10 08:00:{i:02d} -0400"}
                      for i in range(6)]}],
    }})


def pages(client: TestClient, **options: object) -> dict[str, str]:
    """The same options put through all three render endpoints."""
    common = {"embed_token": EMBED_TOKEN, **options}
    return {
        "map": client.get("/v1/render/map", params={**BOX, **common}).text,
        "chart": client.get("/v1/render/chart",
                            params={"metric": "weight_body_mass", **common}).text,
        "stat": client.get("/v1/render/stat",
                           params={"metric": "weight_body_mass", **common}).text,
    }


def test_every_render_endpoint_takes_the_shared_options(tmp_path: Path) -> None:
    """The whole point: one set of params, three endpoints, same meaning.

    Before this, `refresh_minutes` was written out three times identically
    while `margin` and `title` existed on exactly one endpoint each — for no
    reason beyond which card happened to need them first.
    """
    client = make_client(tmp_path)
    seed(client)

    rendered = pages(client, title="Custom", refresh_minutes=5, margin=6, theme="dark")

    for name, html in rendered.items():
        assert "<title>Custom</title>" in html, f"{name} ignored title"
        assert "}, 300000);" in html, f"{name} ignored refresh_minutes"
        assert re.search(r"#page\{[^}]*padding:6(\.0)?%", html), f"{name} ignored margin"
        assert 'data-theme="dark"' in html, f"{name} ignored theme"


def test_the_defaults_leave_every_page_as_it_was(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    seed(client)

    for name, html in pages(client).items():
        # No stamp means "follow the viewer", which is the common case. Checked
        # on the tag alone: the palette's own [data-theme] scopes are always in
        # the stylesheet, waiting for a stamp that may never come.
        tag = re.search(r"<html[^>]*>", html).group(0)
        assert "data-theme" not in tag, f"{name} stamped a theme it was not given"
        assert re.search(r"#page\{[^}]*padding:0(\.0)?%", html), f"{name} padded"
        assert "}, 1800000);" in html, f"{name} lost the 30-minute default"


def test_a_theme_stamp_drives_the_palette_and_the_basemap(tmp_path: Path) -> None:
    """`theme` has to reach the map's tiles, not just its page background.

    The palette already carried `[data-theme]` blocks that nothing set; the map
    picked its CARTO layer from prefers-color-scheme alone, so an override
    would have left a light page behind dark tiles.
    """
    client = make_client(tmp_path)
    seed(client)

    light = pages(client, theme="light")
    assert 'data-theme="light"' in light["map"]
    # The palette's override scopes are what the stamp keys into.
    assert '[data-theme="dark"]' in light["map"]
    # And the basemap consults the stamp before falling back to the media query.
    assert "getAttribute('data-theme')" in light["map"]
    assert "prefers-color-scheme: dark" in light["map"]


def test_the_map_uses_the_shared_palette_rather_than_its_own(tmp_path: Path) -> None:
    # It was the one card that hardcoded a slate background and so never
    # followed the viewer's setting, however the others were configured.
    client = make_client(tmp_path)
    seed(client)

    html = pages(client)["map"]

    assert "#0b0e14" not in html
    assert "var(--surface)" in html


def test_an_out_of_range_option_is_rejected_not_clamped(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    seed(client)

    for bad in ({"margin": -1}, {"margin": MAX_MARGIN + 1},
                {"theme": "sepia"}, {"refresh_minutes": 0}):
        response = client.get("/v1/render/stat",
                              params={"metric": "weight_body_mass",
                                      "embed_token": EMBED_TOKEN, **bad})
        assert response.status_code == 422, f"{bad} was accepted"


def test_the_dependencies_build_the_dataclasses_a_page_expects() -> None:
    # A future endpoint declares these two and gets the whole set; the shell
    # takes plain dataclasses so page modules never import FastAPI.
    options = page_options(title="T", refresh_minutes=9, margin=3.0, theme="dark")
    span = timeframe(date_range="last 7 days", start_date=None, end_date=None)

    assert options == PageOptions(title="T", refresh_minutes=9, margin=3.0,
                                  theme="dark")
    assert span.date_range == "last 7 days"


def test_an_endpoint_title_is_a_default_the_caller_can_override() -> None:
    supplied = PageOptions(title="Mine")
    empty = PageOptions()

    assert supplied.with_title("Derived").title == "Mine"
    assert empty.with_title("Derived").title == "Derived"
    # And nothing else is disturbed by filling the default in.
    assert empty.with_title("Derived").refresh_minutes == empty.refresh_minutes


def test_the_shell_clamps_a_margin_that_would_consume_the_frame() -> None:
    html = render_page(body="x", style="", options=PageOptions(margin=999))

    assert float(re.search(r"padding:([\d.]+)%", html).group(1)) == MAX_MARGIN


@pytest.mark.parametrize("theme,expected", [("auto", False), ("light", True),
                                            ("dark", True)])
def test_only_an_explicit_theme_is_stamped(theme: str, expected: bool) -> None:
    html = render_page(body="x", style="", options=PageOptions(theme=theme))  # type: ignore[arg-type]

    assert ("data-theme" in re.search(r"<html[^>]*>", html).group(0)) is expected
