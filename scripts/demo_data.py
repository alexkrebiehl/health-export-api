#!/usr/bin/env python3
"""Seed and serve an instance full of synthetic health data.

For screenshots, for demos, and for working on the render endpoints without a
real export archive. The data is invented; none of it describes anyone.

Everything is deterministic — one RNG seed and a fixed ``SAMPLE_TODAY`` — so
the cards render identically on every run and a committed screenshot does not
drift. Nothing here reads the real clock.

    uv run python scripts/demo_data.py --serve
    uv run python scripts/demo_data.py --urls

Seeding goes through ``POST /v1/exports`` rather than writing to SQLite
directly, so the generated payloads take the same path a real export does and
exercise normalization on the way in.
"""

from __future__ import annotations

import argparse
import math
import random
import shutil
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from health_export_api.app import create_app, derive_embed_token  # noqa: E402

# A fixed "now". The tiles say "Today · 15 Jun" whenever this is run, which is
# what keeps a committed screenshot from going stale the next day.
SAMPLE_TODAY = date(2026, 6, 15)
DAYS = 90
SEED = 20260615

TOKEN = "demo-token"
OFFSET = "-0500"  # America/Chicago in June, matching the sample map

# ---------------------------------------------------------------------------
# The map
# ---------------------------------------------------------------------------

# A residential block on Chicago's North West Side. Chosen because Chicago's
# street grid is aligned almost exactly to true north: walking a constant
# latitude follows an east–west street and a constant longitude follows a
# north–south one, so a lattice of synthetic routes lands on real roads instead
# of cutting through buildings. Nobody's home, in every sense.
MAP_LAT, MAP_LON = 41.9435, -87.7043

# Chicago blocks run 1/8 mile (~201 m) between numbered streets, with north–south
# streets roughly half that apart on this grid. Metres per degree at this
# latitude; longitude is compressed by cos(lat).
_M_PER_DEG_LAT = 111_132.0
_M_PER_DEG_LON = 111_320.0 * math.cos(math.radians(MAP_LAT))
BLOCK_NS_M = 201.0   # spacing between east–west streets
BLOCK_EW_M = 201.0   # spacing between north–south streets
GRID_ROWS, GRID_COLS = 7, 7

# Roughly one GPS sample every few seconds of walking.
STEP_M = 12.0


