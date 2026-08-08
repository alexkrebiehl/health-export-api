"""Tests for route-coverage geometry and GET /v1/workouts/routes/geojson."""
from datetime import date
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from health_export_api.app import create_app
from health_export_api.geo import SegmentAggregator, bounding_box, trim_to_vertex_budget

HEADERS = {"Authorization": "Bearer test-token"}

# A patch of Berlin. CENTER_* is the centre of one 15m snapping cell, so points
# placed a few metres either side of it are unambiguously inside that cell
# rather than straddling a boundary.
CENTER_LAT = 52.5199425
CENTER_LON = 13.3999414
LON_STEP = 0.00022145  # ~15m east at this latitude
FIVE_M = 0.0000449  # ~5m north

# Two tracks 10m apart — think opposite sidewalks of the same street.
SOUTH_SIDE = CENTER_LAT - FIVE_M
NORTH_SIDE = CENTER_LAT + FIVE_M

BOX = {"lat": 52.52, "lon": 13.40, "width": 500, "height": 500}


def _lon(step: int) -> float:
    return CENTER_LON + step * LON_STEP


def _route(points: list[tuple[float, float]], day: int) -> list[dict[str, Any]]:
    def stamp(index: int) -> str:
        # One point per second. Ingestion drops points whose timestamp will not
        # parse, so the clock has to stay valid however long the track is.
        minutes, seconds = divmod(index, 60)
        return f"2026-07-{day:02d} 08:{minutes:02d}:{seconds:02d} -0400"

    return [
        {"latitude": lat, "longitude": lon, "timestamp": stamp(index)}
        for index, (lat, lon) in enumerate(points)
    ]


def _workout(
    workout_id: str,
    *,
    name: str = "Outdoor Walk",
    day: int = 10,
    points: list[tuple[float, float]],
) -> dict[str, Any]:
    return {
        "id": workout_id,
        "name": name,
        "start": f"2026-07-{day:02d} 08:00:00 -0400",
        "end": f"2026-07-{day:02d} 08:45:00 -0400",
        "duration": {"qty": 2700, "units": "s"},
        "distance": {"qty": 2.5, "units": "mi"},
        "activeEnergy": {"qty": 280, "units": "kcal"},
        "route": _route(points, day),
    }


def _straight(lat: float, steps: int = 5, reverse: bool = False) -> list[tuple[float, float]]:
    lons = [_lon(i) for i in range(steps)]
    if reverse:
        lons.reverse()
    return [(lat, lon) for lon in lons]


def make_client(tmp_path: Path) -> TestClient:
    return TestClient(
        create_app(storage_dir=tmp_path, api_token="test-token",
                   summary_today=date(2026, 7, 12))
    )


def fetch(client: TestClient, **params: Any) -> dict[str, Any]:
    response = client.get(
        "/v1/workouts/routes/geojson", headers=HEADERS, params={**BOX, **params}
    )
    assert response.status_code == 200, response.text
    return response.json()


def ingest(client: TestClient, *workouts: dict[str, Any]) -> None:
    response = client.post(
        "/v1/exports", headers=HEADERS, json={"data": {"workouts": list(workouts)}}
    )
    assert response.status_code == 201, response.text


# ---------------------------------------------------------------------------
# Bounding box maths
# ---------------------------------------------------------------------------


def test_bounding_box_is_centred_on_the_given_point() -> None:
    box = bounding_box(lat=0.0, lon=0.0, width_m=222640.0, height_m=222640.0)

    assert box.min_lat == -1.0 and box.max_lat == 1.0
    assert box.min_lon == -1.0 and box.max_lon == 1.0


def test_bounding_box_widens_in_longitude_away_from_the_equator() -> None:
    equator = bounding_box(lat=0.0, lon=0.0, width_m=1000.0, height_m=1000.0)
    north = bounding_box(lat=60.0, lon=0.0, width_m=1000.0, height_m=1000.0)

    # cos(60 degrees) is 0.5, so a 1km box spans twice as many degrees there.
    assert round(north.max_lon / equator.max_lon, 3) == 2.0
    assert round(north.max_lat - north.min_lat, 9) == round(
        equator.max_lat - equator.min_lat, 9
    )


