"""Tests for GET /v1/workouts/routes/map and the derived map token."""
import json
import re
from datetime import date
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from health_export_api.app import create_app, derive_map_token

HEADERS = {"Authorization": "Bearer test-token"}
MAP_TOKEN = derive_map_token("test-token")

CENTER_LAT = 52.5199425
CENTER_LON = 13.3999414
LON_STEP = 0.00022145

BOX = {"lat": 52.52, "lon": 13.40, "width": 500, "height": 500}


def make_client(tmp_path: Path) -> TestClient:
    return TestClient(
        create_app(storage_dir=tmp_path, api_token="test-token",
                   summary_today=date(2026, 7, 12))
    )


def ingest(client: TestClient, *, name: str = "Outdoor Walk", day: int = 10,
           workout_id: str = "walk-1") -> None:
    points = [
        {
            "latitude": CENTER_LAT,
            "longitude": CENTER_LON + i * LON_STEP,
            "timestamp": f"2026-07-{day:02d} 08:00:{i:02d} -0400",
        }
        for i in range(5)
    ]
    response = client.post("/v1/exports", headers=HEADERS, json={"data": {"workouts": [{
        "id": workout_id,
        "name": name,
        "start": f"2026-07-{day:02d} 08:00:00 -0400",
        "end": f"2026-07-{day:02d} 08:45:00 -0400",
        "duration": {"qty": 2700, "units": "s"},
        "distance": {"qty": 2.5, "units": "mi"},
        "activeEnergy": {"qty": 280, "units": "kcal"},
        "route": points,
    }]}})
    assert response.status_code == 201, response.text


def embedded(html: str) -> dict[str, Any]:
    """Pull the FeatureCollection back out of the rendered page."""
    match = re.search(
        r'<script type="application/json" id="coverage">(.*?)</script>', html, re.S
    )
    assert match, "no embedded coverage payload"
    return json.loads(match.group(1))


# ---------------------------------------------------------------------------
# Token derivation
# ---------------------------------------------------------------------------


def test_map_token_is_derived_from_the_api_token_and_is_not_it() -> None:
    token = derive_map_token("test-token")

    assert token != "test-token"
    assert len(token) == 32
    assert token == derive_map_token("test-token")  # stable
    assert token != derive_map_token("other-token")  # rotates with the source