def _lat(row: int) -> float:
    return MAP_LAT + (row - GRID_ROWS // 2) * BLOCK_NS_M / _M_PER_DEG_LAT


def _lon(col: int) -> float:
    return MAP_LON + (col - GRID_COLS // 2) * BLOCK_EW_M / _M_PER_DEG_LON


def _leg(start: tuple[float, float], end: tuple[float, float]) -> list[tuple[float, float]]:
    """Points along one block, dense enough to look like a recorded track."""
    lat_m = (end[0] - start[0]) * _M_PER_DEG_LAT
    lon_m = (end[1] - start[1]) * _M_PER_DEG_LON
    steps = max(2, int(math.hypot(lat_m, lon_m) / STEP_M))
    return [
        (start[0] + (end[0] - start[0]) * i / steps,
         start[1] + (end[1] - start[1]) * i / steps)
        for i in range(steps + 1)
    ]


def _walk(rng: random.Random, blocks: int) -> list[tuple[float, float]]:
    """An out-and-back walk along grid edges, starting from the same corner.

    A shared start plus random turns is what gives the coverage map something
    to colour: the streets nearest home get walked on most trips, so traversal
    counts fall off with distance the way they do in real data.
    """
    row, col = GRID_ROWS // 2, GRID_COLS // 2
    out: list[tuple[int, int]] = [(row, col)]
    for _ in range(blocks):
        moves = []
        if row > 0:
            moves.append((-1, 0))
        if row < GRID_ROWS - 1:
            moves.append((1, 0))
        if col > 0:
            moves.append((0, -1))
        if col < GRID_COLS - 1:
            moves.append((0, 1))
        d_row, d_col = rng.choice(moves)
        row, col = row + d_row, col + d_col
        out.append((row, col))
    # Retrace the way back, as a walk from home usually does.
    nodes = out + out[-2::-1]

    points: list[tuple[float, float]] = []
    for (r1, c1), (r2, c2) in zip(nodes, nodes[1:]):
        leg = _leg((_lat(r1), _lon(c1)), (_lat(r2), _lon(c2)))
        points.extend(leg if not points else leg[1:])
    return points


def _workouts(rng: random.Random) -> list[dict]:
    """One walk on most days, a few longer ones at weekends."""
    workouts = []
    for offset in range(DAYS):
        day = SAMPLE_TODAY - timedelta(days=DAYS - 1 - offset)
        if rng.random() < 0.25:
            continue  # a rest day
        blocks = rng.randint(10, 16) if day.weekday() >= 5 else rng.randint(5, 10)
        points = _walk(rng, blocks)
        start = datetime.combine(day, time(hour=rng.randint(7, 18),
                                           minute=rng.randint(0, 59)))
        metres = len(points) * STEP_M
        workouts.append({
            "id": f"demo-{day.isoformat()}",
            "name": "Outdoor Walk",
            "start": start.strftime(f"%Y-%m-%d %H:%M:%S {OFFSET}"),
            "end": (start + timedelta(seconds=len(points) * 4)).strftime(
                f"%Y-%m-%d %H:%M:%S {OFFSET}"),
            "duration": {"qty": len(points) * 4, "units": "s"},
            "distance": {"qty": round(metres / 1609.34, 2), "units": "mi"},
            "activeEnergy": {"qty": round(metres * 0.06), "units": "kcal"},
            "route": [
                {"latitude": round(lat, 6), "longitude": round(lon, 6),
                 "timestamp": (start + timedelta(seconds=4 * i)).strftime(
                     f"%Y-%m-%d %H:%M:%S {OFFSET}")}
                for i, (lat, lon) in enumerate(points)
            ],
        })
    return workouts


# ---------------------------------------------------------------------------
# The metrics
# ---------------------------------------------------------------------------


def _samples(name: str, unit: str, values: dict[date, float], hour: int) -> dict:
    return {
        "name": name,
        "units": unit,
        "data": [
            {"date": f"{day.isoformat()}T{hour:02d}:00:00{OFFSET[:3]}:{OFFSET[3:]}",
             "qty": round(value, 3)}
            for day, value in sorted(values.items())
        ],
    }


def _metrics(rng: random.Random) -> list[dict]:
    days = [SAMPLE_TODAY - timedelta(days=DAYS - 1 - i) for i in range(DAYS)]

    # A gentle decline with day-to-day noise, so the rolling trend has
    # something to cut through. Invented numbers, not anybody's.
    weight = {}
    level = 176.0
    for day in days:
        level -= 0.045
        weight[day] = level + rng.gauss(0, 0.55)

    steps, distance = {}, {}
    for day in days:
        base = 11_500 if day.weekday() >= 5 else 8_600
        count = max(2_000, rng.gauss(base, 2_400))
        steps[day] = count
        # Stride varies between walking and running days, so the two series
        # are correlated without being redundant.
        distance[day] = count / rng.uniform(1_650, 2_100)

    basal, active, diet = {}, {}, {}
    for index, day in enumerate(days):
        basal[day] = rng.gauss(1_680, 40)
        active[day] = 220 + steps[day] * rng.uniform(0.035, 0.05)
        # Deliberately crosses over — 33 of the 90 days run a surplus. The
        # balance tile then reads a deficit over 7 days and a surplus over 14,
        # so both of its colour states can be shown from one honest dataset
        # rather than by inventing a second one.
        swing = 320 * math.sin(index / 11.0) + 260 * math.sin(index / 3.7)
        diet[day] = basal[day] + active[day] - 200 + swing

    return [
        _samples("weight_body_mass", "lb", weight, 7),
        _samples("step_count", "count", steps, 12),
        _samples("walking_running_distance", "mi", distance, 12),
        _samples("basal_energy_burned", "kcal", basal, 12),
        _samples("active_energy", "kcal", active, 12),
        _samples("dietary_energy", "kcal", diet, 12),
    ]


# ---------------------------------------------------------------------------
# Seeding and serving
# ---------------------------------------------------------------------------


def build_payloads() -> list[dict]:
    """Every export body this demo posts, in order. Deterministic."""
    rng = random.Random(SEED)
    return [
        {"data": {"metrics": _metrics(rng)}},
        {"data": {"workouts": _workouts(rng)}},
    ]


def seed(storage: Path) -> None:
    if storage.exists():
        # A stale store would silently mix old data into the screenshots.
        shutil.rmtree(storage)
    storage.mkdir(parents=True)

    app = create_app(storage_dir=storage, api_token=TOKEN,
                     summary_today=SAMPLE_TODAY)
    with TestClient(app) as client:
        for payload in build_payloads():
            response = client.post("/v1/exports", json=payload,
                                   headers={"Authorization": f"Bearer {TOKEN}"})
            response.raise_for_status()


def card_urls(base: str) -> dict[str, str]:
    """The canonical demo cards, and the URLs the doc screenshots come from."""
    token = derive_embed_token(TOKEN)

    def url(path: str, params: list[tuple[str, str]]) -> str:
        return f"{base}{path}?" + urlencode(params + [("embed_token", token)])

    return {
        "coverage-map": url("/v1/render/map", [
            ("lat", str(MAP_LAT)), ("lon", str(MAP_LON)),
            ("width", "2200"), ("height", "1400"),
            ("min_count", "1"), ("tolerance_m", "5"), ("weight", "5"),
            ("zoom_control", "false"), ("attribution", "true"),
        ]),
        "chart-line": url("/v1/render/chart", [
            ("metric", "weight_body_mass"), ("date_range", "last 90 days"),
            ("window", "21"), ("title", "Weight"),
        ]),
        "chart-bar": url("/v1/render/chart", [
            ("metric", "step_count"), ("unit", ""), ("title", "Steps"),
            ("kind", "bar"), ("window", "0"), ("date_range", "last 30 days"),
        ]),
        "chart-stacked": url("/v1/render/chart", [
            ("metric", "basal_energy_burned"), ("stack", "burn"),
            ("label", "Resting"), ("unit", "kcal"),
            ("metric", "active_energy"), ("stack", "burn"),
            ("label", "Active"), ("unit", "kcal"),
            ("metric", "dietary_energy"), ("stack", "eaten"),
            ("label", "Eaten"), ("unit", "kcal"),
            ("kind", "bar"), ("window", "0"), ("layout", "overlay"),
            ("date_range", "last 7 days"), ("title", "Energy"),
        ]),
        "stat-latest": url("/v1/render/stat", [
            ("metric", "weight_body_mass"), ("label", "Current"),
            ("margin", "5"), ("align", "center"),
        ]),
        "stat-change": url("/v1/render/stat", [
            ("metric", "weight_body_mass"), ("stat", "change"),
            ("good_direction", "down"), ("margin", "5"), ("align", "center"),
        ]),
        "stat-balance": url("/v1/render/stat", [
            ("stat", "balance"), ("metric", "dietary_energy"),
            ("minus", "basal_energy_burned"), ("minus", "active_energy"),
            ("window", "7"), ("label", "7-day balance"),
            ("margin", "5"), ("align", "center"),
        ]),
        # The same tile over a longer window, which lands on a surplus — the
        # only card with a two-sided colour, so the doc should show both.
        "stat-surplus": url("/v1/render/stat", [
            ("stat", "balance"), ("metric", "dietary_energy"),
            ("minus", "basal_energy_burned"), ("minus", "active_energy"),
            ("window", "14"), ("label", "14-day balance"),
            ("margin", "5"), ("align", "center"),
        ]),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", type=Path,
                        default=Path(__file__).resolve().parent.parent
                        / "storage" / "demo",
                        help="where to put the store (recreated each run)")
    parser.add_argument("--serve", action="store_true", help="serve after seeding")
    parser.add_argument("--urls", action="store_true", help="print the card URLs")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    seed(args.dir)
    print(f"seeded {args.dir} with {DAYS} days ending {SAMPLE_TODAY}", file=sys.stderr)

    base = f"http://127.0.0.1:{args.port}"
    if args.urls or not args.serve:
        for name, url in card_urls(base).items():
            print(f"{name}\t{url}")

    if args.serve:
        import uvicorn

        # Built here rather than through create_app_from_env because the demo
        # needs "today" pinned, and production has no business growing a knob
        # for that.
        app = create_app(storage_dir=args.dir, api_token=TOKEN,
                         summary_today=SAMPLE_TODAY)
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
