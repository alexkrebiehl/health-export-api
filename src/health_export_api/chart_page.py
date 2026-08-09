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
from dataclasses import dataclass
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
_KEYGUT = 20.0     # the strip above the plot the legend sits in
# How close to the panel floor a y tick has to be to rest on it rather
# than straddle it. A zero baseline puts one there every time.
_FLOOR_TICK_SLACK = 0.6

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

# Fills available to a multi-series panel, in fixed order — never cycled into
# a ninth hue. The steps come from the `dataviz` reference palette and were
# checked with its validator in both modes rather than chosen by eye.
#
# Slots 1 and 2 are two steps of one hue and slot 3 contrasts with them, which
# suits parts-of-a-whole beside a separate measure: resting and active energy
# are components of burn, so relating them by hue says something true, while
# intake is a different quantity and takes a different one. The one-hue pair
# has to be validated as an *ordinal ramp*, not as categorical slots — as
# categorical it fails the normal-vision floor at ΔE 9.5 against 15, because
# that check asks whether two independent series can be told apart, which is
# the wrong question for two halves of a stacked bar.
_SERIES_TONES = 3


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


def window_balance(
    intake: Sequence[Point],
    spend: Sequence[Sequence[Point]],
    window_days: int,
    anchor: date,
) -> tuple[float, int] | None:
    """Energy in minus energy out over a window, and the days it covered.

    Negative is a deficit — less taken in than spent.

    **Only days with an intake reading count.** A past day with none is missing
    data rather than a day of fasting — two such days in the last sixty have
    burn recorded and no intake, against a lowest genuinely-logged day of 1,317
    kcal, so calling them zero would invent a 2,200 kcal deficit each. They are
    left out and the caller says how many days remain.

    Today is the exception, and it is the caller's to make: pass intake through
    :func:`zero_fill_today` first and today arrives with a zero reading, so its
    partial burn counts against nothing eaten yet — which is what a running
    total for a day in progress should say.

    Returns ``(net, days)``, or ``None`` when no day in the window qualifies.
    """
    if window_days < 1:
        return None
    earliest = anchor - timedelta(days=window_days - 1)

    eaten: dict[date, float] = {}
    for day, value in intake:
        if earliest <= day <= anchor:
            eaten[day] = eaten.get(day, 0.0) + value
    if not eaten:
        return None

    burned = 0.0
    for series in spend:
        for day, value in series:
            if day in eaten:
                burned += value
    return sum(eaten.values()) - burned, len(eaten)


def zero_fill_today(points: Sequence[Point], today: date) -> list[Point]:
    """Record today as zero when a daily total has nothing logged yet.

    Only for **today**, and only for a summed metric. The day is in progress:
    nothing logged means nothing has happened yet, which is a number, and
    reporting yesterday's total under a "Today" label instead is worse.

    A *past* day is a different claim. There the log is finished, so an absent
    day is missing data rather than a day of fasting — and it shows: two days
    in the last sixty have burn recorded and no intake, against a lowest
    genuinely-logged day of 1,317 kcal. Calling those zero would invent a
    2,200 kcal deficit each, so they stay absent.
    """
    if any(day == today for day, _ in points):
        return list(points)
    return sorted([*points, (today, 0.0)])


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


def _bar_path(x: float, y: float, width: float, height: float, capped: bool) -> str:
    """A bar as a path: rounded at the data end, square at the base.

    A rect can only round all four corners at once, which is fine for a lone
    bar sitting on the axis but wrong inside a stack — two rounded edges meeting
    pinch the join and the column stops reading as one bar. So only the segment
    that caps a stack gets the corners, and every join below it stays square.

    The two radii differ because ``preserveAspectRatio="none"`` scales the axes
    independently; equal ones would come out as a visible ellipse.
    """
    bottom, right = y + height, x + width
    if not capped:
        return f"M {x:.1f},{bottom:.1f} L {x:.1f},{y:.1f} " \
               f"L {right:.1f},{y:.1f} L {right:.1f},{bottom:.1f} Z"
    rx, ry = min(_BAR_RX, width / 2), min(_BAR_RY, height)
    return (f"M {x:.1f},{bottom:.1f} L {x:.1f},{y + ry:.1f} "
            f"Q {x:.1f},{y:.1f} {x + rx:.1f},{y:.1f} "
            f"L {right - rx:.1f},{y:.1f} "
            f"Q {right:.1f},{y:.1f} {right:.1f},{y + ry:.1f} "
            f"L {right:.1f},{bottom:.1f} Z")