def test_map_token_endpoint_requires_the_real_bearer(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    assert client.get("/v1/map-token").status_code == 401
    # The map token must not unlock the endpoint that reveals it.
    assert client.get(
        "/v1/map-token", headers={"Authorization": f"Bearer {MAP_TOKEN}"}
    ).status_code == 401

    response = client.get("/v1/map-token", headers=HEADERS)
    assert response.status_code == 200
    assert response.json() == {"map_token": MAP_TOKEN}


# ---------------------------------------------------------------------------
# Access to the map page
# ---------------------------------------------------------------------------


def test_map_page_accepts_the_derived_token(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    ingest(client)

    response = client.get(
        "/v1/workouts/routes/map", params={**BOX, "map_token": MAP_TOKEN}
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


def test_map_page_accepts_the_full_bearer_token(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    ingest(client)

    response = client.get("/v1/workouts/routes/map", headers=HEADERS, params=BOX)

    assert response.status_code == 200


def test_map_page_rejects_missing_and_wrong_tokens(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    assert client.get("/v1/workouts/routes/map", params=BOX).status_code == 401
    assert client.get(
        "/v1/workouts/routes/map", params={**BOX, "map_token": "nope"}
    ).status_code == 401
    assert client.get(
        "/v1/workouts/routes/map", params={**BOX, "map_token": MAP_TOKEN[:-1] + "0"}
    ).status_code == 401


def test_leaflet_is_served_same_origin(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    css = client.get("/static/leaflet.css")
    js = client.get("/static/leaflet.js")

    assert css.status_code == 200 and js.status_code == 200
    assert "leaflet" in js.text[:200].lower()


# ---------------------------------------------------------------------------
# Rendered content
# ---------------------------------------------------------------------------


def test_map_page_embeds_the_coverage_collection(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    ingest(client)

    html = client.get(
        "/v1/workouts/routes/map", params={**BOX, "map_token": MAP_TOKEN}
    ).text

    collection = embedded(html)
    assert collection["type"] == "FeatureCollection"
    assert len(collection["features"]) == 1
    assert collection["properties"]["workout_count"] == 1
    assert len(collection["bbox"]) == 4
    # Same-origin assets, no CDN.
    assert "/static/leaflet.js" in html and "/static/leaflet.css" in html
    assert "unpkg.com" not in html and "cdn.jsdelivr" not in html


def test_map_page_passes_filters_through_to_the_store(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    ingest(client)

    demanding = embedded(client.get(
        "/v1/workouts/routes/map",
        params={**BOX, "map_token": MAP_TOKEN, "min_count": 99},
    ).text)
    filtered = embedded(client.get(
        "/v1/workouts/routes/map",
        params={**BOX, "map_token": MAP_TOKEN, "workout_type": "Outdoor Run"},
    ).text)

    assert demanding["features"] == []
    assert demanding["properties"]["min_count"] == 99
    assert filtered["features"] == []


def test_map_page_renders_an_empty_area_without_failing(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    response = client.get(
        "/v1/workouts/routes/map",
        params={**BOX, "lat": 48.85, "lon": 2.35, "map_token": MAP_TOKEN},
    )

    assert response.status_code == 200
    assert embedded(response.text)["features"] == []


def test_refresh_interval_is_configurable(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    html = client.get(
        "/v1/workouts/routes/map",
        params={**BOX, "map_token": MAP_TOKEN, "refresh_minutes": 5},
    ).text

    assert "300000" in html


def test_zoom_control_is_off_by_default_and_can_be_turned_on(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    default = client.get(
        "/v1/workouts/routes/map", params={**BOX, "map_token": MAP_TOKEN}
    ).text
    enabled = client.get(
        "/v1/workouts/routes/map",
        params={**BOX, "map_token": MAP_TOKEN, "zoom_control": "true"},
    ).text

    assert "zoomControl: false" in default
    assert "zoomControl: true" in enabled


def test_attribution_is_on_by_default_and_survives_being_hidden(
    tmp_path: Path,
) -> None:
    client = make_client(tmp_path)

    default = client.get(
        "/v1/workouts/routes/map", params={**BOX, "map_token": MAP_TOKEN}
    ).text
    hidden = client.get(
        "/v1/workouts/routes/map",
        params={**BOX, "map_token": MAP_TOKEN, "attribution": "false"},
    ).text

    assert "attributionControl: true" in default
    assert "attributionControl: false" in hidden
    # OSM and CARTO require credit; it stays in the source either way.
    for html in (default, hidden):
        assert "OpenStreetMap" in html and "CARTO" in html


def test_weight_scales_with_count_unless_pinned(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    default = client.get(
        "/v1/workouts/routes/map", params={**BOX, "map_token": MAP_TOKEN}
    ).text
    pinned = client.get(
        "/v1/workouts/routes/map",
        params={**BOX, "map_token": MAP_TOKEN, "weight": 3.5},
    ).text

    assert "var fixedWeight = null;" in default
    assert "var fixedWeight = 3.5;" in pinned
    # Colour still carries frequency in both cases.
    assert "color: colour(t)" in default and "color: colour(t)" in pinned


def test_weight_must_be_a_sensible_stroke_width(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    for bad in (0, -2, 25):
        response = client.get(
            "/v1/workouts/routes/map",
            params={**BOX, "map_token": MAP_TOKEN, "weight": bad},
        )
        assert response.status_code == 422, f"weight={bad} should be rejected"


def test_a_workout_name_cannot_break_out_of_the_script_block(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    ingest(client, name="</script><script>alert(1)</script>")

    html = client.get(
        "/v1/workouts/routes/map", params={**BOX, "map_token": MAP_TOKEN}
    ).text

    # Exactly the three script tags the template itself opens.
    assert html.count("<script") == 3
    assert "</script><script>alert(1)" not in html
    # ...and the name still round-trips intact once parsed.
    assert embedded(html)["features"][0]["properties"]["workout_types"] == [
        "</script><script>alert(1)</script>"
    ]
