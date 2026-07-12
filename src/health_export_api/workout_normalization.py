"""Normalization and summarization of Apple Health workout payloads.

Workout payloads from Health Auto Export look like:
  {"data": {"workouts": [{"id": "...", "name": "Outdoor Walk", ...}, ...]}}

Key design decisions:
- Deduplication by workout `id` field (UUID assigned by HealthKit).
- "Traditional Strength Training" is the type Hevy writes back to Apple Health.
  These are excluded from summary queries by default (include_hevy=False) to
  avoid double-counting against Hevy MCP data.
- When workout_type is None, all matching workout types are aggregated together.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Mapping

# Workout type written by Hevy to HealthKit — excluded by default.
HEVY_WORKOUT_TYPE = "Traditional Strength Training"

_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S %z",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%d",
)


@dataclass(frozen=True)
class WorkoutSession:
    id: str
    name: str
    started: datetime
    duration_min: float
    distance_mi: float
    active_energy_kcal: float
    avg_heart_rate: float | None


def _parse_timestamp(value: str) -> datetime | None:
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _qty(field: Any, default: float = 0.0) -> float:
    if isinstance(field, dict):
        return float(field.get("qty", default))
    if isinstance(field, (int, float)):
        return float(field)
    return default


def _iter_workouts(records: Iterable[Mapping[str, Any]]) -> Iterable[WorkoutSession]:
    """Yield WorkoutSession objects from all stored export records."""
    for record in records:
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            continue
        data = payload.get("data")
        if not isinstance(data, Mapping):
            continue
        raw_list = data.get("workouts")
        if not isinstance(raw_list, list):
            continue
        for raw in raw_list:
            if not isinstance(raw, Mapping):
                continue
            wid = raw.get("id")
            if not isinstance(wid, str) or not wid:
                continue
            name = raw.get("name")
            if not isinstance(name, str) or not name:
                continue
            start_str = raw.get("start")
            if not isinstance(start_str, str):
                continue
            started = _parse_timestamp(start_str)
            if started is None:
                continue

            # Duration: Health Auto Export stores seconds in qty
            dur_sec = _qty(raw.get("duration"), 0.0)
            dur_min = dur_sec / 60.0

            # Distance: units should be "mi" — trust the export config
            dist_mi = _qty(raw.get("distance"), 0.0)

            # Active energy
            ae = _qty(
                raw.get("activeEnergy", raw.get("activeEnergyBurned")), 0.0
            )

            # Average heart rate
            hr_raw = raw.get("avgHeartRate")
            avg_hr = _qty(hr_raw) if hr_raw is not None else None
            if avg_hr == 0.0:
                avg_hr = None

            yield WorkoutSession(
                id=wid,
                name=name,
                started=started,
                duration_min=dur_min,
                distance_mi=dist_mi,
                active_energy_kcal=ae,
                avg_heart_rate=avg_hr,
            )



