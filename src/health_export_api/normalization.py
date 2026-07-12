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


def summarize_metric(
    records: Iterable[Mapping[str, Any]],
    *,
    metric: str,
    start_date: date,
    end_date: date,
    granularity: str,
) -> dict[str, Any]:
    if granularity not in {"day", "month"}:
        raise ValueError("granularity must be 'day' or 'month'")

    metric_found = False
    unit: str | None = None
    samples: list[MetricSample] = []
    seen: set[tuple[str, datetime, float, str | None]] = set()

    for record in records:
        payload = record.get("payload")
        for metric_data in _iter_metrics(payload):
            if metric_data["name"] != metric:
                continue
            metric_found = True
            unit = unit or metric_data["unit"]
            for sample in metric_data["samples"]:
                if not start_date <= sample.timestamp.date() <= end_date:
                    continue
                identity = (sample.metric, sample.timestamp, sample.value, sample.source)
                if identity not in seen:
                    seen.add(identity)
                    samples.append(sample)

    aggregation = "sum" if metric in _SUMMED_METRICS else "average"
    groups: dict[str, list[float]] = defaultdict(list)
    for sample in samples:
        period = (
            sample.timestamp.date().isoformat()
            if granularity == "day"
            else sample.timestamp.strftime("%Y-%m")
        )
        groups[period].append(sample.value)

    series = [
        {
            "period": period,
            "sample_count": len(values),
            "value": sum(values) if aggregation == "sum" else sum(values) / len(values),
        }
        for period, values in sorted(groups.items())
    ]
    return {
        "metric": metric,
        "unit": unit,
        "aggregation": aggregation,
        "granularity": granularity,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "metric_found": metric_found,
        "series": series,
    }


def available_metrics(records: Iterable[Mapping[str, Any]]) -> list[dict[str, str | None]]:
    metrics: dict[str, str | None] = {}
    for record in records:
        for metric_data in _iter_metrics(record.get("payload")):
            metrics.setdefault(metric_data["name"], metric_data["unit"])
    return [
        {"metric": name, "unit": metrics[name]}
        for name in sorted(metrics)
    ]


def _iter_metrics(payload: Any) -> Iterable[dict[str, Any]]:
    if not isinstance(payload, Mapping):
        return
    containers = [payload]
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
            unit = raw_metric.get("units") or raw_metric.get("unit")
            if unit is not None and not isinstance(unit, str):
                unit = str(unit)
            raw_samples = raw_metric.get("data")
            if name == "sleep_analysis" and isinstance(raw_samples, list):
                # Sleep samples carry per-stage fields rather than a single qty/value.
                # Expand into: sleep_analysis (totalSleep) + per-stage sub-metrics.
                #
                # Apple Watch sometimes emits overlapping sub-records for a single
                # night — e.g. the full session plus 4 shorter stage-transition
                # fragments that all share the same sleepEnd. De-duplicate by keeping
                # only the record with the earliest sleepStart per unique sleepEnd
                # (i.e. the outermost / longest record for each session).
                _SLEEP_FIELDS = {
                    "sleep_analysis": "totalSleep",
                    "sleep_analysis_deep": "deep",
                    "sleep_analysis_core": "core",
                    "sleep_analysis_rem": "rem",
                    "sleep_analysis_awake": "awake",
                }
                # First pass: deduplicate by sleepEnd, keeping earliest sleepStart.
                best: dict[str, Any] = {}  # sleepEnd_str -> raw_sample
                for raw_sample in raw_samples:
                    if not isinstance(raw_sample, Mapping):
                        continue
                    sleep_end = raw_sample.get("sleepEnd") or raw_sample.get("inBedEnd")
                    if not isinstance(sleep_end, str):
                        continue
                    sleep_start_str = raw_sample.get("sleepStart") or raw_sample.get("inBedStart") or ""
                    existing = best.get(sleep_end)
                    if existing is None:
                        best[sleep_end] = raw_sample
                    else:
                        existing_start = existing.get("sleepStart") or existing.get("inBedStart") or ""
                        if sleep_start_str < existing_start:
                            best[sleep_end] = raw_sample
                # Second pass: emit MetricSamples from deduplicated records.
                sub: dict[str, list[MetricSample]] = {k: [] for k in _SLEEP_FIELDS}
                for raw_sample in best.values():
                    raw_date = (
                        raw_sample.get("date")
                        or raw_sample.get("sleepStart")
                        or raw_sample.get("startDate")
                    )
                    if not isinstance(raw_date, str):
                        continue
                    timestamp = _parse_timestamp(raw_date)
                    if timestamp is None:
                        continue
                    src = raw_sample.get("source")
                    source = src if isinstance(src, str) else None
                    for sub_metric, field in _SLEEP_FIELDS.items():
                        raw_value = raw_sample.get(field)
                        if isinstance(raw_value, (int, float)) and not isinstance(raw_value, bool):
                            sub[sub_metric].append(
                                MetricSample(
                                    metric=sub_metric,
                                    unit=unit,
                                    timestamp=timestamp,
                                    value=float(raw_value),
                                    source=source,
                                )
                            )
                for sub_metric, samples in sub.items():
                    yield {"name": sub_metric, "unit": unit, "samples": samples}
            else:
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