def test_bounding_box_clamps_at_the_pole_instead_of_wrapping() -> None:
    box = bounding_box(lat=89.999, lon=179.999, width_m=100_000.0, height_m=100_000.0)

    assert box.max_lat == 90.0
    assert box.max_lon == 180.0


def test_geojson_bbox_is_ordered_west_south_east_north() -> None:
    box = bounding_box(lat=52.52, lon=13.40, width_m=500.0, height_m=500.0)

    assert box.as_geojson_bbox() == [
        box.min_lon,
        box.min_lat,
        box.max_lon,
        box.max_lat,
    ]


# ---------------------------------------------------------------------------
# Snapping and merging
# ---------------------------------------------------------------------------


def _aggregate(tolerance_m: float, *tracks: list[tuple[float, float]]) -> list[dict[str, Any]]:
    aggregator = SegmentAggregator(center_lat=52.52, tolerance_m=tolerance_m)
    for index, track in enumerate(tracks):
        aggregator.add_workout(
            [(i, lat, lon) for i, (lat, lon) in enumerate(track)],
            workout_type="Outdoor Walk",
            started_date=f"2026-07-{10 + index:02d}",
        )
    return aggregator.features()


def test_nearby_tracks_merge_into_one_path_with_a_traversal_count() -> None:
    features = _aggregate(15.0, _straight(SOUTH_SIDE), _straight(NORTH_SIDE))

    assert len(features) == 1
    assert features[0]["properties"]["count"] == 2
    assert len(features[0]["geometry"]["coordinates"]) == 5


def test_a_tight_tolerance_keeps_nearby_tracks_apart() -> None:
    features = _aggregate(2.0, _straight(SOUTH_SIDE), _straight(NORTH_SIDE))

    assert len(features) == 2
    assert [f["properties"]["count"] for f in features] == [1, 1]


def test_opposite_directions_of_travel_merge() -> None:
    features = _aggregate(
        15.0, _straight(SOUTH_SIDE), _straight(SOUTH_SIDE, reverse=True)
    )

    assert len(features) == 1
    assert features[0]["properties"]["count"] == 2


def test_a_gap_in_point_index_breaks_the_path() -> None:
    aggregator = SegmentAggregator(center_lat=52.52, tolerance_m=15.0)
    # Indices 2 and 3 are missing — the points were outside the box, or were
    # dropped at ingestion. No line should be drawn across the hole.
    aggregator.add_workout(
        [(0, CENTER_LAT, _lon(0)), (1, CENTER_LAT, _lon(1)),
         (4, CENTER_LAT, _lon(4)), (5, CENTER_LAT, _lon(5))],
        workout_type="Outdoor Walk",
        started_date="2026-07-10",
    )

    features = aggregator.features()

    assert len(features) == 2
    assert all(len(f["geometry"]["coordinates"]) == 2 for f in features)


def test_a_workout_confined_to_one_cell_yields_no_geometry() -> None:
    assert _aggregate(15.0, [(CENTER_LAT, CENTER_LON), (CENTER_LAT, CENTER_LON)]) == []


def test_chain_splits_where_the_traversal_count_changes() -> None:
    # One track covers the whole street, the other only its eastern half, so
    # the two halves cannot share a feature.
    features = _aggregate(15.0, _straight(CENTER_LAT, steps=5),
                          [(CENTER_LAT, _lon(i)) for i in (2, 3, 4)])

    assert sorted(f["properties"]["count"] for f in features) == [1, 2]


def test_prune_below_drops_the_least_travelled_paths() -> None:
    aggregator = SegmentAggregator(center_lat=52.52, tolerance_m=15.0)
    # The street twice, plus one detour down a side road.
    for index, track in enumerate([
        _straight(CENTER_LAT), _straight(CENTER_LAT),
        [(CENTER_LAT, _lon(2)), (CENTER_LAT - 0.0005, _lon(2))],
    ]):
        aggregator.add_workout(
            [(i, lat, lon) for i, (lat, lon) in enumerate(track)],
            workout_type="Outdoor Walk",
            started_date=f"2026-07-{10 + index:02d}",
        )
    assert len(aggregator.features()) == 3  # the detour splits the street

    aggregator.prune_below(2)
    features = aggregator.features()

    assert len(features) == 1
    assert features[0]["properties"]["count"] == 2
    assert len(features[0]["geometry"]["coordinates"]) == 5


