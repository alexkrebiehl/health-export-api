"""The seam between the rendering layer and the data layer.

Everything the render endpoints are allowed to ask for, and nothing else. The
render routes never touch :class:`~health_export_api.store.Store` directly —
they come through here.

Two properties make this the boundary a process split would follow:

* **Plain arguments in, plain JSON-able dicts out.** The dicts returned are
  byte-identical to the bodies of the corresponding data endpoints, so a remote
  implementation would be a thin HTTP client with no shape translation.

* **No web framework.** Failures are raised as domain errors — ``ValueError``
  for an unusable date range, :class:`~health_export_api.throttle.QueueFull`
  when load-shedding — and the routers turn those into status codes. Keeping
  ``HTTPException`` out of here is what lets the same object be used from a
  process that is not serving HTTP.

Caching and the request gate live on this side because they belong to the work
being protected: the coverage computation is CPU-bound, and it is the thing
that must be serialised no matter who asks for it.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Callable, Sequence

from health_export_api.normalization import resolve_date_range
from health_export_api.store import Store
from health_export_api.throttle import RequestGate, TTLCache


class DataProvider:
    """Read access to health data, shared by the data and render routers."""

    def __init__(
        self,
        store: Store,
        *,
        cache: TTLCache,
        gate: RequestGate,
        today: Callable[[], date],
    ) -> None:
        self._store = store
        self._cache = cache
        self._gate = gate
        self._today = today

    @property
    def today(self) -> date:
        """The current date, per the server clock (see the TZ env var)."""
        return self._today()

    def resolve_range(
        self,
        *,
        date_range: str | None,
        start_date: str | None,
        end_date: str | None,
        default_range: str | None = None,
    ) -> tuple[date | None, date | None]:
        """Resolve a timeframe, or ``(None, None)`` when none was asked for.

        Raises ``ValueError`` on an unusable range, which the routers surface
        as a 422.
        """
        if not (date_range or start_date or end_date or default_range):
            return None, None
        return resolve_date_range(
            date_range=date_range or (default_range if not start_date else None),
            start_date=start_date,
            end_date=end_date,
            today=self.today,
        )

    def metric_summary(
        self,
        *,
        metric: str,
        start_date: date,
        end_date: date,
        granularity: str = "day",
    ) -> dict[str, Any]:
        """A metric's series — the body of ``GET /v1/health/summary``.

        Cached but deliberately not gated: this reads a handful of rows and is
        nothing like the GIL-bound coverage render, so queueing it behind one
        would be pure latency.
        """
        key = ("summary", metric, start_date.isoformat(), end_date.isoformat(),
               granularity)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        result = self._store.summarize_metric(
            metric=metric,
            start_date=start_date,
            end_date=end_date,
            granularity=granularity,
        )
        self._cache.put(key, result)
        return result

    def invalidate(self) -> None:
        """Drop every cached answer, because the data underneath them changed.

        Called when an export is ingested. Without it the cache is only
        time-bounded, so a new reading could sit in the store for the whole TTL
        while the rendered tiles kept serving the figures from before it —
        `/v1/health/summary` current, the dashboard five minutes behind, and no
        way to tell from the page which you were looking at.

        Everything goes, not just the summaries: an export can carry workouts,
        and those move the coverage map.
        """
        self._cache.clear()

    def coverage(
        self,
        *,
        lat: float,
        lon: float,
        width: float,
        height: float,
        start_date: date | None,
        end_date: date | None,
        workout_type: Sequence[str] | None,
        max_vertices: int,
        tolerance_m: float,
        min_count: int,
    ) -> dict[str, Any]:
        """Route coverage — the body of ``GET /v1/workouts/routes/geojson``.

        Raises :class:`QueueFull` when too many renders are already queued; the
        routers surface that as a 429.
        """
        # Keyed on the filters that shape the geometry. Presentation options
        # are deliberately absent, so restyling an area is a cache hit.
        key = (
            "coverage",
            lat,
            lon,
            width,
            height,
            start_date.isoformat() if start_date else None,
            end_date.isoformat() if end_date else None,
            tuple(workout_type) if workout_type else None,
            max_vertices,
            tolerance_m,
            min_count,
        )

        # Checked before queueing, so a cached answer never waits behind a
        # computation that is already running.
        hit = self._cache.get(key)
        if hit is not None:
            return hit

        with self._gate.enter():
            # Re-check: an identical request may have finished while this one
            # was waiting its turn, which is the common case when a URL edit
            # fires a request per keystroke.
            hit = self._cache.get(key)
            if hit is not None:
                return hit
            result = self._store.route_coverage_geojson(
                lat=lat,
                lon=lon,
                width=width,
                height=height,
                start_date=start_date,
                end_date=end_date,
                workout_types=workout_type,
                max_vertices=max_vertices,
                tolerance_m=tolerance_m,
                min_count=min_count,
            )
            self._cache.put(key, result)
            return result
