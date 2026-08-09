"""Server-rendered SVG line chart for a daily health metric.

Companion to :mod:`map_page` — a self-contained page for embedding in a Home
Assistant Webpage card. Plain SVG built here rather than a charting library:
the chart is two polylines and a few ticks, and vendoring a chart library would
be far more weight than the drawing it does.

Design notes:

* **Two lines, one measure.** The daily readings are drawn thin in muted ink;
  the rolling trend sits on top, bold and saturated. They are the same
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

from health_export_api.theme import FONT_STACK, PALETTE_CSS

Point = tuple[date, float]

# Gaps up to this many days are drawn through; beyond it the line breaks.
# Weigh-ins are near-daily (observed gaps: 1d x56, 2d x7, 3d x1), so this keeps
# the normal cadence continuous while a real hiatus still shows as a break.
DEFAULT_MAX_GAP_DAYS = 3

# Least squares through two points is just the segment joining them, which
# would draw the trend on top of the raw line and say nothing. Three is the
# smallest window where the fit actually does any fitting.
_MIN_FIT_POINTS = 3

_W, _H = 1000, 320
_PAD_R, _PAD_T = 14, 16

# Neither tick gutter lives in the viewBox, because a viewBox pad scales with
# the card and the labels do not. A 46-unit left pad is 48px on a 1040px card
# and 18px on a 380px one, while "15,000" stays 43px wide; a 26-unit bottom pad
# is 26px on a 320px-tall card and 11px on a 130px one, while "11 Jul" stays
# ~17px tall. Either way the plot ends up drawn over its own labels. So both
# gutters are CSS pixels and #plot is inset by them, which cannot collapse.
_YGUT_GAP = 8.0    # between the y labels and the plot's left edge
_XGUT = 21.0       # the strip below the plot that the x labels sit in
_XGUT_GAP = 3.0    # between the plot's bottom edge and the x labels

# Tooltip placement, in CSS pixels: how far it stays clear of the frame edge,
# and how far it sits from the point it describes.
_TIP_EDGE, _TIP_GAP = 4, 10

# Vertical space between stacked panels, and the span at which x ticks switch
# from month starts to evenly spaced days.
_PANEL_GAP = 22
_MONTH_TICK_SPAN_DAYS = 70

# A bar takes this share of its day's slot; the remainder is the gap to its
# neighbour, which is what keeps thirty bars legible as thirty rather than a
# solid block.
_BAR_SLOT_FILL = 0.62

# Rounded data-end, in viewBox units. Both radii are set because
# `preserveAspectRatio="none"` scales the axes independently — a single value
# would come out as an ellipse. These are sized to read as ~4px at a dashboard
# card's proportions; the corner is decoration, so drifting a pixel is fine.
_BAR_RX, _BAR_RY = 3.6, 3.2


# ---------------------------------------------------------------------------
# Series maths
# ---------------------------------------------------------------------------


def rolling_trend(
    points: Sequence[Point], window_days: int, *, min_points: int = _MIN_FIT_POINTS
) -> list[Point]:
    """Rolling least-squares fit, evaluated at the right edge of each window.

    At each day with a reading, fit a straight line through the readings in the
    trailing window and take that line's value *at that day*. Unlike a moving
    average this follows the local slope rather than averaging it away, so it
    turns with the data instead of lagging half a window behind it.

    A trailing *calendar* window rather than a trailing N points: readings are
    near-daily but not every day, and a point-count window would silently
    stretch across a gap and fit over a longer period than advertised.

    Days are measured from the first reading, so the regression works in small
    numbers regardless of how far into the epoch the dates sit.
    """
    if window_days < 1 or not points:
        return []

    origin = points[0][0]
    xs = [(day - origin).days for day, _ in points]

    out: list[Point] = []
    start = 0
    for end, (day, _) in enumerate(points):
        earliest = day - timedelta(days=window_days - 1)
        while points[start][0] < earliest:
            start += 1
        count = end - start + 1
        if count < min_points:
            continue

        window_x = xs[start : end + 1]
        window_y = [v for _, v in points[start : end + 1]]
        mean_x = sum(window_x) / count
        mean_y = sum(window_y) / count
        sxx = sum((x - mean_x) ** 2 for x in window_x)
        if sxx == 0:
            # Every reading on one day; a slope is undefined, so use the level.
            out.append((day, mean_y))
            continue
        sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(window_x, window_y))
        slope = sxy / sxx
        out.append((day, mean_y + slope * (xs[end] - mean_x)))
    return out


def window_change(
    points: Sequence[Point], window_days: int, anchor: date
) -> tuple[float, float, float] | None:
    """Week-over-week change: mean of the recent window minus the one before.

    Both windows are the same length in **calendar days** and adjacent, ending
    at ``anchor``: ``[anchor-(w-1), anchor]`` against ``[anchor-(2w-1),
    anchor-w]``. Equal spans matter — unequal ones would weight the two means
    differently and bias the comparison.

    Returns ``(recent_mean, prior_mean, delta)``, or ``None`` if either window
    holds no readings, since a difference against nothing is not a number.
    """
    if window_days < 1:
        return None
    recent_from = anchor - timedelta(days=window_days - 1)
    prior_from = anchor - timedelta(days=2 * window_days - 1)
    prior_to = anchor - timedelta(days=window_days)

    recent = [v for d, v in points if recent_from <= d <= anchor]
    prior = [v for d, v in points if prior_from <= d <= prior_to]
    if not recent or not prior:
        return None

    recent_mean = sum(recent) / len(recent)
    prior_mean = sum(prior) / len(prior)
    return recent_mean, prior_mean, recent_mean - prior_mean


def latest_reading(points: Sequence[Point]) -> Point | None:
    """The most recent reading, or None when there is nothing to report."""
    return max(points, key=lambda p: p[0]) if points else None


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


def parse_series(series: list[dict[str, Any]]) -> list[Point]:
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


# Tick labels are 12px tabular-nums, so every digit takes one fixed advance
# and the separators take less. Estimating the width from the string is what
# lets the gutter fit the labels it actually has.
_TICK_DIGIT_PX = 7.4
_TICK_THIN_PX = 4.2
_MIN_YGUT = 34.0


def _tick_label_px(text: str) -> float:
    return sum(_TICK_DIGIT_PX if ch.isdigit() else _TICK_THIN_PX for ch in text)


# Above this, readouts are comma-grouped and lose their decimal: "9,162"
# carries every digit that means anything on a step count. Matches the stat
# tile's rule, so the tooltip and the tile beside it agree.
_GROUP_ABOVE = 1000


def _readout(value: float) -> str:
    """The number as the tooltip states it."""
    if abs(value) >= _GROUP_ABOVE:
        return f"{value:,.0f}"
    if abs(value) >= 1:
        text = f"{value:.1f}"
        return text[:-2] if text.endswith(".0") else text
    # Sub-unit metrics would round away entirely at one decimal.
    return f"{value:g}"


_TEMPLATE = Template("""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>$title</title>
<style>
$palette
  /* A dashboard tile must never scroll. The tooltip is clamped inside the
     frame below, but the hover dot is centred on its point and still pokes a
     few pixels past the edge at the last reading — enough for a scrollbar on
     its own. This closes the whole class rather than that one instance. */
  html,body{margin:0;height:100%;overflow:hidden;background:var(--surface);
    font:13px/1.4 $font;color:var(--ink)}
  /* The plot fills the frame in both axes. `preserveAspectRatio: none` is
     what makes that exact — "meet" would letterbox, leaving the chart short
     of the card's height. Stretching the geometry is fine for a value scale,
     but it must not stretch the ink: every stroked element carries
     non-scaling-stroke so line weights stay as authored, and the tick labels
     live in HTML positioned by percentage rather than in the SVG, so the type
     is never distorted either. */
  #wrap{position:relative;width:100%;height:100%}
  /* The plot, inset from the left by the tick gutter. Everything positioned
     against the plot's coordinates — the SVG, the x ticks, the hover marker —
     lives in here; only the y labels and the tooltip sit outside it. */
  #plot{position:absolute;left:calc(var(--ygut) + ${ygutgap}px);right:0;
        top:0;bottom:${xgut}px}
  svg{position:absolute;inset:0;width:100%;height:100%;display:block}
  .grid,.axis,.raw,.trend,.cross{vector-effect:non-scaling-stroke}
  .grid{stroke:var(--grid);stroke-width:1}
  .axis{stroke:var(--axis);stroke-width:1}
  .raw{fill:none;stroke:var(--raw);stroke-width:1.5;
       stroke-linecap:round;stroke-linejoin:round}
  .trend{fill:none;stroke:var(--trend);stroke-width:3;
         stroke-linecap:round;stroke-linejoin:round}
  .cross{stroke:var(--axis);stroke-width:1;opacity:0}
  /* Deliberately absent from the vector-effect rule above: a bar is a fill,
     and its width and height are the encoding — they are *supposed* to scale
     with the frame. */
  .bar{fill:var(--trend)}
  /* The hover marker for bars, the counterpart of .hdot for lines. A wash of
     ink over the bar rather than a colour change, so it reads the same in both
     themes without introducing a second hue. */
  .hbar{position:absolute;background:var(--ink);opacity:0;pointer-events:none;
        border-radius:3px}
  .tick{position:absolute;color:var(--muted);font-size:12px;
        font-variant-numeric:tabular-nums;pointer-events:none;white-space:nowrap}
  /* Right-aligned against the plot's left edge, so the widest label is always
     fully on screen no matter how narrow the card gets. */
  .ytick{transform:translate(-100%,-50%);left:var(--ygut)}
  :root{--ygut:${ygut}px}
  /* Below the plot, in the strip #plot is inset by — not inside it. A label
     with a fixed height cannot share a box whose height is a percentage. */
  .xtick{transform:translate(-50%,0);top:calc(100% + ${xgutgap}px)}
  .xtick.last{transform:translate(-100%,0)}
  .hdot{position:absolute;width:9px;height:9px;border-radius:50%;
        background:var(--trend);border:2px solid var(--surface);box-sizing:border-box;
        transform:translate(-50%,-50%);opacity:0;pointer-events:none}
  /* No centring transform: the position is computed and clamped in JS, so
     that half the box cannot hang off the right edge at the last reading. */
  #tip{position:absolute;pointer-events:none;opacity:0;
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
  var cross = svg.querySelector('.cross');
  var marks = [].slice.call(document.querySelectorAll('.hdot,.hbar'));
  var bars = d.kind === 'bar';
  var tip = document.getElementById('tip'), wrap = document.getElementById('wrap');
  var plot = document.getElementById('plot');

  // With preserveAspectRatio="none" the viewBox maps linearly onto the box,
  // so the conversion is a plain ratio in each axis — no CTM needed, and the
  // same ratios position the HTML overlay elements.
  var VW = $vw, VH = $vh;
  function fracX(x){ return x / VW; }
  function fracY(y){ return y / VH; }

  function show(e){
    var box = svg.getBoundingClientRect();
    var vx = (e.clientX - box.left) / box.width * VW;
    var best = 0, bestD = Infinity;
    for (var i = 0; i < d.points.length; i++){
      var dist = Math.abs(d.points[i].x - vx);
      if (dist < bestD){ bestD = dist; best = i; }
    }
    var p = d.points[best];
    if (cross){
      cross.setAttribute('x1', p.x); cross.setAttribute('x2', p.x);
      cross.style.opacity = 1;
    }

    // One marker per panel, and one tooltip listing every series for this
    // date. A single series keeps its trend readout; with several, the bold
    // line already shows the trend and repeating it per series would crowd
    // the box.
    var many = d.series.length > 1;
    var html = '<span>' + p.label + '</span>', anchor = null;
    for (var s = 0; s < d.series.length; s++){
      var v = p.v[s], meta = d.series[s], mark = marks[s];
      if (!v){ if (mark) mark.style.opacity = 0; continue; }
      var vy = v.ty !== null ? v.ty : v.ry;
      if (anchor === null) anchor = vy;
      if (mark && bars){
        // Cover the bar itself: its slot in x, and value-to-floor in y.
        mark.style.left = (fracX(p.x - d.bw / 2) * 100) + '%';
        mark.style.width = (fracX(d.bw) * 100) + '%';
        mark.style.top = (fracY(v.ry) * 100) + '%';
        mark.style.height = (fracY(meta.y0 - v.ry) * 100) + '%';
        mark.style.opacity = 0.18;
      } else if (mark){
        mark.style.left = (fracX(p.x) * 100) + '%';
        mark.style.top = (fracY(vy) * 100) + '%';
        mark.style.opacity = 1;
      }
      var unit = meta.unit ? ' ' + meta.unit : '';
      html += '<br>' + (many ? '<span>' + meta.label + '</span> ' : '') + v.rv + unit;
      if (!many && v.tv !== null){
        html += '<br><b>' + v.tv + unit + '</b> <span>' + d.window +
                '-day trend</span>';
      }
    }
    if (anchor === null) return;   // no series has a reading here
    var y = anchor;
    tip.innerHTML = html;

    // Placed in pixels and clamped, rather than centred with a transform:
    // centring puts half the box past the right edge at the last reading,
    // which used to grow the page and raise a scrollbar. Measured after the
    // content is set — opacity does not affect layout, so this is correct
    // even on the first hover.
    // The tooltip is a child of #wrap, not #plot, so it can use the gutter's
    // width too — on a narrow card that is the difference between fitting and
    // being clamped. Its coordinates therefore need the plot's own offset.
    var frame = wrap.getBoundingClientRect(), area = plot.getBoundingClientRect();
    var px = area.left - frame.left + fracX(p.x) * area.width;
    var py = fracY(y) * area.height;
    var tw = tip.offsetWidth, th = tip.offsetHeight;

    var left = Math.min(Math.max(px - tw / 2, $edge), frame.width - tw - $edge);
    var top = py - th - $gap;
    if (top < $edge) top = py + $gap;   // no room above: flip below the point

    tip.style.left = left + 'px';
    tip.style.top = top + 'px';
    tip.style.opacity = 1;
  }
  function hide(){
    if (cross) cross.style.opacity = 0;
    for (var i = 0; i < marks.length; i++) marks[i].style.opacity = 0;
    tip.style.opacity = 0;
  }

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