@dataclass(frozen=True)
class Series:
    """One metric's readings, with everything needed to draw and label it."""

    points: list[Point]
    unit: str
    label: str
    integral: bool
    stack: str | None


def _stacks_of(panel: Sequence[Series]) -> list[list[Series]]:
    """The panel's series grouped by stack name, in first-appearance order.

    A series with no stack name is its own stack, so an ungrouped panel comes
    back as one stack of one — which is what every chart before this was.
    """
    order: list[str] = []
    grouped: dict[str, list[Series]] = {}
    for index, item in enumerate(panel):
        key = item.stack if item.stack else f"\x00{index}"
        if key not in grouped:
            order.append(key)
            grouped[key] = []
        grouped[key].append(item)
    return [grouped[key] for key in order]


def _stack_totals(stack: Sequence[Series]) -> dict[date, float]:
    """Day-by-day sum of a stack, which is what its bar's height must fit."""
    totals: dict[date, float] = {}
    for item in stack:
        for day, value in item.points:
            totals[day] = totals.get(day, 0.0) + value
    return totals


# Tick labels are 12px tabular-nums, so every digit takes one fixed advance
# and the separators take less. Estimating the width from the string is what
# lets the gutter fit the labels it actually has.
_TICK_DIGIT_PX = 7.4
_TICK_THIN_PX = 4.2
_MIN_YGUT = 34.0


def _tick_label_px(text: str) -> float:
    return sum(_TICK_DIGIT_PX if ch.isdigit() else _TICK_THIN_PX for ch in text)


# Units whose values are conventionally reported whole. `count` is a tally —
# Apple Health's unit for step_count and flights_climbed — where a fraction is
# an artefact of summing partial samples. `kcal` is not a tally, but nobody
# reports a 756.3 calorie deficit either; the tenth is noise at that magnitude.
_WHOLE_UNITS = {"count", "kcal"}


def is_whole_unit(unit: str | None) -> bool:
    """Whether a metric's stored unit is one reported without decimals.

    Read from the *stored* unit, so blanking the displayed one still formats
    the number correctly.
    """
    return (unit or "").strip().lower() in _WHOLE_UNITS


# Above this, readouts are comma-grouped and lose their decimal: "9,162"
# carries every digit that means anything on a step count. Matches the stat
# tile's rule, so the tooltip and the tile beside it agree.
_GROUP_ABOVE = 1000


