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
  /* A *size* container, not inline-size: the text has to react to height as
     well as width. With inline-size only `cqw` is available, so a short wide
     tile could not use its height and a narrow one shrank the type even with
     vertical room to spare.

     Each size is the smaller of two budgets — a share of the height (`cqh`)
     and a share of the width (`cqw`) — so whichever dimension binds first
     wins, and the text neither overflows nor leaves the tile half empty. */
  body{container-type:size}
  #tile{height:100%;display:flex;flex-direction:column;justify-content:center;
        gap:1.5cqh;padding:3cqh 4cqw;box-sizing:border-box}
  .label{color:var(--ink-2);font-size:min(14cqh,${label_cqw}cqw);line-height:1.15;
         letter-spacing:.01em;white-space:nowrap}
  /* Proportional figures on purpose: tabular-nums pads every digit to a zero's
     width, which looks gappy at display sizes. */
  .value{color:var(--ink);font-weight:600;line-height:1;
         font-size:min(52cqh,${value_cqw}cqw);white-space:nowrap}
  .value .unit{font-size:.45em;font-weight:500;color:var(--ink-2);
               margin-left:.22em}
  .note{color:var(--muted);font-size:min(12cqh,${note_cqw}cqw);line-height:1.15;
        white-space:nowrap}
  .good{color:var(--good)}
  .empty{color:var(--muted)}
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


# Approximate advance widths, in em, for the system sans. Only good to a few
# percent, which is all the width budget below needs — the point is that a
# short string is allowed to grow larger than a long one, not to typeset.
_EM_WIDTHS = {" ": 0.26, ".": 0.28, ",": 0.28, "·": 0.32,
              "↓": 0.75, "↑": 0.75, "→": 0.85, "—": 1.0}
_EM_DEFAULT = 0.58

# Share of the tile's width each element may occupy. Padding is 4cqw a side.
_WIDTH_BUDGET = {"value": 84.0, "label": 84.0, "note": 84.0}


def _em_width(text: str) -> float:
    return sum(_EM_WIDTHS.get(character, _EM_DEFAULT) for character in text)


def _cqw_from_em(em: float, budget: float, *, cap: float) -> float:
    """Font size, as a share of tile width, that fills `budget` at `em` wide.

    Without this the width budget would have to assume a string length, and a
    short value in a narrow tile would be typeset far smaller than it needs to
    be — the tile would look half empty purely because a constant was picked
    for the longest plausible string.
    """
    return cap if em <= 0 else min(cap, round(budget / em, 2))


def _cqw_for(text: str, budget: float, *, cap: float) -> float:
    return _cqw_from_em(_em_width(text), budget, cap=cap)


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
        return _render(label, "—", "", "No readings yet", "", refresh_minutes)

    day, value = reading
    age = ((today or date.today()) - day).days
    if age <= 0:
        note = "Today"
    elif age == 1:
        note = "Yesterday"
    else:
        note = f"{age} days ago"
    note += day.strftime(" · %-d %b")
    return _render(label, _fmt(value), unit, note, "", refresh_minutes)


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
        return _render(label, "—", "",
                       f"Not enough readings for {window_days} days",
                       "", refresh_minutes)

    _, _, delta = change
    arrow = "↓" if delta < 0 else ("↑" if delta > 0 else "→")
    value = f"{arrow} {_fmt(abs(delta))}"

    # Colour only when the caller has said which way is good; the arrow and
    # sign carry direction on their own either way.
    improving = (delta < 0 and good_direction == "down") or (
        delta > 0 and good_direction == "up"
    )
    tone = "good" if improving else ""
    return _render(label, value, unit, f"vs previous {window_days} days",
                   tone, refresh_minutes)


def _render(
    label: str,
    main: str,
    unit: str,
    note: str,
    tone: str,
    refresh_minutes: int,
) -> str:
    # The unit rides at 0.45em with a 0.22em gap, so it costs proportionally
    # less width than its character count suggests.
    value_em = _em_width(main) + (0.22 + _em_width(unit) * 0.45 if unit else 0.0)
    value_html = main + (f'<span class="unit">{unit}</span>' if unit else "")

    return _TEMPLATE.substitute(
        title=label,
        label=label,
        value=value_html,
        note=note,
        tone=tone,
        # Caps stop a very short string ballooning past the height budget.
        value_cqw=_cqw_from_em(value_em, _WIDTH_BUDGET["value"], cap=40.0),
        label_cqw=_cqw_for(label, _WIDTH_BUDGET["label"], cap=9.0),
        note_cqw=_cqw_for(note, _WIDTH_BUDGET["note"], cap=8.0),
        palette=PALETTE_CSS,
        font=FONT_STACK,
        refresh_ms=refresh_minutes * 60_000,
    )
