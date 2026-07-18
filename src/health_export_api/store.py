"""SQLite-backed store for Apple Health export data.

Architecture
============
- Raw JSON export files on disk remain the source of truth. They are never
  modified and can be used to rebuild the database from scratch.
- On every ``POST /v1/exports`` the ingestion path writes the JSON to disk
  (unchanged) then calls ``Store.ingest()`` to parse and upsert samples into
  SQLite.  ``INSERT OR IGNORE`` on unique constraints makes re-exports and
  duplicate files idempotent.
- On startup, ``Store.backfill()`` scans for any export files not yet recorded
  in ``processed_exports`` and ingests them.  This handles the initial
  migration from the file-only approach and edge cases where the process was
  killed mid-request.
- Query endpoints call SQL instead of loading all files.  Peak memory per
  query is proportional to the result set size, not the total file count.

Schema
======
metric_samples
  One row per unique (metric, ts_iso, value, source) tuple.  Averaged and
  summed in SQL at query time.  ``ts_iso`` stores the ISO-8601 string from
  _parse_timestamp so tz-offset arithmetic stays in Python where the logic
  already lives (the ts_date column carries the YYYY-MM-DD calendar date for
  range filtering without timezone conversion in SQL).

sleep_sessions
  One row per (sleep_end_iso, session_type) pair, keeping the highest
  total_sleep value seen across all export files (cross-file dedup for
  Apple Watch multi-fragment exports).

workout_sessions
  One row per HealthKit workout UUID.  Deduplicated by the ``id`` field
  provided by Health Auto Export.

processed_exports
  Tracks which export_ids have been ingested so backfill can skip them.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from health_export_api.normalization import (
    SleepSession,
    _SUMMED_METRICS,
    _iter_metrics,
    iter_sleep_sessions,
)
from health_export_api.workout_normalization import (
    HEVY_WORKOUT_TYPE,
    _iter_workouts,
    _iter_route_points,
)

log = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS metric_samples (
    metric   TEXT    NOT NULL,
    ts_iso   TEXT    NOT NULL,
    ts_date  TEXT    NOT NULL,   -- YYYY-MM-DD, local date of the sample
    ts_month TEXT    NOT NULL,   -- YYYY-MM
    value    REAL    NOT NULL,
    unit     TEXT,
    source   TEXT,
    UNIQUE (metric, ts_iso, value)
);
CREATE INDEX IF NOT EXISTS idx_ms_metric_date ON metric_samples (metric, ts_date);

CREATE TABLE IF NOT EXISTS sleep_sessions (
    session_type TEXT NOT NULL,  -- 'main' or 'nap'
    sleep_end    TEXT NOT NULL,  -- ISO-8601 of wake time (grouping key)
    sleep_date   TEXT NOT NULL,  -- YYYY-MM-DD of sleep_end
    sleep_month  TEXT NOT NULL,  -- YYYY-MM
    total_sleep  REAL NOT NULL,
    deep         REAL NOT NULL DEFAULT 0,
    core         REAL NOT NULL DEFAULT 0,
    rem          REAL NOT NULL DEFAULT 0,
    awake        REAL NOT NULL DEFAULT 0,
    unit         TEXT,
    source       TEXT,
    PRIMARY KEY (sleep_end, session_type)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_ss_month ON sleep_sessions (session_type, sleep_month);

CREATE TABLE IF NOT EXISTS workout_sessions (
    id               TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    started_iso      TEXT NOT NULL,
    started_date     TEXT NOT NULL,  -- YYYY-MM-DD
    started_month    TEXT NOT NULL,  -- YYYY-MM
    duration_min     REAL NOT NULL DEFAULT 0,
    distance_mi      REAL NOT NULL DEFAULT 0,
    active_energy    REAL NOT NULL DEFAULT 0,
    avg_heart_rate   REAL,         -- nullable: not all workouts have HR
    has_route        INTEGER NOT NULL DEFAULT 0  -- 1 if route data exists
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_ws_name_date ON workout_sessions (name, started_date);

CREATE TABLE IF NOT EXISTS workout_routes (
    workout_id          TEXT NOT NULL,
    point_index         INTEGER NOT NULL,
    timestamp           TEXT NOT NULL,      -- ISO-8601
    latitude            REAL NOT NULL,
    longitude           REAL NOT NULL,
    altitude            REAL,               -- meters
    horizontal_accuracy REAL,               -- meters
    vertical_accuracy   REAL,               -- meters
    speed               REAL,               -- m/s
    speed_accuracy      REAL,               -- m/s
    course              REAL,               -- degrees
    course_accuracy     REAL,               -- degrees
    PRIMARY KEY (workout_id, point_index),
    FOREIGN KEY (workout_id) REFERENCES workout_sessions(id) ON DELETE CASCADE
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_wr_workout ON workout_routes (workout_id);

CREATE TABLE IF NOT EXISTS processed_exports (
    export_id   TEXT PRIMARY KEY,
    received_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL
) WITHOUT ROWID;
"""


