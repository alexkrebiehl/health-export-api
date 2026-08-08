"""A single stat tile, rendered as a self-contained page.

The third embeddable page, beside the coverage map and the metric chart. Some
questions are a number, not a plot — "what do I weigh" and "which way is it
going" both are — so this renders the number rather than a chart of it.

Design notes:

* **The value is the only loud thing.** Label above in secondary ink, value
  large and semibold, one line of context beneath in muted ink.

* **Proportional figures, not tabular.** ``tabular-nums`` gives every digit the
  width of a zero, which reads loose at display sizes. The chart's axis ticks
  use tabular because they sit in a column and must align; a standalone figure
  does not.

* **Direction is never colour alone.** The change tile always shows a sign and
  an arrow. Colour is added only when the caller says which direction is good,
  because whether a metric rising is good is a property of the goal, not of the
  metric.
"""

from __future__ import annotations

from datetime import date
from string import Template
from typing import Literal

from health_export_api.theme import FONT_STACK, PALETTE_CSS

GoodDirection = Literal["up", "down", "none"]

_TEMPLATE = Template("""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>$title</title>
<style>
$palette
  html,body{margin:0;height:100%;background:var(--surface);
    font:13px/1.4 $font;color:var(--ink)}
  #tile{height:100%;display:flex;flex-direction:column;justify-content:center;
        padding:0 clamp(10px,4%,22px);box-sizing:border-box}
  .label{color:var(--ink-2);font-size:clamp(11px,3.2cqw,14px);
         letter-spacing:.01em;margin-bottom:2px}
  /* Proportional figures on purpose: tabular-nums pads every digit to a zero's
     width, which looks gappy at display sizes. */
  .value{color:var(--ink);font-weight:600;line-height:1.05;
         font-size:clamp(26px,11cqw,52px);white-space:nowrap}
  .value .unit{font-size:.45em;font-weight:500;color:var(--ink-2);
               margin-left:.25em}
  .note{color:var(--muted);font-size:clamp(10px,3cqw,13px);margin-top:4px}
  .good{color:var(--good)}
  .empty{color:var(--muted)}
  body{container-type:inline-size}
</style>
</head>
<body>
<div id="tile">
  <div class="label">$label</div>
  <div class="value $tone">$value</div>
  <div class="note">$note</div>
</div>
<script>setTimeout(function(){location.reload();}, $refresh_ms);</script>
</body>
</html>
""")


def _fmt(value: float) -> str:
    """One decimal, but no trailing '.0' — 191.4 lb, 2 lb."""
    text = f"{value:.1f}"
    return text[:-2] if text.endswith(".0") else text


def render_latest_tile(
    reading: tuple[date, float] | None,
    *,
    unit: str = "",
    label: str = "Current",
    refresh_minutes: int = 30,
    today: date | None = None,
) -> str:
    """Tile showing the most recent reading and how fresh it is."""
    if reading is None:
        return _render(label, "—", "No readings yet", "", refresh_minutes)

    day, value = reading
    age = ((today or date.today()) - day).days
    if age <= 0:
        note = "Today"
    elif age == 1:
        note = "Yesterday"
    else:
        note = f"{age} days ago"
    note += day.strftime(" · %-d %b")
    return _render(label, f"{_fmt(value)}<span class=\"unit\">{unit}</span>",
                   note, "", refresh_minutes)


def render_change_tile(
    change: tuple[float, float, float] | None,
    *,
    unit: str = "",
    label: str = "Weekly trend",
    window_days: int = 7,
    good_direction: GoodDirection = "none",
    refresh_minutes: int = 30,
) -> str:
    """Tile showing a signed week-over-week change."""
    if change is None:
        return _render(label, "—", f"Not enough readings for {window_days} days",
                       "", refresh_minutes)

    _, _, delta = change
    arrow = "↓" if delta < 0 else ("↑" if delta > 0 else "→")
    value = f"{arrow} {_fmt(abs(delta))}<span class=\"unit\">{unit}</span>"

    # Colour only when the caller has said which way is good; the arrow and
    # sign carry direction on their own either way.
    improving = (delta < 0 and good_direction == "down") or (
        delta > 0 and good_direction == "up"
    )
    tone = "good" if improving else ""
    return _render(label, value, f"vs previous {window_days} days", tone,
                   refresh_minutes)


def _render(label: str, value: str, note: str, tone: str, refresh_minutes: int) -> str:
    return _TEMPLATE.substitute(
        title=label,
        label=label,
        value=value,
        note=note,
        tone=tone,
        palette=PALETTE_CSS,
        font=FONT_STACK,
        refresh_ms=refresh_minutes * 60_000,
    )
