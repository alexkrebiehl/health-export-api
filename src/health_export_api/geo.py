"""Route coverage geometry — turning many GPS tracks into one set of paths.

The goal is a *coverage* view: given every workout route recorded inside some
box, produce the union of streets travelled, not one squiggle per session.

Key design decisions:

* **Grid snapping, not geometric buffering.** Each GPS point is rounded to a
  cell of ``tolerance_m`` on a side. Two tracks that ran along opposite
  sidewalks of the same street land in the same cells and collapse into one
  path. This is O(n) and needs no geometry library — the container has neither
  shapely nor numpy, and a 256Mi memory ceiling.

* **Undirected edges.** A segment is keyed on the *sorted* pair of its two
  cells, so walking a street north-to-south and later south-to-north produces
  one segment with ``count = 2`` rather than two overlapping lines.

* **Bounded memory.** The aggregator only ever holds cells and segments, both
  bounded by the area of the box divided by the tolerance — never by the
  number of raw GPS points, which may be in the millions.

* **Chaining.** Emitting every segment as its own two-point LineString would
  double the vertex count and produce thousands of features. Segments are
  dissolved back into long polylines, broken only at junctions and where the
  rendered properties (traversal count, workout types) change.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from math import cos, radians
from typing import Any, Iterable

# Metres per degree of latitude. Longitude is this scaled by cos(latitude).
# A sphere is accurate enough: over a city-sized box the error is centimetres.
M_PER_DEG_LAT = 111_320.0

# Guard against division by zero at the poles, where cos(lat) collapses.
_MIN_COS_LAT = 1e-6

# GeoJSON coordinate precision. Six decimals is ~0.11m — finer than GPS.
_COORD_PRECISION = 6

Cell = tuple[int, int]
Edge = tuple[Cell, Cell]


# ---------------------------------------------------------------------------
# Bounding box
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BoundingBox:
    min_lat: float
    min_lon: float
    max_lat: float
    max_lon: float

    def as_geojson_bbox(self) -> list[float]:
        """GeoJSON orders a bbox [west, south, east, north]."""
        return [self.min_lon, self.min_lat, self.max_lon, self.max_lat]


def bounding_box(
    *, lat: float, lon: float, width_m: float, height_m: float
) -> BoundingBox:
    """Box of ``width_m`` x ``height_m`` metres centred on (lat, lon).

    Boxes that would run past a pole or across the antimeridian are clamped
    rather than wrapped; splitting a box at +/-180 is not supported.
    """
    half_lat = (height_m / 2) / M_PER_DEG_LAT
    half_lon = (width_m / 2) / (M_PER_DEG_LAT * max(cos(radians(lat)), _MIN_COS_LAT))
    return BoundingBox(
        min_lat=max(lat - half_lat, -90.0),
        min_lon=max(lon - half_lon, -180.0),
        max_lat=min(lat + half_lat, 90.0),
        max_lon=min(lon + half_lon, 180.0),
    )


def cell_size_degrees(*, center_lat: float, tolerance_m: float) -> tuple[float, float]:
    """Size of one snapping cell as (latitude degrees, longitude degrees).

    Derived once from the centre latitude of the box so that cells stay a
    uniform shape across it, rather than shearing with each point's latitude.
    """
    return (
        tolerance_m / M_PER_DEG_LAT,
        tolerance_m / (M_PER_DEG_LAT * max(cos(radians(center_lat)), _MIN_COS_LAT)),
    )


def _edge_key(a: Cell, b: Cell) -> Edge:
    """Order-independent key, so both directions of travel share a segment."""
    return (a, b) if a <= b else (b, a)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


class CellBudgetExceeded(Exception):
    """The grid was too fine for the area asked for — retry with a coarser one.

    Raised mid-stream so an over-fine pass is abandoned as soon as it is known
    to be hopeless, rather than filling the heap with cells that are about to
    be thrown away.
    """


@dataclass(slots=True)
class _SegmentStat:
    """How many sessions used one segment, and what kind they were.

    Slotted because there is one of these per segment, and a box the size of a
    city holds hundreds of thousands of them.
    """

    count: int = 0
    types: set[str] = field(default_factory=set)
    first: str = ""
    last: str = ""


class SegmentAggregator:
    """Accumulates snapped, deduplicated route segments across many workouts.

    Feed one workout at a time with :meth:`add_workout`, then read the merged
    result with :meth:`features`.
    """

    def __init__(
        self,
        *,
        center_lat: float,
        tolerance_m: float,
        max_cells: int | None = None,
    ) -> None:
        self._d_lat, self._d_lon = cell_size_degrees(
            center_lat=center_lat, tolerance_m=tolerance_m
        )
        self._max_cells = max_cells
        # cell -> [latitude sum, longitude sum, point count]; the representative
        # coordinate is the running mean, so a cell shared between features
        # always resolves to the identical coordinate and the lines join up.
        self._cells: dict[Cell, list[float]] = {}
        self._segments: dict[Edge, _SegmentStat] = {}
        self.workout_count = 0

    def add_workout(
        self,
        points: Iterable[tuple[int, float, float]],
        *,
        workout_type: str,
        started_date: str,
    ) -> None:
        """Add one workout's in-box points as (point_index, lat, lon), in order.

        ``point_index`` is the position in the original route array. Only points
        inside the box are passed in, so a route that leaves and re-enters
        arrives here with a hole in its indices — a break in the index sequence
        is treated as a break in the path, which is what stops a false straight
        line being drawn across the box. Genuine holes also occur where
        ingestion skipped a malformed point, and breaking there is equally
        correct.
        """
        edges: set[Edge] = set()
        previous_index: int | None = None
        previous_cell: Cell | None = None

        for index, lat, lon in points:
            cell = self._record(lat, lon)
            if (
                previous_cell is not None
                and index == previous_index + 1
                and cell != previous_cell
            ):
                edges.add(_edge_key(previous_cell, cell))
            previous_index, previous_cell = index, cell

        if previous_cell is None:
            return  # no points in the box
        self.workout_count += 1

        # One increment per workout per segment: pacing back and forth along
        # the same street within a single session still counts as one pass.
        for edge in edges:
            stat = self._segments.get(edge)
            if stat is None:
                stat = _SegmentStat(first=started_date, last=started_date)
                self._segments[edge] = stat
            stat.count += 1
            stat.types.add(workout_type)
            stat.first = min(stat.first, started_date)
            stat.last = max(stat.last, started_date)

    def prune_below(self, min_count: int) -> None:
        """Drop segments walked fewer than ``min_count`` times.

        Snapping a street walked a hundred times does not produce one line: GPS
        scatter spreads each pass across a band of neighbouring cells, and the
        one-off excursions cross-link them into a braid. Those stray edges are
        real data — someone did walk there once — but they dominate the
        topology. Over 120 walks of one neighbourhood, 44% of nodes came out as
        junctions rather than the two neighbours a clean line would have, and
        dropping single-pass edges alone halved the graph.

        Pruning is therefore about which paths are worth drawing, not about
        tidying geometry, so it is opt-in and off by default.
        """
        if min_count <= 1:
            return
        self._segments = {
            edge: stat
            for edge, stat in self._segments.items()
            if stat.count >= min_count
        }

    def _record(self, lat: float, lon: float) -> Cell:
        cell = (round(lat / self._d_lat), round(lon / self._d_lon))
        accumulator = self._cells.get(cell)
        if accumulator is None:
            if self._max_cells is not None and len(self._cells) >= self._max_cells:
                raise CellBudgetExceeded
            self._cells[cell] = [lat, lon, 1]
        else:
            accumulator[0] += lat
            accumulator[1] += lon
            accumulator[2] += 1
        return cell

    def coordinate(self, cell: Cell) -> list[float]:
        """Representative [longitude, latitude] for a cell — GeoJSON axis order."""
        lat_sum, lon_sum, n = self._cells[cell]
        return [
            round(lon_sum / n, _COORD_PRECISION),
            round(lat_sum / n, _COORD_PRECISION),
        ]

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    def features(self) -> list[dict[str, Any]]:
        """The merged coverage as a list of GeoJSON LineString features."""
        return self.features_from_chains(self.chains())

    def features_from_chains(
        self, chains: list[list[Cell]]
    ) -> list[dict[str, Any]]:
        """Render already-computed chains as GeoJSON features.

        Split out from :meth:`chains` because callers fitting a vertex budget
        need the vertex count of a candidate grid, and turning chains into
        feature dictionaries costs far more than the chains themselves — worth
        avoiding for a pass that is about to be discarded.
        """
        return [self._feature(chain) for chain in chains]

    def _signature(self, edge: Edge) -> tuple[int, tuple[str, ...]]:
        stat = self._segments[edge]
        return (stat.count, tuple(sorted(stat.types)))

    def chains(self) -> list[list[Cell]]:
        """Dissolve segments into maximal polylines of uniform properties."""
        adjacency: dict[Cell, list[Cell]] = defaultdict(list)
        for a, b in self._segments:
            adjacency[a].append(b)
            adjacency[b].append(a)

        def is_break(cell: Cell) -> bool:
            """True at a junction, a dead end, or a change in properties."""
            neighbours = adjacency[cell]
            if len(neighbours) != 2:
                return True
            left, right = neighbours
            return self._signature(_edge_key(cell, left)) != self._signature(
                _edge_key(cell, right)
            )

        visited: set[Edge] = set()

        def walk(start: Cell, first_hop: Cell) -> list[Cell]:
            visited.add(_edge_key(start, first_hop))
            chain = [start, first_hop]
            previous, node = start, first_hop
            while not is_break(node):
                # Exactly two neighbours here, and they are distinct: segments
                # are stored in a dict keyed on unordered pairs, so an edge
                # cannot repeat, and a cell is never adjacent to itself.
                following = next(n for n in adjacency[node] if n != previous)
                edge = _edge_key(node, following)
                if edge in visited:
                    break  # closed the loop back onto the start
                visited.add(edge)
                chain.append(following)
                previous, node = node, following
            return chain

        chains: list[list[Cell]] = []
        for cell in [c for c in adjacency if is_break(c)]:
            for neighbour in adjacency[cell]:
                if _edge_key(cell, neighbour) not in visited:
                    chains.append(walk(cell, neighbour))
        # Whatever is left is a closed ring of uniform properties with no
        # break to start from, so begin anywhere on it.
        for a, b in self._segments:
            if _edge_key(a, b) not in visited:
                chains.append(walk(a, b))
        return chains

    def _feature(self, chain: list[Cell]) -> dict[str, Any]:
        stats = [
            self._segments[_edge_key(chain[i], chain[i + 1])]
            for i in range(len(chain) - 1)
        ]
        return {
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": [self.coordinate(cell) for cell in chain],
            },
            "properties": {
                "count": max(s.count for s in stats),
                "workout_types": sorted({t for s in stats for t in s.types}),
                "first_seen": min(s.first for s in stats),
                "last_seen": max(s.last for s in stats),
            },
        }


# ---------------------------------------------------------------------------
# Vertex budget
# ---------------------------------------------------------------------------


def count_vertices(features: list[dict[str, Any]]) -> int:
    return sum(len(f["geometry"]["coordinates"]) for f in features)


def count_chain_vertices(chains: list[list[Cell]]) -> int:
    return sum(len(chain) for chain in chains)


def trim_to_vertex_budget(
    features: list[dict[str, Any]], max_vertices: int
) -> list[dict[str, Any]]:
    """Drop the least-travelled paths until the budget is met.

    A last resort for when raising the snapping tolerance has not brought the
    vertex count down far enough. Frequently-walked paths matter most to a
    coverage map, so they are kept first.
    """
    ordered = sorted(features, key=lambda f: -f["properties"]["count"])
    kept: list[dict[str, Any]] = []
    used = 0
    for feature in ordered:
        size = len(feature["geometry"]["coordinates"])
        if used + size > max_vertices:
            continue
        kept.append(feature)
        used += size
    return kept