class Store:
    """Thread-safe SQLite store using WAL mode and per-request connections."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        # Run DDL once at startup on a dedicated connection.
        con = self._connect()
        try:
            con.executescript(_DDL)
            con.commit()
        finally:
            con.close()

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self._db_path, check_same_thread=False)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA foreign_keys=ON")
        # Keep temp tables in memory — avoids disk I/O errors when SQLite tries
        # to write sort/aggregation temp files to /tmp on container overlay fs.
        con.execute("PRAGMA temp_store=MEMORY")
        con.row_factory = sqlite3.Row
        return con

    # ------------------------------------------------------------------
    # Ingestion
    # ------------------------------------------------------------------

    def ingest(self, export_id: str, received_at: str, payload: Any) -> None:
        """Parse payload and upsert all samples into SQLite.

        Idempotent: re-ingesting the same export_id is a no-op.
        """
        con = self._connect()
        try:
            with con:
                # Skip if already processed.
                row = con.execute(
                    "SELECT 1 FROM processed_exports WHERE export_id = ?", (export_id,)
                ).fetchone()
                if row:
                    return

                now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
                self._ingest_metrics(con, payload)
                self._ingest_sleep(con, payload)
                self._ingest_workouts(con, payload)
                con.execute(
                    "INSERT OR IGNORE INTO processed_exports VALUES (?, ?, ?)",
                    (export_id, received_at, now),
                )
        finally:
            con.close()

    def _ingest_metrics(self, con: sqlite3.Connection, payload: Any) -> None:
        rows = []
        for metric_data in _iter_metrics(payload):
            for sample in metric_data["samples"]:
                ts = sample.timestamp
                rows.append((
                    sample.metric,
                    ts.isoformat(),
                    ts.date().isoformat(),
                    ts.strftime("%Y-%m"),
                    sample.value,
                    sample.unit,
                    sample.source,
                ))
        if rows:
            con.executemany(
                "INSERT OR IGNORE INTO metric_samples "
                "(metric, ts_iso, ts_date, ts_month, value, unit, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    def _ingest_sleep(self, con: sqlite3.Connection, payload: Any) -> None:
        # Collect sessions, dedup by (sleep_end, session_type) keeping max total_sleep.
        best: dict[tuple[str, str], SleepSession] = {}
        for sess in iter_sleep_sessions(payload):
            key = (sess.sleep_end.isoformat(), sess.session_type)
            existing = best.get(key)
            if existing is None or sess.total_sleep > existing.total_sleep:
                best[key] = sess

        for (end_iso, stype), sess in best.items():
            end_ts = sess.sleep_end
            # Upsert: if a better (higher total_sleep) record arrives from a
            # later export file, replace it.
            con.execute(
                """
                INSERT INTO sleep_sessions
                    (session_type, sleep_end, sleep_date, sleep_month,
                     total_sleep, deep, core, rem, awake, unit, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(sleep_end, session_type) DO UPDATE SET
                    total_sleep = MAX(total_sleep, excluded.total_sleep),
                    deep  = CASE WHEN excluded.total_sleep > total_sleep THEN excluded.deep  ELSE deep  END,
                    core  = CASE WHEN excluded.total_sleep > total_sleep THEN excluded.core  ELSE core  END,
                    rem   = CASE WHEN excluded.total_sleep > total_sleep THEN excluded.rem   ELSE rem   END,
                    awake = CASE WHEN excluded.total_sleep > total_sleep THEN excluded.awake ELSE awake END
                """,
                (
                    stype,
                    end_iso,
                    end_ts.date().isoformat(),
                    end_ts.strftime("%Y-%m"),
                    sess.total_sleep,
                    sess.deep,
                    sess.core,
                    sess.rem,
                    sess.awake,
                    sess.unit,
                    sess.source,
                ),
            )

    def _ingest_workouts(self, con: sqlite3.Connection, payload: Any) -> None:
        # _iter_workouts expects an iterable of records; wrap payload in a
        # synthetic record so we can reuse the existing parser unchanged.
        fake_record = {"payload": payload}
        rows = []
        for sess in _iter_workouts([fake_record]):
            started = sess.started
            rows.append((
                sess.id,
                sess.name,
                started.isoformat(),
                started.date().isoformat(),
                started.strftime("%Y-%m"),
                sess.duration_min,
                sess.distance_mi,
                sess.active_energy_kcal,
                sess.avg_heart_rate,
                1 if sess.has_route else 0,
            ))
        if rows:
            con.executemany(
                "INSERT OR IGNORE INTO workout_sessions "
                "(id, name, started_iso, started_date, started_month, "
                " duration_min, distance_mi, active_energy, avg_heart_rate, has_route) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        
        # Ingest route points
        route_rows = []
        for point in _iter_route_points([fake_record]):
            route_rows.append((
                point.workout_id,
                point.point_index,
                point.timestamp.isoformat(),
                point.latitude,
                point.longitude,
                point.altitude,
                point.horizontal_accuracy,
                point.vertical_accuracy,
                point.speed,
                point.speed_accuracy,
                point.course,
                point.course_accuracy,
            ))
        
        if route_rows:
            con.executemany(
                "INSERT OR IGNORE INTO workout_routes "
                "(workout_id, point_index, timestamp, latitude, longitude, "
                " altitude, horizontal_accuracy, vertical_accuracy, speed, "
                " speed_accuracy, course, course_accuracy) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                route_rows,
            )

    # ------------------------------------------------------------------
    # Startup backfill
    # ------------------------------------------------------------------

    def backfill(self, exports_dir: Path) -> None:
        """Ingest any export files not yet in processed_exports.

        Runs once at startup to handle the initial migration from file-only
        storage and any files written when the process was down.
        """
        con = self._connect()
        try:
            done = {
                row[0]
                for row in con.execute("SELECT export_id FROM processed_exports")
            }
        finally:
            con.close()

        files = sorted(exports_dir.glob("*.json"))
        pending = [f for f in files if f.stem not in done]
        if not pending:
            return

        log.info("Backfilling %d export file(s) into SQLite…", len(pending))
        for path in pending:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                log.warning("Skipping unreadable export %s: %s", path.name, exc)
                continue
            export_id = record.get("id", path.stem)
            received_at = record.get("received_at", "")
            payload = record.get("payload")
            try:
                self.ingest(export_id, received_at, payload)
            except Exception as exc:
                log.warning("Failed to ingest %s: %s", export_id, exc)
        log.info("Backfill complete.")

    # ------------------------------------------------------------------
    # Query — health metrics
    # ------------------------------------------------------------------

    def available_metrics(self) -> list[dict[str, str | None]]:
        con = self._connect()
        try:
            # Metric samples table covers all non-sleep metrics.
            rows = con.execute(
                "SELECT DISTINCT metric, unit FROM metric_samples ORDER BY metric"
            ).fetchall()
            metrics = {r["metric"]: r["unit"] for r in rows}
            # Sleep sub-metrics: derive from what's in sleep_sessions.
            has_main = con.execute(
                "SELECT 1 FROM sleep_sessions WHERE session_type='main' LIMIT 1"
            ).fetchone()
            has_nap = con.execute(
                "SELECT 1 FROM sleep_sessions WHERE session_type='nap' LIMIT 1"
            ).fetchone()
            if has_main:
                for sub in ("sleep_analysis", "sleep_analysis_deep",
                            "sleep_analysis_core", "sleep_analysis_rem",
                            "sleep_analysis_awake"):
                    metrics.setdefault(sub, "hr")
            if has_nap:
                metrics.setdefault("sleep_analysis_nap", "hr")
                metrics.setdefault("sleep_analysis_nap_count", "count")
            return [{"metric": k, "unit": v} for k, v in sorted(metrics.items())]
        finally:
            con.close()

    def summarize_metric(
        self,
        *,
        metric: str,
        start_date: date,
        end_date: date,
        granularity: str,
    ) -> dict[str, Any]:
        if granularity not in {"day", "month"}:
            raise ValueError("granularity must be 'day' or 'month'")

        period_expr = (
            "ts_date" if granularity == "day" else "ts_month"
        )
        is_sum = metric in _SUMMED_METRICS
        agg_fn = "SUM" if is_sum else "AVG"
        aggregation = "sum" if is_sum else "average"

        start_s = start_date.isoformat()
        end_s = end_date.isoformat()

        # --- Sleep sub-metrics ---
        sleep_col, sleep_type = _sleep_metric_col(metric)
        if sleep_col:
            con = self._connect()
            try:
                unit_row = con.execute(
                    "SELECT unit FROM sleep_sessions WHERE session_type=? LIMIT 1",
                    (sleep_type,),
                ).fetchone()
                unit = unit_row["unit"] if unit_row else "hr"

                period_col = "sleep_date" if granularity == "day" else "sleep_month"
                if metric == "sleep_analysis_nap_count":
                    # Count distinct nap sessions per period.
                    rows = con.execute(
                        f"""
                        SELECT {period_col} AS period, COUNT(*) AS n, COUNT(*) AS val
                        FROM sleep_sessions
                        WHERE session_type='nap'
                          AND sleep_date BETWEEN ? AND ?
                        GROUP BY {period_col}
                        ORDER BY {period_col}
                        """,
                        (start_s, end_s),
                    ).fetchall()
                    unit = "count"
                elif metric == "sleep_analysis_nap":
                    rows = con.execute(
                        f"""
                        SELECT {period_col} AS period, COUNT(*) AS n,
                               AVG(total_sleep) AS val
                        FROM sleep_sessions
                        WHERE session_type='nap'
                          AND sleep_date BETWEEN ? AND ?
                        GROUP BY {period_col}
                        ORDER BY {period_col}
                        """,
                        (start_s, end_s),
                    ).fetchall()
                else:
                    rows = con.execute(
                        f"""
                        SELECT {period_col} AS period, COUNT(*) AS n,
                               AVG({sleep_col}) AS val
                        FROM sleep_sessions
                        WHERE session_type='main'
                          AND sleep_date BETWEEN ? AND ?
                        GROUP BY {period_col}
                        ORDER BY {period_col}
                        """,
                        (start_s, end_s),
                    ).fetchall()

                metric_found = bool(rows)
                series = [
                    {
                        "period": r["period"],
                        "sample_count": r["n"],
                        "value": round(r["val"], 4),
                    }
                    for r in rows
                ]
                return {
                    "metric": metric,
                    "unit": unit,
                    "aggregation": "sum" if metric == "sleep_analysis_nap_count" else "average",
                    "granularity": granularity,
                    "start_date": start_s,
                    "end_date": end_s,
                    "metric_found": metric_found,
                    "series": series,
                }
            finally:
                con.close()

        # --- Regular metrics ---
        con = self._connect()
        try:
            unit_row = con.execute(
                "SELECT unit FROM metric_samples WHERE metric=? LIMIT 1", (metric,)
            ).fetchone()
            unit = unit_row["unit"] if unit_row else None

            rows = con.execute(
                f"""
                SELECT {period_expr} AS period,
                       COUNT(*)        AS n,
                       {agg_fn}(value) AS val
                FROM metric_samples
                WHERE metric  = ?
                  AND ts_date BETWEEN ? AND ?
                GROUP BY {period_expr}
                ORDER BY {period_expr}
                """,
                (metric, start_s, end_s),
            ).fetchall()

            metric_found = bool(
                con.execute(
                    "SELECT 1 FROM metric_samples WHERE metric=? LIMIT 1", (metric,)
                ).fetchone()
            )
            series = [
                {
                    "period": r["period"],
                    "sample_count": r["n"],
                    "value": round(r["val"], 4),
                }
                for r in rows
            ]
            return {
                "metric": metric,
                "unit": unit,
                "aggregation": aggregation,
                "granularity": granularity,
                "start_date": start_s,
                "end_date": end_s,
                "metric_found": metric_found,
                "series": series,
            }
        finally:
            con.close()

    # ------------------------------------------------------------------
    # Query — workouts
    # ------------------------------------------------------------------

    def available_workout_types(
        self, *, include_hevy: bool = False
    ) -> list[dict[str, Any]]:
        con = self._connect()
        try:
            if include_hevy:
                rows = con.execute(
                    "SELECT name, COUNT(*) AS n FROM workout_sessions GROUP BY name ORDER BY name"
                ).fetchall()
            else:
                rows = con.execute(
                    "SELECT name, COUNT(*) AS n FROM workout_sessions "
                    "WHERE name != ? GROUP BY name ORDER BY name",
                    (HEVY_WORKOUT_TYPE,),
                ).fetchall()
            return [{"name": r["name"], "session_count": r["n"]} for r in rows]
        finally:
            con.close()

    def summarize_workouts(
        self,
        *,
        start_date: date,
        end_date: date,
        granularity: str,
        workout_type: str | None = None,
        include_hevy: bool = False,
    ) -> dict[str, Any]:
        if granularity not in {"day", "month"}:
            raise ValueError("granularity must be 'day' or 'month'")

        period_col = "started_date" if granularity == "day" else "started_month"
        start_s = start_date.isoformat()
        end_s = end_date.isoformat()

        filters = ["started_date BETWEEN ? AND ?"]
        params: list[Any] = [start_s, end_s]

        if not include_hevy:
            filters.append("name != ?")
            params.append(HEVY_WORKOUT_TYPE)
        if workout_type is not None:
            filters.append("name = ?")
            params.append(workout_type)

        where = " AND ".join(filters)

        con = self._connect()
        try:
            rows = con.execute(
                f"""
                SELECT {period_col}            AS period,
                       COUNT(*)                AS sessions,
                       SUM(duration_min)       AS dur_min,
                       SUM(distance_mi)        AS dist_mi,
                       SUM(active_energy)      AS ae_kcal,
                       SUM(avg_heart_rate)     AS hr_sum,
                       COUNT(avg_heart_rate)   AS hr_count
                FROM workout_sessions
                WHERE {where}
                GROUP BY {period_col}
                ORDER BY {period_col}
                """,
                params,
            ).fetchall()
        finally:
            con.close()

        series = [
            {
                "period": r["period"],
                "sessions": r["sessions"],
                "total_duration_min": round(r["dur_min"] or 0.0, 1),
                "total_distance_mi": round(r["dist_mi"] or 0.0, 2),
                "total_active_energy_kcal": round(r["ae_kcal"] or 0.0, 1),
                "avg_heart_rate": (
                    round(r["hr_sum"] / r["hr_count"], 1)
                    if r["hr_count"]
                    else None
                ),
            }
            for r in rows
        ]
        return {
            "workout_type": workout_type,
            "granularity": granularity,
            "start_date": start_s,
            "end_date": end_s,
            "include_hevy": include_hevy,
            "series": series,
        }

    def get_workout_route(
        self, workout_id: str, *, max_points: int | None = None
    ) -> dict[str, Any]:
        """Retrieve GPS route data for a specific workout.
        
        Args:
            workout_id: The HealthKit workout UUID
            max_points: Optional limit on number of points returned (for large routes)
        
        Returns:
            Dictionary with workout metadata and route points
        """
        con = self._connect()
        try:
            # Get workout metadata
            workout_row = con.execute(
                "SELECT id, name, started_iso, duration_min, distance_mi, "
                "       active_energy, avg_heart_rate, has_route "
                "FROM workout_sessions WHERE id = ?",
                (workout_id,),
            ).fetchone()
            
            if not workout_row:
                return {
                    "error": "Workout not found",
                    "workout_id": workout_id,
                }
            
            if not workout_row["has_route"]:
                return {
                    "workout_id": workout_id,
                    "name": workout_row["name"],
                    "started": workout_row["started_iso"],
                    "has_route": False,
                    "route_points": [],
                }
            
            # Get route points
            limit_clause = f"LIMIT {max_points}" if max_points else ""
            route_rows = con.execute(
                f"""
                SELECT point_index, timestamp, latitude, longitude, altitude,
                       horizontal_accuracy, vertical_accuracy, speed,
                       speed_accuracy, course, course_accuracy
                FROM workout_routes
                WHERE workout_id = ?
                ORDER BY point_index
                {limit_clause}
                """,
                (workout_id,),
            ).fetchall()
            
            route_points = [
                {
                    "index": r["point_index"],
                    "timestamp": r["timestamp"],
                    "latitude": r["latitude"],
                    "longitude": r["longitude"],
                    "altitude": r["altitude"],
                    "horizontal_accuracy": r["horizontal_accuracy"],
                    "vertical_accuracy": r["vertical_accuracy"],
                    "speed": r["speed"],
                    "speed_accuracy": r["speed_accuracy"],
                    "course": r["course"],
                    "course_accuracy": r["course_accuracy"],
                }
                for r in route_rows
            ]
            
            # Get total point count if we're limiting
            total_points = len(route_points)
            if max_points:
                count_row = con.execute(
                    "SELECT COUNT(*) as cnt FROM workout_routes WHERE workout_id = ?",
                    (workout_id,),
                ).fetchone()
                total_points = count_row["cnt"]
            
            return {
                "workout_id": workout_id,
                "name": workout_row["name"],
                "started": workout_row["started_iso"],
                "duration_min": workout_row["duration_min"],
                "distance_mi": workout_row["distance_mi"],
                "active_energy_kcal": workout_row["active_energy"],
                "avg_heart_rate": workout_row["avg_heart_rate"],
                "has_route": True,
                "total_points": total_points,
                "returned_points": len(route_points),
                "route_points": route_points,
            }
        finally:
            con.close()


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _sleep_metric_col(metric: str) -> tuple[str | None, str | None]:
    """Return (column_name, session_type) for a sleep sub-metric, or (None, None)."""
    mapping = {
        "sleep_analysis":       ("total_sleep", "main"),
        "sleep_analysis_deep":  ("deep",        "main"),
        "sleep_analysis_core":  ("core",        "main"),
        "sleep_analysis_rem":   ("rem",         "main"),
        "sleep_analysis_awake": ("awake",       "main"),
        "sleep_analysis_nap":   ("total_sleep", "nap"),
        "sleep_analysis_nap_count": ("total_sleep", "nap"),
    }
    col, stype = mapping.get(metric, (None, None))
    return col, stype