def _x_ticks(first: date, last: date) -> list[tuple[date, str]]:
    """Dates to label along the x axis, chosen to suit the span.

    Month starts read well across a quarter but collapse to a single label over
    a month, so a short span gets evenly spaced day markers instead.
    """
    span = (last - first).days
    if span >= _MONTH_TICK_SPAN_DAYS:
        out = []
        month = date(first.year, first.month, 1)
        while month <= last:
            if month >= first:
                out.append((month, month.strftime("%b")))
            month = date(month.year + (month.month == 12), month.month % 12 + 1, 1)
        return out

    step = max(span // 4, 1)
    days = list(range(0, span + 1, step))
    return [(first + timedelta(days=n), (first + timedelta(days=n)).strftime("%-d %b"))
            for n in days]


def render_chart_page(
    summaries: dict[str, Any] | Sequence[dict[str, Any]],
    *,
    title: str = "Weight",
    series_labels: Sequence[str] | None = None,
    series_units: Sequence[str] | None = None,
    window: int = 7,
    kind: str = "line",
    baseline: float | None = None,
    refresh_minutes: int = 30,
    max_gap_days: int = DEFAULT_MAX_GAP_DAYS,
) -> str:
    """Render one or more metric summaries as a standalone SVG chart page.

    Each summary becomes its own **stacked panel** with its own y-axis, sharing
    one x-axis. Measures in different units — steps and miles, say — cannot
    share a y-scale honestly: a dual axis can be slid until either series
    appears to lead, so it asserts a relationship the data does not have.
    Separate panels state each measure on its own terms.

    With a single summary the layout collapses to exactly one full-height
    panel, which is the original single-metric chart.

    ``kind`` picks the mark. ``"line"`` suits a sampled level — body weight is
    a continuous quantity you happen to read on some days. ``"bar"`` suits a
    discrete daily total like a step count, where there is no value *between*
    Tuesday and Wednesday to interpolate to.

    ``baseline`` pins the floor of every panel's y-axis. Left unset the axis
    zooms to the data, which reads day-to-day variation; ``baseline=0`` makes
    bar lengths proportional to their values instead.
    """
    if isinstance(summaries, dict):
        summaries = [summaries]

    # An override per series, positionally. `step_count`'s stored unit is the
    # literal string "count", which reads as noise beside the number, so an
    # empty override has to be able to drop it. `None` keeps what was stored.
    overrides = list(series_units or []) + [None] * len(summaries)
    panels = [
        (parse_series(s.get("series") or []),
         (s.get("unit") or "") if override is None else override,
         label)
        for s, label, override in zip(
            summaries,
            list(series_labels or []) + [""] * len(summaries),
            overrides,
        )
    ]
    panels = [p for p in panels if p[0]]  # a panel with no readings is no panel

    if not panels:
        body = '<div class="empty">No readings in this period.</div>'
        payload = {"points": [], "series": [], "window": window}
        return _TEMPLATE.substitute(
            title=title, body=body,
            palette=PALETTE_CSS, font=FONT_STACK, vw=_W, vh=_H,
            edge=_TIP_EDGE, gap=_TIP_GAP, ygut=_MIN_YGUT, ygutgap=_YGUT_GAP, xgut=_XGUT, xgutgap=_XGUT_GAP,
            data=json.dumps(payload).replace("<", "\\u003c"),
            refresh_ms=refresh_minutes * 60_000,
        )

    # One shared x scale across every panel.
    first = min(points[0][0] for points, _, _ in panels)
    last = max(points[-1][0] for points, _, _ in panels)
    day_span = max((last - first).days, 1)

    plot_w = _W - _PAD_R
    # A band scale for bars, a point scale for lines. On a point scale the
    # first and last readings sit exactly on the frame edges, which would slice
    # the end bars in half; a band gives every day a slot and centres its bar
    # in it.
    slot_w = plot_w / (day_span + 1)
    bar_w = slot_w * _BAR_SLOT_FILL

    def sx(d: date) -> float:
        if kind == "bar":
            return ((d - first).days + 0.5) * slot_w
        return (d - first).days / day_span * plot_w

    count = len(panels)
    gap = _PANEL_GAP if count > 1 else 0
    panel_h = (_H - _PAD_T - gap * (count - 1)) / count

    parts: list[str] = []
    # Tick text lives outside the SVG. The SVG stretches to fill the frame, and
    # anything inside it stretches too — type included — so the labels are HTML
    # placed at the same coordinates expressed as percentages.
    x_labels: list[str] = []
    y_labels: list[str] = []
    series_meta: list[dict[str, Any]] = []
    gutter_px = _MIN_YGUT
    by_day: list[dict[date, dict[str, Any]]] = []

    for index, (points, unit, label) in enumerate(panels):
        top = _PAD_T + index * (panel_h + gap)
        bottom = top + panel_h

        trend = rolling_trend(points, window) if window >= 1 else []
        values = [v for _, v in points] + [v for _, v in trend]
        low, high = min(values), max(values)
        # A little headroom so the extremes are not welded to the frame.
        margin = (high - low) * 0.12 or 1.0
        low, high = low - margin, high + margin
        if baseline is not None:
            # Pinned floor. Guarded because a baseline at or above the data
            # would invert the scale and divide by zero.
            low = min(baseline, max(values) - abs(margin))

        def sy(v: float, top=top, bottom=bottom, low=low, high=high) -> float:
            return bottom - (v - low) / (high - low) * (bottom - top)

        for tick in _nice_ticks(low, high):
            y = sy(tick)
            text = f"{tick:,g}"
            parts.append(f'<line class="grid" x1="0" y1="{y:.1f}" '
                         f'x2="{plot_w}" y2="{y:.1f}"/>')
            # `--ygut` is measured from the labels rather than fixed, because
            # "15,000" needs half again the room "190" does, and it sets the
            # plot's inset too — that is what keeps the two from colliding.
            gutter_px = max(gutter_px, _tick_label_px(text) + 2)
            y_labels.append(
                f'<div class="tick ytick" style="top:{y / _H * 100:.3f}%">'
                f'{text}</div>')

        parts.append(f'<line class="axis" x1="0" y1="{bottom:.1f}" '
                     f'x2="{plot_w}" y2="{bottom:.1f}"/>')

        if kind == "bar":
            # Anchored to the axis floor. A reading below an explicit baseline
            # has no bar to draw rather than one hanging under the axis.
            floor = sy(low)
            for day, value in points:
                y = sy(value)
                height = floor - y
                if height <= 0:
                    continue
                parts.append(
                    f'<rect class="bar" x="{sx(day) - bar_w / 2:.1f}" y="{y:.1f}" '
                    f'width="{bar_w:.1f}" height="{height:.1f}" '
                    f'rx="{_BAR_RX}" ry="{_BAR_RY}"/>')
        else:
            for run in split_on_gaps(points, max_gap_days):
                if len(run) > 1:
                    parts.append(f'<path class="raw" d="{_path(run, sx, sy)}"/>')
        for run in split_on_gaps(trend, max_gap_days):
            if len(run) > 1:
                parts.append(f'<path class="trend" d="{_path(run, sx, sy)}"/>')

        meta: dict[str, Any] = {"unit": unit, "label": label}
        if kind == "bar":
            # Where the hover overlay's bottom edge sits, per panel.
            meta["y0"] = round(sy(low), 1)
        series_meta.append(meta)
        trend_by_day = dict(trend)
        by_day.append({
            d: {
                "ry": round(sy(v), 1),
                "ty": (round(sy(trend_by_day[d]), 1) if d in trend_by_day else None),
                "rv": _readout(v),
                "tv": (_readout(trend_by_day[d]) if d in trend_by_day else None),
            }
            for d, v in points
        })

    # X ticks once, under the bottom panel.
    # No tick marks: the plot now ends exactly at its baseline, so a stub drawn
    # below it would fall outside the viewBox and be clipped. Each label sits
    # centred under its own day, which is the reference it was providing.
    ticks = _x_ticks(first, last)
    for index, (day, text) in enumerate(ticks):
        x = sx(day)
        # Centred on its mark, except the last one — half of a centred label
        # hangs past the right edge, which on a narrow card is enough to clip
        # it. That one hangs to the left of its mark instead.
        edge = " last" if index == len(ticks) - 1 and index else ""
        # Only the horizontal position comes from the plot; the CSS pins these
        # to the bottom edge.
        x_labels.append(f'<div class="tick xtick{edge}" '
                        f'style="left:{x / _W * 100:.3f}%">{text}</div>')

    # Bars carry the hover themselves — the highlighted bar names the day more
    # plainly than a rule drawn through it would.
    if kind != "bar":
        parts.append(f'<line class="cross" y1="{_PAD_T}" y2="{_H}"/>')

    marker = "hbar" if kind == "bar" else "hdot"
    markers = "".join(f'<div class="{marker}"></div>' for _ in panels)
    # Anything measured in plot coordinates goes inside #plot; the y labels sit
    # outside it, in the gutter that #plot is inset by.
    body = (f'<div id="plot">'
            f'<svg viewBox="0 0 {_W} {_H}" preserveAspectRatio="none" role="img" '
            f'aria-label="{title}">{"".join(parts)}</svg>'
            f'{"".join(x_labels)}{markers}'
            f'</div>'
            f'{"".join(y_labels)}<div id="tip"></div>')

    every_day = sorted({d for points, _, _ in panels for d, _ in points})
    payload = {
        "window": window,
        "series": series_meta,
        "points": [
            {
                "x": round(sx(d), 1),
                "label": d.strftime("%a %-d %b"),
                # One entry per panel, null where that series has no reading
                # for the day, so the hover can skip it.
                "v": [panel.get(d) for panel in by_day],
            }
            for d in every_day
        ],
    }
    if kind == "bar":
        # Only the bar hover needs these, and leaving them out otherwise keeps
        # the line chart's payload exactly as it was.
        payload["kind"] = kind
        payload["bw"] = round(bar_w, 1)

    return _TEMPLATE.substitute(
        title=title, body=body, palette=PALETTE_CSS, font=FONT_STACK,
        vw=_W, vh=_H, edge=_TIP_EDGE, gap=_TIP_GAP, ygut=round(gutter_px, 1), ygutgap=_YGUT_GAP,
        xgut=_XGUT, xgutgap=_XGUT_GAP,
        data=json.dumps(payload, separators=(",", ":")).replace("<", "\\u003c"),
        refresh_ms=refresh_minutes * 60_000,
    )
