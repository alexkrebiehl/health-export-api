"""Server-rendered SVG line chart for a daily health metric.

Companion to :mod:`map_page` — a self-contained page for embedding in a Home
Assistant Webpage card. Plain SVG built here rather than a charting library:
the chart is two polylines and a few ticks, and vendoring a chart library would
be far more weight than the drawing it does.

Design notes:

* **Two lines, one measure.** The daily readings are drawn thin in muted ink;
  the moving average sits on top, bold and saturated. They are the same
  quantity at two levels of processing, so this is an emphasis pair rather than
  two competing categorical hues — the raw series is deliberately desaturated
  so the trend dominates.

* **The y axis is zoomed to the data, never zero-based.** Body weight moves a
  few pounds around ~190; anchoring at zero would compress every real movement
  into a flat line.

* **Gaps stay gaps.** The daily line breaks across a hiatus rather than drawing
  a straight segment through days with no reading.

* **No legend and no series labels** — the card heading supplies the context
  and the emphasis difference reads on its own. The hover tooltip carries exact
  values so nothing is unreachable.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from math import ceil, floor, log10
from string import Template
from typing import Any, Sequence

Point = tuple[date, float]

# Gaps up to this many days are drawn through; beyond it the line breaks.
# Weigh-ins are near-daily (observed gaps: 1d x56, 2d x7, 3d x1), so this keeps
# the normal cadence continuous while a real hiatus still shows as a break.
DEFAULT_MAX_GAP_DAYS = 3

# A window narrower than this is just the raw value redrawn, so the average
# only starts once it has something to average.
_MIN_WINDOW_SAMPLES = 2

_W, _H = 1000, 320
_PAD_L, _PAD_R, _PAD_T, _PAD_B = 46, 14, 16, 26


# ---------------------------------------------------------------------------
# Series maths
# ---------------------------------------------------------------------------


def moving_average(
    points: Sequence[Point], window_days: int, *, min_samples: int = _MIN_WINDOW_SAMPLES
) -> list[Point]:
    """Trailing calendar-day mean, anchored to days that have a reading.

    A trailing *calendar* window rather than a trailing N samples: readings are
    near-daily but not every day, and a sample-count window would silently
    stretch across a gap and average over a longer period than advertised.
    """
    if window_days < 1:
        return []
    out: list[Point] = []
    start = 0
    total = 0.0
    for end, (day, value) in enumerate(points):
        total += value
        earliest = day - timedelta(days=window_days - 1)
        while points[start][0] < earliest:
            total -= points[start][1]
            start += 1
        count = end - start + 1
        if count >= min_samples:
            out.append((day, total / count))
    return out


def split_on_gaps(
    points: Sequence[Point], max_gap_days: int = DEFAULT_MAX_GAP_DAYS
) -> list[list[Point]]:
    """Break a series wherever consecutive readings are too far apart."""
    runs: list[list[Point]] = []
    current: list[Point] = []
    for point in points:
        if current and (point[0] - current[-1][0]).days > max_gap_days:
            runs.append(current)
            current = []
        current.append(point)
    if current:
        runs.append(current)
    return runs


def _nice_ticks(low: float, high: float, target: int = 4) -> list[float]:
    """Round tick values covering [low, high]."""
    span = high - low
    if span <= 0:
        return [low]
    rough = span / target
    magnitude = 10 ** floor(log10(rough))
    for multiple in (1, 2, 2.5, 5, 10):
        step = multiple * magnitude
        if rough <= step:
            break
    ticks = []
    value = ceil(low / step) * step
    while value <= high + step * 1e-6:
        ticks.append(round(value, 6))
        value += step
    return ticks


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _parse(series: list[dict[str, Any]]) -> list[Point]:
    points: list[Point] = []
    for row in series:
        value = row.get("value")
        period = row.get("period")
        if value is None or not period:
            continue
        try:
            points.append((date.fromisoformat(period), float(value)))
        except ValueError:
            continue  # month-granularity periods, or malformed rows
    points.sort(key=lambda p: p[0])
    return points


def _path(points: Sequence[Point], sx, sy) -> str:
    return "M " + " L ".join(f"{sx(d):.1f},{sy(v):.1f}" for d, v in points)


_TEMPLATE = Template("""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>$title</title>
<style>
  :root{
    color-scheme: light;
    --surface:#fcfcfb; --ink:#0b0b0b; --ink-2:#52514e; --muted:#898781;
    --grid:#e1e0d9; --axis:#c3c2b7; --raw:#898781; --trend:#2a78d6;
  }
  @media (prefers-color-scheme: dark){
    :root:not([data-theme="light"]){
      color-scheme: dark;
      --surface:#1a1a19; --ink:#ffffff; --ink-2:#c3c2b7; --muted:#898781;
      --grid:#2c2c2a; --axis:#383835; --raw:#898781; --trend:#3987e5;
    }
  }
  :root[data-theme="dark"]{
    color-scheme: dark;
    --surface:#1a1a19; --ink:#ffffff; --ink-2:#c3c2b7; --muted:#898781;
    --grid:#2c2c2a; --axis:#383835; --raw:#898781; --trend:#3987e5;
  }
  html,body{margin:0;height:100%;background:var(--surface);
    font:13px/1.4 system-ui,-apple-system,"Segoe UI",sans-serif;color:var(--ink)}
  /* Fit inside the frame in both axes: a card is not always the chart's
     aspect ratio, and height:auto overflows a short one. */
  #wrap{position:relative;width:100%;height:100%}
  svg{width:100%;height:100%;display:block}
  .tick{fill:var(--muted);font-size:12px;font-variant-numeric:tabular-nums}
  .grid{stroke:var(--grid);stroke-width:1}
  .axis{stroke:var(--axis);stroke-width:1}
  .raw{fill:none;stroke:var(--raw);stroke-width:1.5;
       stroke-linecap:round;stroke-linejoin:round}
  .trend{fill:none;stroke:var(--trend);stroke-width:3;
         stroke-linecap:round;stroke-linejoin:round}
  .cross{stroke:var(--axis);stroke-width:1;opacity:0}
  .dot{fill:var(--trend);stroke:var(--surface);stroke-width:2;opacity:0}
  #tip{position:absolute;pointer-events:none;opacity:0;transform:translate(-50%,-115%);
       background:var(--surface);color:var(--ink);border:1px solid var(--axis);
       border-radius:6px;padding:5px 8px;white-space:nowrap;
       box-shadow:0 2px 8px rgba(0,0,0,.28);font-variant-numeric:tabular-nums}
  #tip b{color:var(--trend)}
  #tip span{color:var(--ink-2)}
  .empty{position:absolute;inset:0;display:flex;align-items:center;
         justify-content:center;color:var(--muted);text-align:center;padding:20px}
