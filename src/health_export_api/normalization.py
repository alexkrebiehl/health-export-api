import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Mapping

_SUMMED_METRICS = {
    "active_energy",
    "apple_exercise_time",
    "apple_stand_hour",
    "apple_stand_time",
    "basal_energy_burned",
    "calcium",
    "carbohydrates",
    "cholesterol",
    "dietary_energy",
    "dietary_sugar",
    "fiber",
    "flights_climbed",
    "iron",
    "magnesium",
    "potassium",
    "protein",
    "sleep_analysis_nap_count",
    "sodium",
    "step_count",
    "total_fat",
    "walking_running_distance",
}

_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S %z",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%d",
)


@dataclass(frozen=True)
class MetricSample:
    metric: str
    unit: str | None
    timestamp: datetime
    value: float
    source: str | None


@dataclass(frozen=True)
class SleepSession:
    """A single classified sleep session parsed from an Apple Health export.

    Classification rules (applied to sleepStart local time, using the timezone
    offset embedded in the source string — never call .astimezone() before
    reading .hour, as the server may run in a different timezone):
      - sleepStart in [12:00, 20:00) AND ends same calendar day  → nap
      - sleepStart in [12:00, 20:00) AND crosses midnight         → artifact; discard
      - sleepStart outside [12:00, 20:00)                         → main sleep
        (includes morning sleep-ins before noon)

    ``sleep_end`` is used as the timestamp so cross-midnight sessions land on
    the correct wake date. Callers must deduplicate across files by keeping the
    session with the largest ``total_sleep`` for each unique (sleep_end,
    session_type) pair.
    """

    session_type: str  # 'main' or 'nap'
    sleep_end: datetime
    total_sleep: float
    deep: float
    core: float
    rem: float
    awake: float
    unit: str | None
    source: str | None


def resolve_date_range(
    *,
    date_range: str | None,
    start_date: str | None,
    end_date: str | None,
    today: date,
) -> tuple[date, date]:
    if start_date or end_date:
        if not start_date or not end_date:
            raise ValueError("start_date and end_date must be provided together")
        start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    elif date_range:
        normalized = " ".join(date_range.strip().lower().split())
        last_days = re.fullmatch(r"last (\d+) days?", normalized)
        if last_days:
            count = int(last_days.group(1))
            if count < 1:
                raise ValueError("last N days requires N to be at least 1")
            start, end = today - timedelta(days=count - 1), today
        else:
            iso_range = re.fullmatch(r"(\d{4}-\d{2}-\d{2}) through (\d{4}-\d{2}-\d{2})", normalized)
            if iso_range:
                start, end = date.fromisoformat(iso_range.group(1)), date.fromisoformat(
                    iso_range.group(2)
                )
            else:
                named_range = re.fullmatch(
                    r"([a-z]+ \d{1,2})(?:,? (\d{4}))? through ([a-z]+ \d{1,2})(?:,? (\d{4}))?",
                    normalized,
                )
                if not named_range:
                    raise ValueError(
                        "use 'last N days', 'YYYY-MM-DD through YYYY-MM-DD', or start_date/end_date"
                    )
                start_year = int(named_range.group(2) or today.year)
                end_year = int(named_range.group(4) or start_year)
                start = datetime.strptime(
                    f"{named_range.group(1)} {start_year}", "%B %d %Y"
                ).date()
                end = datetime.strptime(
                    f"{named_range.group(3)} {end_year}", "%B %d %Y"
                ).date()
    else:
        raise ValueError("provide date_range or start_date/end_date")

    if start > end:
        raise ValueError("start date must not be after end date")
    return start, end


