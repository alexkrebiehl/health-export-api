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

    # Sleep metrics: multiple export files may each carry a different sub-record
    # (stage-transition fragment) of the same session. All fragments share the same
    # sleepEnd timestamp, so deduplicate by keeping the maximum value per timestamp
    # (the outermost/longest session for each unique sleepEnd).
    if metric.startswith("sleep_analysis"):
        by_ts: dict[datetime, MetricSample] = {}
        for s in samples:
            if s.timestamp not in by_ts or s.value > by_ts[s.timestamp].value:
                by_ts[s.timestamp] = s
        samples = list(by_ts.values())

    # For sleep_analysis (main night) and sleep_analysis_nap / sleep_analysis_nap_count:
    # group all sessions by wake date (timestamp.date()), then classify the longest
    # session as main sleep and all shorter sessions on that same date as naps.
    if metric in ("sleep_analysis", "sleep_analysis_nap", "sleep_analysis_nap_count"):
        # Group samples by wake date
        by_date: dict[date, list[MetricSample]] = defaultdict(list)
        for s in samples:
            by_date[s.timestamp.date()].append(s)

        main_samples: list[MetricSample] = []
        nap_samples: list[MetricSample] = []
        nap_count_samples: list[MetricSample] = []

        for wake_date, day_samples in by_date.items():
            # Longest session = main sleep
            day_samples_sorted = sorted(day_samples, key=lambda s: s.value, reverse=True)
            main_samples.append(day_samples_sorted[0])
            for nap in day_samples_sorted[1:]:
                nap_samples.append(MetricSample(
                    metric="sleep_analysis_nap",
                    unit=nap.unit,
                    timestamp=nap.timestamp,
                    value=nap.value,
                    source=nap.source,
                ))
                nap_count_samples.append(MetricSample(
                    metric="sleep_analysis_nap_count",
                    unit="count",
                    timestamp=nap.timestamp,
                    value=1.0,
                    source=nap.source,
                ))

        if metric == "sleep_analysis":
            samples = main_samples
        elif metric == "sleep_analysis_nap":
            samples = nap_samples
        else:  # sleep_analysis_nap_count
            samples = nap_count_samples

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
                # Expands into seven queryable sub-metrics:
                #   sleep_analysis          — main night's totalSleep (hr, average)
                #   sleep_analysis_deep     — deep sleep (hr, average)
                #   sleep_analysis_core     — core sleep (hr, average)
                #   sleep_analysis_rem      — REM sleep (hr, average)
                #   sleep_analysis_awake    — awake time (hr, average)
                #   sleep_analysis_nap      — nap duration (hr, average)
                #   sleep_analysis_nap_count — nap count (count, sum)
                #
                # Apple Watch sometimes emits overlapping sub-records for the same
                # session (stage-transition fragments sharing the same sleepEnd).
                # Deduplicate within each export file by keeping only the record
                # with the earliest sleepStart per unique sleepEnd.
                #
                # Main sleep vs nap classification: group deduplicated sessions by
                # wake date (sleepEnd.date()). The session with the greatest
                # totalSleep on each wake date is the main sleep; any remaining
                # sessions on that same wake date are naps.
                _SLEEP_STAGE_FIELDS = {
                    "sleep_analysis": "totalSleep",
                    "sleep_analysis_deep": "deep",
                    "sleep_analysis_core": "core",
                    "sleep_analysis_rem": "rem",
                    "sleep_analysis_awake": "awake",
                }
                _ALL_SUB_METRICS = list(_SLEEP_STAGE_FIELDS) + [
                    "sleep_analysis_nap",
                    "sleep_analysis_nap_count",
                ]
                # First pass: deduplicate by sleepEnd within this export file,
                # keeping the record with the earliest sleepStart (longest session).
                best: dict[str, Any] = {}  # sleepEnd_str -> raw_sample
                for raw_sample in raw_samples:
                    if not isinstance(raw_sample, Mapping):
                        continue
                    sleep_end = raw_sample.get("sleepEnd") or raw_sample.get("inBedEnd")
                    if not isinstance(sleep_end, str):
                        continue
                    sleep_start_str = (
                        raw_sample.get("sleepStart") or raw_sample.get("inBedStart") or ""
                    )
                    existing = best.get(sleep_end)
                    if existing is None:
                        best[sleep_end] = raw_sample
                    else:
                        existing_start = (
                            existing.get("sleepStart") or existing.get("inBedStart") or ""
                        )
                        if sleep_start_str < existing_start:
                            best[sleep_end] = raw_sample
                # Second pass: emit MetricSamples, tagging each with its sleepEnd
                # date so the caller can perform cross-file main/nap classification.
                sub: dict[str, list[MetricSample]] = {k: [] for k in _ALL_SUB_METRICS}
                for raw_sample in best.values():
                    raw_date = (
                        raw_sample.get("date")
                        or raw_sample.get("sleepStart")
                        or raw_sample.get("startDate")
                    )
                    sleep_end_str = (
                        raw_sample.get("sleepEnd") or raw_sample.get("inBedEnd") or raw_date
                    )
                    if not isinstance(raw_date, str) or not isinstance(sleep_end_str, str):
                        continue
                    # Use sleepEnd as the timestamp so wake-date grouping works
                    # correctly for sessions that cross midnight.
                    timestamp = _parse_timestamp(sleep_end_str)
                    if timestamp is None:
                        timestamp = _parse_timestamp(raw_date)
                    if timestamp is None:
                        continue
                    src = raw_sample.get("source")
                    source = src if isinstance(src, str) else None
                    total = raw_sample.get("totalSleep")
                    if not isinstance(total, (int, float)) or isinstance(total, bool):
                        continue
                    for sub_metric, field in _SLEEP_STAGE_FIELDS.items():
                        raw_value = raw_sample.get(field)
                        if isinstance(raw_value, (int, float)) and not isinstance(
                            raw_value, bool
                        ):
                            sub[sub_metric].append(
                                MetricSample(
                                    metric=sub_metric,
                                    unit=unit,
                                    timestamp=timestamp,
                                    value=float(raw_value),
                                    source=source,
                                )
                            )
                    # Nap fields — populated during cross-file dedup in summarize_metric.
                    # Emit a placeholder so the metric name appears in the catalog.
                    sub["sleep_analysis_nap"].append(
                        MetricSample(
                            metric="sleep_analysis_nap",
                            unit=unit,
                            timestamp=timestamp,
                            value=float(total),
                            source=source,
                        )
                    )
                    sub["sleep_analysis_nap_count"].append(
                        MetricSample(
                            metric="sleep_analysis_nap_count",
                            unit="count",
                            timestamp=timestamp,
                            value=1.0,
                            source=source,
                        )
                    )
                for sub_metric, samples in sub.items():
                    yield {"name": sub_metric, "unit": unit if sub_metric != "sleep_analysis_nap_count" else "count", "samples": samples}
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