</style>
</head>
<body>
<div id="wrap">
$body
</div>
<script type="application/json" id="data">$data</script>
<script>
(function(){
  var d = JSON.parse(document.getElementById('data').textContent);
  if(!d.points.length) return;
  var svg = document.querySelector('svg');
  if(!svg) return;
  var cross = svg.querySelector('.cross'), dot = svg.querySelector('.dot');
  var tip = document.getElementById('tip'), wrap = document.getElementById('wrap');

  // The SVG scales to fit and may be letterboxed, so map through its CTM
  // rather than assuming the drawing fills the element's box.
  function toScreen(x, y){
    var p = svg.createSVGPoint(); p.x = x; p.y = y;
    return p.matrixTransform(svg.getScreenCTM());
  }
  function toViewBoxX(clientX){
    var p = svg.createSVGPoint(); p.x = clientX; p.y = 0;
    return p.matrixTransform(svg.getScreenCTM().inverse()).x;
  }

  function show(e){
    var vx = toViewBoxX(e.clientX);
    var best = 0, bestD = Infinity;
    for (var i = 0; i < d.points.length; i++){
      var dist = Math.abs(d.points[i].x - vx);
      if (dist < bestD){ bestD = dist; best = i; }
    }
    var p = d.points[best], hasTrend = p.ty !== null;
    var y = hasTrend ? p.ty : p.ry;

    cross.setAttribute('x1', p.x); cross.setAttribute('x2', p.x);
    cross.style.opacity = 1;
    dot.setAttribute('cx', p.x); dot.setAttribute('cy', y);
    dot.style.opacity = 1;

    tip.innerHTML = '<span>' + p.label + '</span><br>' + p.rv + ' ' + d.unit +
      (hasTrend ? '<br><b>' + p.tv + ' ' + d.unit + '</b> <span>' + d.window +
                  '-day avg</span>' : '');
    var s = toScreen(p.x, y), w = wrap.getBoundingClientRect();
    tip.style.left = (s.x - w.left) + 'px';
    tip.style.top  = (s.y - w.top) + 'px';
    tip.style.opacity = 1;
  }
  function hide(){ cross.style.opacity = 0; dot.style.opacity = 0; tip.style.opacity = 0; }

  wrap.addEventListener('mousemove', show);
  wrap.addEventListener('mouseleave', hide);
  wrap.addEventListener('touchmove', function(e){
    if (e.touches[0]) show(e.touches[0]);
  }, {passive:true});
})();
</script>
<script>setTimeout(function(){location.reload();}, $refresh_ms);</script>
</body>
</html>
""")


def render_chart_page(
    summary: dict[str, Any],
    *,
    title: str = "Weight",
    window: int = 7,
    refresh_minutes: int = 30,
    max_gap_days: int = DEFAULT_MAX_GAP_DAYS,
) -> str:
    """Render a metric summary as a standalone SVG line chart page."""
    points = _parse(summary.get("series") or [])
    unit = summary.get("unit") or ""

    if not points:
        body = '<div class="empty">No readings in this period.</div>'
        payload = {"points": [], "unit": unit, "window": window}
        return _TEMPLATE.substitute(
            title=title, body=body,
            data=json.dumps(payload).replace("<", "\\u003c"),
            refresh_ms=refresh_minutes * 60_000,
        )

    trend = moving_average(points, window) if window >= 1 else []
    trend_by_day = dict(trend)

    first, last = points[0][0], points[-1][0]
    day_span = max((last - first).days, 1)
    values = [v for _, v in points] + [v for _, v in trend]
    low, high = min(values), max(values)
    # A little headroom so the extremes are not welded to the frame.
    margin = (high - low) * 0.12 or 1.0
    low, high = low - margin, high + margin

    def sx(d: date) -> float:
        return _PAD_L + (d - first).days / day_span * (_W - _PAD_L - _PAD_R)

    def sy(v: float) -> float:
        return _H - _PAD_B - (v - low) / (high - low) * (_H - _PAD_T - _PAD_B)

    parts: list[str] = []

    for tick in _nice_ticks(low, high):
        y = sy(tick)
        parts.append(f'<line class="grid" x1="{_PAD_L}" y1="{y:.1f}" '
                     f'x2="{_W - _PAD_R}" y2="{y:.1f}"/>')
        parts.append(f'<text class="tick" x="{_PAD_L - 8}" y="{y + 4:.1f}" '
                     f'text-anchor="end">{tick:g}</text>')

    # Month starts make honest x ticks over a 90-day window.
    month = date(first.year, first.month, 1)
    while month <= last:
        if month >= first:
            x = sx(month)
            parts.append(f'<line class="axis" x1="{x:.1f}" y1="{_H - _PAD_B}" '
                         f'x2="{x:.1f}" y2="{_H - _PAD_B + 4}"/>')
            parts.append(f'<text class="tick" x="{x:.1f}" y="{_H - _PAD_B + 18}" '
                         f'text-anchor="middle">{month.strftime("%b")}</text>')
        month = date(month.year + (month.month == 12),
                     month.month % 12 + 1, 1)

    parts.append(f'<line class="axis" x1="{_PAD_L}" y1="{_H - _PAD_B}" '
                 f'x2="{_W - _PAD_R}" y2="{_H - _PAD_B}"/>')

    for run in split_on_gaps(points, max_gap_days):
        if len(run) > 1:
            parts.append(f'<path class="raw" d="{_path(run, sx, sy)}"/>')

    for run in split_on_gaps(trend, max_gap_days):
        if len(run) > 1:
            parts.append(f'<path class="trend" d="{_path(run, sx, sy)}"/>')

    parts.append(f'<line class="cross" y1="{_PAD_T}" y2="{_H - _PAD_B}"/>')
    parts.append('<circle class="dot" r="4.5"/>')

    body = (f'<svg viewBox="0 0 {_W} {_H}" role="img" '
            f'aria-label="{title}">{"".join(parts)}</svg>'
            '<div id="tip"></div>')

    payload = {
        "unit": unit,
        "window": window,
        "points": [
            {
                "x": round(sx(d), 1),
                "ry": round(sy(v), 1),
                "ty": round(sy(trend_by_day[d]), 1) if d in trend_by_day else None,
                "rv": f"{v:g}",
                "tv": f"{trend_by_day[d]:.1f}" if d in trend_by_day else None,
                "label": d.strftime("%a %-d %b"),
            }
            for d, v in points
        ],
    }

    return _TEMPLATE.substitute(
        title=title, body=body,
        data=json.dumps(payload, separators=(",", ":")).replace("<", "\\u003c"),
        refresh_ms=refresh_minutes * 60_000,
    )