def iter_sleep_sessions(payload: Any) -> Iterable[SleepSession]:
    """Parse and classify sleep sessions from a raw export payload.

    Yields one :class:`SleepSession` per raw sample that is not discarded as
    an Apple Watch artifact. Multiple exports may contain overlapping
    sub-records of the same night; callers must deduplicate by keeping the
    session with the largest ``total_sleep`` for each (sleep_end, session_type)
    pair.
    """
    if not isinstance(payload, Mapping):
        return
    containers: list[Any] = [payload]
    data = payload.get("data")
    if isinstance(data, Mapping):
        containers.append(data)

    for container in containers:
        raw_metrics = container.get("metrics")
        if not isinstance(raw_metrics, list):
            continue
        for raw_metric in raw_metrics:
            if not isinstance(raw_metric, Mapping):
                continue
            if raw_metric.get("name") != "sleep_analysis":
                continue
            unit = raw_metric.get("units") or raw_metric.get("unit")
            if unit is not None and not isinstance(unit, str):
                unit = str(unit)
            raw_samples = raw_metric.get("data")
            if not isinstance(raw_samples, list):
                continue

            for raw_sample in raw_samples:
                if not isinstance(raw_sample, Mapping):
                    continue
                sleep_start_str = (
                    raw_sample.get("sleepStart") or raw_sample.get("inBedStart")
                )
                sleep_end_str = (
                    raw_sample.get("sleepEnd") or raw_sample.get("inBedEnd")
                )
                if not isinstance(sleep_start_str, str) or not isinstance(sleep_end_str, str):
                    continue
                sleep_start_ts = _parse_timestamp(sleep_start_str)
                sleep_end_ts = _parse_timestamp(sleep_end_str)
                if sleep_start_ts is None or sleep_end_ts is None:
                    continue

                # Classify by sleepStart local time (use embedded tz offset,
                # NOT .astimezone() which converts to server timezone).
                start_hour = sleep_start_ts.hour
                start_local_date = sleep_start_ts.date()
                end_local_date = sleep_end_ts.date()

                if 12 <= start_hour < 20:
                    if start_local_date == end_local_date:
                        session_type = "nap"
                    else:
                        # Cross-midnight daytime-start → Apple Watch artifact; discard.
                        continue
                else:
                    session_type = "main"

                total = raw_sample.get("totalSleep")
                if not isinstance(total, (int, float)) or isinstance(total, bool):
                    continue

                def _f(key: str) -> float:
                    v = raw_sample.get(key, 0.0)
                    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0

                src = raw_sample.get("source")
                yield SleepSession(
                    session_type=session_type,
                    sleep_end=sleep_end_ts,
                    total_sleep=float(total),
                    deep=_f("deep"),
                    core=_f("core"),
                    rem=_f("rem"),
                    awake=_f("awake"),
                    unit=unit,
                    source=src if isinstance(src, str) else None,
                )
        return  # only process the first container that has metrics


def _iter_metrics(payload: Any) -> Iterable[dict[str, Any]]:
    """Yield non-sleep metric data dicts from a raw export payload.

    Sleep sessions are handled separately by :func:`iter_sleep_sessions`.
    Each yielded dict has keys: ``name``, ``unit``, ``samples`` (list of
    :class:`MetricSample`).
    """
    if not isinstance(payload, Mapping):
        return
    containers: list[Any] = [payload]
    data = payload.get("data")
    if isinstance(data, Mapping):
        containers.append(data)

    for container in containers:
        raw_metrics = container.get("metrics")
        if not isinstance(raw_metrics, list):
            continue
        for raw_metric in raw_metrics:
            if not isinstance(raw_metric, Mapping) or not isinstance(raw_metric.get("name"), str):
                continue
            name = raw_metric["name"]
            if name == "sleep_analysis":
                continue  # handled by iter_sleep_sessions()
            unit = raw_metric.get("units") or raw_metric.get("unit")
            if unit is not None and not isinstance(unit, str):
                unit = str(unit)
            raw_samples = raw_metric.get("data")
            samples: list[MetricSample] = []
            if isinstance(raw_samples, list):
                for raw_sample in raw_samples:
                    sample = _parse_sample(name, unit, raw_sample)
                    if sample:
                        samples.append(sample)
            yield {"name": name, "unit": unit, "samples": samples}
        return


def _parse_sample(metric: str, unit: str | None, raw_sample: Any) -> MetricSample | None:
    if not isinstance(raw_sample, Mapping):
        return None
    raw_date = raw_sample.get("date") or raw_sample.get("startDate") or raw_sample.get("start_date")
    if not isinstance(raw_date, str):
        return None
    timestamp = _parse_timestamp(raw_date)
    if timestamp is None:
        return None
    raw_value = raw_sample.get("qty", raw_sample.get("value", raw_sample.get("quantity")))
    if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
        return None
    source = raw_sample.get("source")
    return MetricSample(
        metric=metric,
        unit=unit,
        timestamp=timestamp,
        value=float(raw_value),
        source=source if isinstance(source, str) else None,
    )


def _parse_timestamp(value: str) -> datetime | None:
    for format_string in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(value, format_string)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
        except ValueError:
            continue
    return None
