"""The render layer must not reach into the data layer.

This is the property that makes the split meaningful rather than cosmetic: if
the rendering ever moves to its own service, only the provider is replaced.
Convention alone would erode — one `from ... import Store` in a hurry and the
boundary is gone with nothing to notice — so it is asserted.
"""
import ast
import pathlib
from datetime import date
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from health_export_api.app import create_app, derive_embed_token

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "health_export_api"

# Modules that own or touch persistence. The render side may not import these.
DATA_LAYER = {"store", "geo", "provider_impl", "normalization", "workout_normalization"}

# Modules that make up the rendering layer.
RENDER_MODULES = ["map_page.py", "chart_page.py", "stat_page.py", "theme.py",
                  "routers/render.py"]

HEADERS = {"Authorization": "Bearer test-token"}
EMBED_TOKEN = derive_embed_token("test-token")


def imported_modules(path: pathlib.Path) -> set[str]:
    """Every `health_export_api.*` module a file imports."""
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
            "health_export_api"
        ):
            found.add((node.module or "").removeprefix("health_export_api."))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("health_export_api"):
                    found.add(alias.name.removeprefix("health_export_api."))
    return found


def test_render_modules_do_not_import_the_data_layer() -> None:
    offenders = {}
    for name in RENDER_MODULES:
        path = SRC / name
        assert path.exists(), f"{name} moved; update this test"
        leaked = imported_modules(path) & DATA_LAYER
        if leaked:
            offenders[name] = sorted(leaked)

    assert offenders == {}, (
        f"render modules reached into the data layer: {offenders}. "
        "Route the access through DataProvider instead."
    )


def test_render_router_never_names_the_store() -> None:
    # The provider is the only door. Catches `Store` arriving by any route,
    # including a type annotation or a local import.
    source = (SRC / "routers" / "render.py").read_text()

    assert "Store" not in source
    assert "DataProvider" in source


def test_the_page_modules_are_pure_templating() -> None:
    # No I/O of any kind in the three page renderers — that is what keeps them
    # trivially testable and portable to another process.
    for name in ("map_page.py", "chart_page.py", "stat_page.py"):
        imports = imported_modules(SRC / name)
        # `page_shell` owns the head, palette and reload script every page
        # repeats; it is framework-free for exactly this reason.
        assert imports <= {"theme", "page_shell"}, f"{name} imports {imports}"


# ---------------------------------------------------------------------------
# The seam's contract
# ---------------------------------------------------------------------------


def make_client(tmp_path: Path) -> TestClient:
    return TestClient(
        create_app(storage_dir=tmp_path, api_token="test-token",
                   summary_today=date(2026, 7, 12))
    )


def ingest_walk(client: TestClient) -> None:
    lat, lon, step = 52.5199425, 13.3999414, 0.00022145
    response = client.post("/v1/exports", headers=HEADERS, json={"data": {"workouts": [{
        "id": "w1", "name": "Outdoor Walk",
        "start": "2026-07-10 08:00:00 -0400", "end": "2026-07-10 08:45:00 -0400",
        "duration": {"qty": 2700, "units": "s"},
        "distance": {"qty": 2.5, "units": "mi"},
        "activeEnergy": {"qty": 280, "units": "kcal"},
        "route": [{"latitude": lat, "longitude": lon + i * step,
                   "timestamp": f"2026-07-10 08:00:{i:02d} -0400"} for i in range(6)],
    }]}})
    assert response.status_code == 201, response.text


def test_the_map_renders_exactly_what_the_geojson_endpoint_serves(
    tmp_path: Path,
) -> None:
    """Pins the contract a process split would depend on.

    The render route and the data route must agree on the payload, because a
    remote renderer would fetch the latter and expect what the former used.
    """
    client = make_client(tmp_path)
    ingest_walk(client)
    box: dict[str, Any] = {"lat": 52.52, "lon": 13.40, "width": 500, "height": 500}

    served = client.get("/v1/workouts/routes/geojson", headers=HEADERS,
                        params=box).json()
    html = client.get("/v1/render/map",
                      params={**box, "embed_token": EMBED_TOKEN}).text

    import json
    import re
    embedded = json.loads(
        re.search(r'id="coverage">(.*?)</script>', html, re.S).group(1)
    )
    assert embedded == served


def test_the_old_render_paths_are_gone(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    for path in ("/v1/workouts/routes/map", "/v1/health/chart", "/v1/health/stat"):
        response = client.get(path, params={"metric": "weight_body_mass",
                                            "embed_token": EMBED_TOKEN,
                                            "lat": 52.52, "lon": 13.40,
                                            "width": 500, "height": 500})
        assert response.status_code == 404, f"{path} still resolves"
