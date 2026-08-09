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

from health_export_api.page_shell import PageOptions, render_page

GoodDirection = Literal["up", "down", "none"]

_STYLE = """  /* A *size* container, not inline-size: the text has to react to height as
     well as width. With inline-size only `cqw` is available, so a short wide
     tile could not use its height and a narrow one shrank the type even with
     vertical room to spare.

     The container is the shell's #page, which is already the *padded* box, so
     these budgets account for the caller's margin without arithmetic here.

     Each size is the smaller of two budgets — a share of the height (`cqh`)
     and a share of the width (`cqw`) — so whichever dimension binds first
     wins, and the text neither overflows nor leaves the tile half empty. */
  #tile{height:100%;display:flex;flex-direction:column;justify-content:center;
        align-items:${align_items};text-align:${align};
        gap:1.5cqh;padding:${pad_v}cqh ${pad_h}cqw;box-sizing:border-box}
  .label{color:var(--ink-2);font-size:min(14cqh,${label_cqw}cqw);line-height:1.15;
         letter-spacing:.01em;white-space:nowrap}
  /* Proportional figures on purpose: tabular-nums pads every digit to a zero's
     width, which looks gappy at display sizes. */
  .value{color:var(--ink);font-weight:600;line-height:1;
         font-size:min(${value_cqh}cqh,${value_cqw}cqw);white-space:nowrap}
  .value .unit{font-size:.45em;font-weight:500;color:var(--ink-2);
               margin-left:.22em}
  .note{color:var(--muted);font-size:min(12cqh,${note_cqw}cqw);line-height:1.15;
        white-space:nowrap}
  .good{color:var(--good)}
  .bad{color:var(--bad)}
  .empty{color:var(--muted)}
"""

_BODY = """<div id="tile">
  <div class="label">$label</div>
  <div class="value $tone">$value</div>
  <div class="note">$note</div>
</div>
"""


# Approximate advance widths, in em, for the system sans. Only good to a few
# percent, which is all the width budget below needs — the point is that a
# short string is allowed to grow larger than a long one, not to typeset.
_EM_WIDTHS = {" ": 0.26, ".": 0.28, ",": 0.28, "·": 0.32,
              "↓": 0.75, "↑": 0.75, "→": 0.85, "—": 1.0}
_EM_DEFAULT = 0.58

# Base padding, as a share of the tile, before any caller-supplied margin.
# Above this, values are comma-grouped and lose the decimal.
_GROUP_ABOVE = 1000

_BASE_PAD_H = 4.0
_BASE_PAD_V = 3.0

# Slack left over after padding, so text never runs right up to the edge.
_WIDTH_SLACK = 8.0

# The value's share of the tile height at zero margin. Together with the label
# (14), note (12), gaps (3) and padding (2x3) this leaves ~13 spare.
_BASE_VALUE_CQH = 52.0

Align = Literal["left", "center", "right"]

_ALIGN_ITEMS = {"left": "flex-start", "center": "center", "right": "flex-end"}


def _width_budget() -> float:
    """Share of the container's width the text may occupy.

    A constant now: the caller's margin shrinks the container itself, so it no
    longer has to be subtracted here as well.
    """
    return 100.0 - 2.0 * _BASE_PAD_H - _WIDTH_SLACK


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


def _fmt(value: float, integral: bool = False) -> str:
    """One decimal, no trailing '.0', and grouped once it gets big.

    191.4 lb, 2 lb, 9,605 steps. A decimal on a five-digit step count is
    noise, and ungrouped digits at that length are hard to read at a glance.

    ``integral`` drops the decimal at any magnitude. Counted things have no
    fractional part to report: 374 steps, never "373.8". Grouping above 1,000
    hid this until an early-morning step count fell below the threshold.
    """
    if integral or abs(value) >= _GROUP_ABOVE:
        return f"{value:,.0f}"
    text = f"{value:.1f}"
    return text[:-2] if text.endswith(".0") else text


def render_latest_tile(
    reading: tuple[date, float] | None,
    *,
    unit: str = "",
    label: str = "Current",
    today: date | None = None,
    align: Align = "left",
    integral: bool = False,
    options: PageOptions = PageOptions(),
) -> str:
    """Tile showing the most recent reading and how fresh it is."""
    if reading is None:
        return _render(label, "—", "", "No readings yet", "", align, options)

    day, value = reading
    age = ((today or date.today()) - day).days
    if age <= 0:
        note = "Today"
    elif age == 1:
        note = "Yesterday"
    else:
        note = f"{age} days ago"
    note += day.strftime(" · %-d %b")
    return _render(label, _fmt(value, integral), unit, note, "", align, options)


