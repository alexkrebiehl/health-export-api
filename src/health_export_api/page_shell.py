"""The scaffolding every rendered page shares.

The three page modules — map, chart, stat — each used to carry their own
``<head>``, their own ``html,body`` reset and their own copy of the reload
script. That is fine for one page and a liability for four: `map_page` had
already drifted far enough to hardcode its background instead of using the
shared palette, so the one card that ignored the theme did so silently.

This module owns the parts that are the same everywhere, and each page supplies
only its own stylesheet fragment and body. A new render endpoint gets the whole
set by rendering through :func:`render_page`.

Deliberately framework-free — no FastAPI import. :class:`PageOptions` is a
plain dataclass so the page modules never depend on the web layer; the
dependency that builds one from query params lives in ``routers/options.py``,
and a test asserts the separation holds.
"""

from __future__ import annotations

from dataclasses import dataclass
from string import Template
from typing import Literal

from health_export_api.theme import FONT_STACK, PALETTE_CSS

Theme = Literal["auto", "light", "dark"]

# A margin bigger than this would leave nothing to render into. Lives here
# rather than in stat_page now that every page honours the option.
MAX_MARGIN = 20.0

DEFAULT_REFRESH_MINUTES = 30


@dataclass(frozen=True)
class PageOptions:
    """Options that mean the same thing on any rendered page.

    Kept separate from each endpoint's own parameters — a map's bounding box or
    a chart's metric list — so that adding an endpoint does not mean copying
    these again.
    """

    title: str = ""
    refresh_minutes: int = DEFAULT_REFRESH_MINUTES
    margin: float = 0.0
    theme: Theme = "auto"

    @property
    def clamped_margin(self) -> float:
        return max(0.0, min(self.margin, MAX_MARGIN))

    def with_title(self, title: str) -> "PageOptions":
        """The same options, with a title supplied by the endpoint.

        Each endpoint has its own sensible default — a metric name, a workout
        map — and an explicit `title` should win over it.
        """
        return self if self.title else PageOptions(
            title=title,
            refresh_minutes=self.refresh_minutes,
            margin=self.margin,
            theme=self.theme,
        )


_SHELL = Template("""<!doctype html>
<html lang="en"$theme_attr>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>$title</title>
$head
<style>
$palette
  /* These are fixed-size embedded tiles. A scrollbar inside one is always a
     bug, never a feature. */
  html,body{margin:0;height:100%;overflow:hidden;background:var(--surface);
    font:13px/1.4 $font;color:var(--ink)}
  /* The box every page draws into.
     `margin` is a share of the frame rather than pixels: these pages embed
     anywhere from ~240px to ~1100px wide, and a fixed inset would swallow a
     small card and vanish in a large one. Percentage padding resolves against
     the width on all four sides, so one number gives a visually even inset.
     It defaults to zero, so a page that asks for nothing is laid out exactly
     as it was before this box existed.
     `container-type: size` makes cqw/cqh available to every page and — the
     point — measures the *padded* box, so a page sizing its own type to the
     container gets the margin accounted for without doing the arithmetic. */
  #page{position:relative;width:100%;height:100%;box-sizing:border-box;
        container-type:size;padding:$pad%}
$style
</style>
</head>
<body>
<div id="page">
$body
</div>
<script>setTimeout(function(){location.reload();}, $refresh_ms);</script>
</body>
</html>
""")


def render_page(
    *, body: str, style: str, options: PageOptions, head: str = ""
) -> str:
    """Wrap a page's own markup and CSS in the shared shell.

    ``head`` is for the rare page that needs more than a stylesheet block —
    the map's Leaflet link, for instance.
    """
    margin = options.clamped_margin
    # `data-theme` is what PALETTE_CSS already keys its overrides on, and what
    # the map reads to pick its basemap. Absent means "follow the viewer",
    # which is the default and the common case.
    theme_attr = f' data-theme="{options.theme}"' if options.theme != "auto" else ""
    return _SHELL.substitute(
        title=options.title or "Health",
        theme_attr=theme_attr,
        palette=PALETTE_CSS,
        font=FONT_STACK,
        head=head,
        style=style,
        body=body,
        pad=round(margin, 2),
        refresh_ms=options.refresh_minutes * 60_000,
    )