def test_prune_below_one_keeps_everything() -> None:
    aggregator = SegmentAggregator(center_lat=52.52, tolerance_m=15.0)
    aggregator.add_workout(
        [(i, lat, lon) for i, (lat, lon) in enumerate(_straight(CENTER_LAT))],
        workout_type="Outdoor Walk",
        started_date="2026-07-10",
    )

    aggregator.prune_below(1)

    assert len(aggregator.features()) == 1


def test_properties_span_the_dates_and_types_that_contributed() -> None:
    aggregator = SegmentAggregator(center_lat=52.52, tolerance_m=15.0)
    for index, name in enumerate(["Outdoor Walk", "Outdoor Run"]):
        aggregator.add_workout(
            [(i, lat, lon) for i, (lat, lon) in enumerate(_straight(CENTER_LAT))],
            workout_type=name,
            started_date=f"2026-07-{10 + index:02d}",
        )

    properties = aggregator.features()[0]["properties"]

    assert properties["workout_types"] == ["Outdoor Run", "Outdoor Walk"]
    assert properties["first_seen"] == "2026-07-10"
    assert properties["last_seen"] == "2026-07-11"


def test_trim_to_vertex_budget_keeps_the_busiest_paths() -> None:
    def feature(count: int, vertices: int) -> dict[str, Any]:
        return {
            "geometry": {"coordinates": [[0.0, 0.0]] * vertices},
            "properties": {"count": count},
        }

    kept = trim_to_vertex_budget([feature(1, 5), feature(9, 4), feature(3, 3)], 7)

    assert [f["properties"]["count"] for f in kept] == [9, 3]


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


def test_endpoint_returns_a_feature_collection_for_overlapping_walks(
    tmp_path: Path,
) -> None:
    client = make_client(tmp_path)
    ingest(
        client,
        _workout("walk-1", day=10, points=_straight(SOUTH_SIDE)),
        _workout("walk-2", day=11, points=_straight(NORTH_SIDE)),
    )

    body = fetch(client)

    assert body["type"] == "FeatureCollection"
    assert len(body["bbox"]) == 4
    assert len(body["features"]) == 1
    feature = body["features"][0]
    assert feature["geometry"]["type"] == "LineString"
    assert feature["properties"] == {
        "count": 2,
        "workout_types": ["Outdoor Walk"],
        "first_seen": "2026-07-10",
        "last_seen": "2026-07-11",
    }
    assert body["properties"]["workout_count"] == 2
    assert body["properties"]["vertex_count"] == 5
    assert body["properties"]["tolerance_m"] == 15.0