def _readout(value: float, integral: bool = False) -> str:
    """The number as the tooltip states it.

    ``integral`` drops the decimal at any magnitude — a counted thing has no
    fractional part to report, so a step count reads 374, never "373.8".
    """
    if integral or abs(value) >= _GROUP_ABOVE:
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
        top:${keygut}px;bottom:${xgut}px}
  svg{position:absolute;inset:0;width:100%;height:100%;display:block}
  .grid,.axis,.raw,.trend,.cross,.over{vector-effect:non-scaling-stroke}
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
  /* Categorical fills for a multi-series panel, assigned in fixed order. */
  .bar.s1{fill:var(--series-1)} .bar.s2{fill:var(--series-2)}
  .bar.s3{fill:var(--series-3)}
  .raw.s1,.over.s1{stroke:var(--series-1)} .raw.s2,.over.s2{stroke:var(--series-2)}
  .raw.s3,.over.s3{stroke:var(--series-3)}
  /* An overlaid stack: a line over the bars, so it needs the weight to read
     against them rather than the muted treatment a raw series gets. */
  /* Drawn over the bars, so it needs more weight than a series line
     sitting on an empty plot would. */
  .over{fill:none;stroke-width:3.5;stroke-linecap:round;stroke-linejoin:round}
  .raw.s1,.raw.s2,.raw.s3{stroke-width:2}
  #key{position:absolute;top:0;left:0;right:0;display:flex;flex-wrap:wrap;
       gap:4px 14px;font-size:12px;color:var(--ink-2);pointer-events:none}
  #key span{display:inline-flex;align-items:center;gap:5px;white-space:nowrap}
  #key i{width:9px;height:9px;border-radius:2px;flex:none}
  #key .k1{background:var(--series-1)} #key .k2{background:var(--series-2)}
  #key .k3{background:var(--series-3)}
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
  .ytick.floor{transform:translate(-100%,-100%)}
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
      // With several series in one panel there is one marker for the panel,
      // not one per series, and it covers the whole day rather than one bar —
      // the day is what the tooltip is reporting.
      var v = p.v[s], meta = d.series[s];
      var mark = meta.panel === undefined ? marks[s] : marks[meta.panel];
      if (!v){ if (mark && meta.panel === undefined) mark.style.opacity = 0; continue; }
      var vy = v.ty !== null ? v.ty : v.ry;
      if (anchor === null) anchor = vy;
      if (mark && bars && meta.panel !== undefined){
        mark.style.left = (fracX(p.x - d.slot / 2) * 100) + '%';
        mark.style.width = (fracX(d.slot) * 100) + '%';
        mark.style.top = '0%';
        mark.style.height = '100%';
        mark.style.opacity = 0.10;
      } else if (mark && bars){
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
    series_stacks: Sequence[str] | None = None,
    window: int = 7,
    kind: str = "line",
    layout: str = "grouped",
    legend: bool | None = None,
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

    ``series_stacks`` names a stack per summary and changes the shape of the
    chart: every summary then shares **one** panel and one y-axis, and those
    naming the same stack are drawn as segments of a single bar. Sharing an
    axis is only honest when the measures share a unit, which is the caller's
    assertion to make, not this function's — it is what grouping them means.
    ``layout`` then decides how the stacks are arranged:

    * ``"grouped"`` — one bar per stack, side by side within the day's slot.
    * ``"overlay"`` — the first stack draws as bars, the rest as lines over it.

    ``legend`` defaults to on only when a panel holds more than one series;
    with one series the title says what it is and a key would be noise.
    """
    if isinstance(summaries, dict):
        summaries = [summaries]

    # An override per series, positionally. `step_count`'s stored unit is the
    # literal string "count", which reads as noise beside the number, so an
    # empty override has to be able to drop it. `None` keeps what was stored.
    overrides = list(series_units or []) + [None] * len(summaries)
    stacks = list(series_stacks or []) + [None] * len(summaries)
    series = [
        Series(points=parse_series(s.get("series") or []),
               unit=(s.get("unit") or "") if override is None else override,
               label=label,
               # From the stored unit, not the displayed one: `unit=` blanks
               # the label but the number is still a tally.
               integral=is_whole_unit(s.get("unit")),
               stack=stack)
        for s, label, override, stack in zip(
            summaries,
            list(series_labels or []) + [""] * len(summaries),
            overrides,
            stacks,
        )
    ]
    series = [s for s in series if s.points]  # nothing to draw, nothing to draw

    # Stacked series share one panel and one y-axis; otherwise each metric gets
    # a panel of its own, which is what every chart did before stacking existed.
    if any(s.stack for s in series):
        panels = [series] if series else []
        # A stacked bar says its segments sum to its height. Cut the axis off
        # above zero and only the bottom segment is foreshortened, so the split
        # between the parts misstates their ratio — 2,123 resting against 1,072
        # active reads as 1.5:1 rather than 2:1. Zoom is a defensible default
        # for a single series and a wrong one here, so stacks start at zero
        # unless the caller says otherwise.
        if baseline is None:
            baseline = 0.0
    else:
        panels = [[s] for s in series]

    if not panels:
        body = '<div class="empty">No readings in this period.</div>'
        payload = {"points": [], "series": [], "window": window}
        return _TEMPLATE.substitute(
            title=title, body=body,
            palette=PALETTE_CSS, font=FONT_STACK, vw=_W, vh=_H,
            edge=_TIP_EDGE, gap=_TIP_GAP, ygut=_MIN_YGUT, ygutgap=_YGUT_GAP,
            xgut=_XGUT, xgutgap=_XGUT_GAP, keygut=0,
            data=json.dumps(payload).replace("<", "\\u003c"),
            refresh_ms=refresh_minutes * 60_000,
        )

    # One shared x scale across every panel.
    every_series = [item for panel in panels for item in panel]
    first = min(item.points[0][0] for item in every_series)
    last = max(item.points[-1][0] for item in every_series)
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

    for index, panel in enumerate(panels):
        top = _PAD_T + index * (panel_h + gap)
        bottom = top + panel_h

        groups = _stacks_of(panel)
        totals = [_stack_totals(group) for group in groups]
        trends = [rolling_trend(item.points, window) if window >= 1 else []
                  for item in panel]

        # The axis has to fit the *stack totals*, not the individual segments:
        # a bar is as tall as its parts together.
        values = [v for total in totals for v in total.values()]
        values += [v for trend in trends for _, v in trend]
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
            # A tick sitting on the panel's floor — which a zero baseline
            # always produces — is centred on a line at the very bottom of the
            # plot, so half the label drops into the strip below. That one
            # rests on the line instead of straddling it.
            on_floor = " floor" if abs(y - bottom) < _FLOOR_TICK_SLACK else ""
            y_labels.append(
                f'<div class="tick ytick{on_floor}" style="top:{y / _H * 100:.3f}%">'
                f'{text}</div>')

        parts.append(f'<line class="axis" x1="0" y1="{bottom:.1f}" '
                     f'x2="{plot_w}" y2="{bottom:.1f}"/>')

        floor = sy(low)
        # Bars share the slot between the stacks when grouped; in overlay mode
        # only the first stack is bars, so it keeps the full width.
        bar_count = len(groups) if layout == "grouped" else 1
        slice_w = bar_w / bar_count
        multi = len(panel) > 1

        for group_index, group in enumerate(groups):
            as_bars = kind == "bar" and (layout == "grouped" or group_index == 0)
            # Left edge of this stack's bar within the day's slot.
            offset = (group_index - (bar_count - 1) / 2) * slice_w if as_bars else 0.0
            # Segments are drawn bottom-up, each starting where the last ended.
            below: dict[date, float] = {}
            # Which segment caps each day's bar — the last one in the stack with
            # a reading there. Only that one gets the rounded data-end.
            caps: dict[date, int] = {}
            for position, item in enumerate(group):
                for day, _ in item.points:
                    caps[day] = position

            for stacked_at, item in enumerate(group):
                position = panel.index(item)
                tone = f" s{position % _SERIES_TONES + 1}" if multi else ""
                if as_bars:
                    # Anchored to the axis floor. A reading below an explicit
                    # baseline has no bar rather than one hanging under it.
                    for day, value in item.points:
                        base = below.get(day, 0.0)
                        y = sy(base + value)
                        height = min(sy(base), floor) - y
                        if height <= 0:
                            continue
                        x = sx(day) - bar_w / 2 + offset
                        path = _bar_path(x, y, slice_w, height,
                                         caps.get(day) == stacked_at)
                        parts.append(f'<path class="bar{tone}" d="{path}"/>')
                        below[day] = base + value
                elif kind == "bar":
                    # An overlaid stack: its running total drawn as a line.
                    running = sorted((d, below.get(d, 0.0) + v) for d, v in item.points)
                    for run in split_on_gaps(running, max_gap_days):
                        if len(run) > 1:
                            parts.append(
                                f'<path class="over{tone}" d="{_path(run, sx, sy)}"/>')
                    for day, value in item.points:
                        below[day] = below.get(day, 0.0) + value
                else:
                    for run in split_on_gaps(item.points, max_gap_days):
                        if len(run) > 1:
                            parts.append(
                                f'<path class="raw{tone}" d="{_path(run, sx, sy)}"/>')

        for position, item in enumerate(panel):
            trend = trends[position]
            for run in split_on_gaps(trend, max_gap_days):
                if len(run) > 1:
                    parts.append(f'<path class="trend" d="{_path(run, sx, sy)}"/>')

            meta: dict[str, Any] = {"unit": item.unit, "label": item.label}
            if kind == "bar":
                # Where the hover overlay's bottom edge sits, per panel.
                meta["y0"] = round(floor, 1)
            if multi:
                # Which panel this belongs to, and which tone drew it — the
                # hover and the legend both need them once a panel holds more
                # than one series.
                meta["panel"] = index
                meta["tone"] = position % _SERIES_TONES + 1
            series_meta.append(meta)
            trend_by_day = dict(trend)
            by_day.append({
                d: {
                    "ry": round(sy(v), 1),
                    "ty": (round(sy(trend_by_day[d]), 1)
                           if d in trend_by_day else None),
                    "rv": _readout(v, item.integral),
                    "tv": (_readout(trend_by_day[d], item.integral)
                           if d in trend_by_day else None),
                }
                for d, v in item.points
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

    # A key only where identity cannot be inferred. With one series the title
    # names it; with three fills in one panel nothing else says which is which,
    # and the light-mode aqua sits under 3:1 against the surface, which the
    # `dataviz` checks say obliges a visible label rather than colour alone.
    show_key = any(len(panel) > 1 for panel in panels) if legend is None else legend
    key = ""
    if show_key and any(m.get("tone") for m in series_meta):
        entries = "".join(
            f'<span><i class="k{m["tone"]}"></i>{m["label"] or m["unit"]}</span>'
            for m in series_meta if m.get("tone"))
        key = f'<div id="key">{entries}</div>'

    # Anything measured in plot coordinates goes inside #plot; the y labels sit
    # outside it, in the gutter that #plot is inset by.
    body = (f'<div id="plot">'
            f'<svg viewBox="0 0 {_W} {_H}" preserveAspectRatio="none" role="img" '
            f'aria-label="{title}">{"".join(parts)}</svg>'
            f'{"".join(x_labels)}{markers}'
            f'</div>'
            f'{"".join(y_labels)}{key}<div id="tip"></div>')

    every_day = sorted({d for item in every_series for d, _ in item.points})
    payload = {
        "window": window,
        "series": series_meta,
        "points": [
            {
                "x": round(sx(d), 1),
                "label": d.strftime("%a %-d %b"),
                # One entry per series, null where that series has no reading
                # for the day, so the hover can skip it.
                "v": [readings.get(d) for readings in by_day],
            }
            for d in every_day
        ],
    }
    if kind == "bar":
        # Only the bar hover needs these, and leaving them out otherwise keeps
        # the line chart's payload exactly as it was.
        payload["kind"] = kind
        payload["bw"] = round(bar_w, 1)
    if any(len(panel) > 1 for panel in panels):
        # A panel of several series highlights the whole day's slot, not one
        # bar, so the hover needs the slot rather than the bar width.
        payload["slot"] = round(slot_w, 1)

    return _TEMPLATE.substitute(
        title=title, body=body, palette=PALETTE_CSS, font=FONT_STACK,
        vw=_W, vh=_H, edge=_TIP_EDGE, gap=_TIP_GAP, ygut=round(gutter_px, 1), ygutgap=_YGUT_GAP,
        xgut=_XGUT, xgutgap=_XGUT_GAP, keygut=_KEYGUT if key else 0,
        data=json.dumps(payload, separators=(",", ":")).replace("<", "\\u003c"),
        refresh_ms=refresh_minutes * 60_000,
    )