def render_change_tile(
    change: tuple[float, float, float] | None,
    *,
    unit: str = "",
    label: str = "Weekly trend",
    window_days: int = 7,
    good_direction: GoodDirection = "none",
    align: Align = "left",
    integral: bool = False,
    options: PageOptions = PageOptions(),
) -> str:
    """Tile showing a signed week-over-week change."""
    if change is None:
        return _render(label, "—", "",
                       f"Not enough readings for {window_days} days",
                       "", align, options)

    _, _, delta = change
    arrow = "↓" if delta < 0 else ("↑" if delta > 0 else "→")
    value = f"{arrow} {_fmt(abs(delta), integral)}"

    # Colour only when the caller has said which way is good; the arrow and
    # sign carry direction on their own either way.
    improving = (delta < 0 and good_direction == "down") or (
        delta > 0 and good_direction == "up"
    )
    tone = "good" if improving else ""
    return _render(label, value, unit, f"vs previous {window_days} days",
                   tone, align, options)


def render_balance_tile(
    balance: tuple[float, int] | None,
    *,
    unit: str = "",
    label: str = "7-day balance",
    window_days: int = 7,
    align: Align = "left",
    integral: bool = False,
    options: PageOptions = PageOptions(),
) -> str:
    """Tile showing energy in minus energy out: a deficit or a surplus.

    Two-sided colour, unlike :func:`render_change_tile`. That tile stays
    neutral because whether a metric rising is good depends on the goal rather
    than the metric — but here the goal is the thing being measured. A deficit
    is what a calorie balance is watched *for*, so green and red mean something
    specific rather than being borrowed approval.

    Colour is never the only channel: the word "deficit" or "surplus" is in the
    note, and the arrow carries direction on its own.
    """
    if balance is None:
        return _render(label, "—", "", "No days with intake logged",
                       "", align, options)

    net, days = balance
    deficit = net < 0
    arrow = "↓" if deficit else ("↑" if net > 0 else "→")
    value = f"{arrow} {_fmt(abs(net), integral)}"

    word = "deficit" if deficit else ("surplus" if net else "even")
    if window_days == 1:
        # A one-day window ends on today, so it *is* today — and a day still
        # being lived is better described than counted.
        covered = "so far today"
    else:
        # A window shorter than asked for means days went unlogged; saying so
        # beats presenting a 3-day figure as though it were a week.
        covered = f"over {days} day{'' if days == 1 else 's'}"
        if days < window_days:
            covered += f" of {window_days}"
    tone = "good" if deficit else ("bad" if net else "")
    return _render(label, value, unit, f"{word} · {covered}",
                   tone, align, options)


def _render(
    label: str,
    main: str,
    unit: str,
    note: str,
    tone: str,
    align: Align,
    options: PageOptions,
) -> str:
    # The unit rides at 0.45em with a 0.22em gap, so it costs proportionally
    # less width than its character count suggests.
    value_em = _em_width(main) + (0.22 + _em_width(unit) * 0.45 if unit else 0.0)
    value_html = main + (f'<span class="unit">{unit}</span>' if unit else "")

    # No margin arithmetic here any more. The shell pads #page and #page is the
    # container these cq units resolve against, so a margin shrinks the box the
    # budgets are shares *of* — which is what the hand-rolled coupling was
    # approximating.
    budget = _width_budget()

    style = Template(_STYLE).substitute(
        align=align,
        align_items=_ALIGN_ITEMS[align],
        pad_h=_BASE_PAD_H,
        pad_v=_BASE_PAD_V,
        value_cqh=_BASE_VALUE_CQH,
        # Caps stop a very short string ballooning past the height budget.
        value_cqw=_cqw_from_em(value_em, budget, cap=40.0),
        label_cqw=_cqw_for(label, budget, cap=9.0),
        note_cqw=_cqw_for(note, budget, cap=8.0),
    )
    body = Template(_BODY).substitute(
        label=label, value=value_html, note=note, tone=tone
    )
    return render_page(body=body, style=style, options=options.with_title(label))