def test_coordinates_are_longitude_then_latitude(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    ingest(client, _workout("walk-1", points=_straight(CENTER_LAT)))

    coordinates = fetch(client)["features"][0]["geometry"]["coordinates"]

    for lon, lat in coordinates:
        assert 13.39 < lon < 13.41
        assert 52.51 < lat < 52.53


def test_points_outside_the_box_are_excluded(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    ingest(
        client,
        _workout("inside", points=_straight(CENTER_LAT)),
        _workout("far-away", points=[(48.85 + i * 0.0002, 2.35) for i in range(5)]),
    )

    body = fetch(client)

    assert body["properties"]["workout_count"] == 1
    for feature in body["features"]:
        for lon, lat in feature["geometry"]["coordinates"]:
            assert 52.51 < lat < 52.53


def test_a_route_that_leaves_and_re_enters_the_box_is_not_bridged(
    tmp_path: Path,
) -> None:
    client = make_client(tmp_path)
    # East along one street, out of the box entirely, then back for a second
    # street to the south. The excursion must not be drawn as a straight line.
    ingest(
        client,
        _workout(
            "walk-1",
            points=[
                (CENTER_LAT, _lon(0)), (CENTER_LAT, _lon(1)),
                (53.60, 13.40), (53.61, 13.40),
                (CENTER_LAT - 0.001, _lon(0)), (CENTER_LAT - 0.001, _lon(1)),
            ],
        ),
    )

    body = fetch(client)

    assert len(body["features"]) == 2
    for feature in body["features"]:
        assert len(feature["geometry"]["coordinates"]) == 2
        for _, lat in feature["geometry"]["coordinates"]:
            assert lat < 52.53


def test_workout_type_filter_accepts_one_or_several_types(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    ingest(
        client,
        _workout("walk-1", name="Outdoor Walk", points=_straight(CENTER_LAT)),
        _workout("run-1", name="Outdoor Run", points=_straight(CENTER_LAT - 0.0005)),
        _workout("cycle-1", name="Outdoor Cycling",
                 points=_straight(CENTER_LAT - 0.001)),
    )

    only_walks = fetch(client, workout_type="Outdoor Walk")
    walks_and_runs = fetch(client, workout_type=["Outdoor Walk", "Outdoor Run"])
    everything = fetch(client)

    assert only_walks["properties"]["workout_count"] == 1
    assert only_walks["features"][0]["properties"]["workout_types"] == ["Outdoor Walk"]
    assert walks_and_runs["properties"]["workout_count"] == 2
    assert everything["properties"]["workout_count"] == 3


def test_timeframe_is_optional_and_filters_on_the_session_start_date(
    tmp_path: Path,
) -> None:
    client = make_client(tmp_path)
    ingest(
        client,
        _workout("walk-1", day=10, points=_straight(CENTER_LAT)),
        _workout("walk-2", day=12, points=_straight(CENTER_LAT - 0.001)),
    )

    assert fetch(client)["properties"]["workout_count"] == 2
    windowed = fetch(client, start_date="2026-07-11", end_date="2026-07-13")
    assert windowed["properties"]["workout_count"] == 1
    assert windowed["properties"]["start_date"] == "2026-07-11"
    assert fetch(client, date_range="last 1 days")["properties"]["workout_count"] == 1


def test_max_vertices_is_respected_by_coarsening_the_tolerance(
    tmp_path: Path,
) -> None:
    client = make_client(tmp_path)
    # ~6km of street: 400 points, each its own cell at the default tolerance.
    ingest(client, _workout("walk-1", points=[(CENTER_LAT, _lon(i)) for i in range(400)]))

    detailed = fetch(client, width=20000, height=20000)
    budgeted = fetch(client, width=20000, height=20000, max_vertices=100)

    assert detailed["properties"]["vertex_count"] == 400
    assert budgeted["properties"]["vertex_count"] <= 100
    assert budgeted["properties"]["tolerance_m"] > 15.0


def test_min_count_keeps_only_repeatedly_travelled_paths(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    ingest(
        client,
        _workout("walk-1", day=10, points=_straight(CENTER_LAT)),
        _workout("walk-2", day=11, points=_straight(CENTER_LAT)),
        _workout("walk-3", day=12, points=_straight(CENTER_LAT - 0.001)),
    )

    everything = fetch(client)
    regulars = fetch(client, min_count=2)

    assert sorted(f["properties"]["count"] for f in everything["features"]) == [1, 2]
    assert [f["properties"]["count"] for f in regulars["features"]] == [2]
    assert regulars["properties"]["min_count"] == 2
    # Both workouts are still reported: the filter is on paths, not sessions.
    assert regulars["properties"]["workout_count"] == 3


def test_an_empty_area_returns_an_empty_feature_collection(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    ingest(client, _workout("walk-1", points=_straight(CENTER_LAT)))

    body = fetch(client, lat=48.85, lon=2.35)

    assert body["type"] == "FeatureCollection"
    assert body["features"] == []
    assert body["properties"]["workout_count"] == 0


def test_a_half_specified_date_range_is_rejected(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    response = client.get(
        "/v1/workouts/routes/geojson",
        headers=HEADERS,
        params={**BOX, "start_date": "2026-07-01"},
    )

    assert response.status_code == 422


def test_an_out_of_range_latitude_is_rejected(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    response = client.get(
        "/v1/workouts/routes/geojson", headers=HEADERS, params={**BOX, "lat": 91}
    )

    assert response.status_code == 422


def test_route_geojson_requires_a_bearer_token(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    assert client.get("/v1/workouts/routes/geojson", params=BOX).status_code == 401
