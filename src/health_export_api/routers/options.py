"""Query parameters shared by every render endpoint.

Declared once here and pulled in with ``Depends``, so adding a render endpoint
is one dependency rather than a copy of three params and a ``<head>``. The
alternative — which this replaces — was `refresh_minutes` written out three
times identically while `margin` and `title` existed on exactly one endpoint
each, for no reason other than which card needed them first.

The dataclasses these build live in :mod:`health_export_api.page_shell`, which
imports no web framework, so the page modules can take them without depending
on FastAPI.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Query

from health_export_api.page_shell import (
    DEFAULT_REFRESH_MINUTES,
    MAX_MARGIN,
    PageOptions,
)


@dataclass(frozen=True)
class Timeframe:
    """The span a page covers, as the caller wrote it.

    Left unresolved on purpose: each endpoint calls ``provider.resolve_range``
    with its own default, because the chart falls back to ninety days and the
    map to all time.
    """

    date_range: str | None = None
    start_date: str | None = None
    end_date: str | None = None


def page_options(
    title: str | None = Query(default=None),
    refresh_minutes: int = Query(default=DEFAULT_REFRESH_MINUTES, ge=1, le=1440),
    margin: float = Query(default=0.0, ge=0, le=MAX_MARGIN),
    theme: str = Query(default="auto", pattern="^(auto|light|dark)$"),
) -> PageOptions:
    """Options meaningful on any rendered page.

    ``theme`` forces light or dark instead of following the viewer's setting;
    ``margin`` pads the contents as a share of the frame; ``title`` names the
    document. Each endpoint supplies its own title default via
    :meth:`PageOptions.with_title`.
    """
    return PageOptions(
        title=title or "",
        refresh_minutes=refresh_minutes,
        margin=margin,
        theme=theme,  # type: ignore[arg-type]
    )


def timeframe(
    date_range: str | None = Query(default=None),
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
) -> Timeframe:
    """The date span, for endpoints that plot one.

    Not on the stat tile, which scopes itself with ``window`` instead.
    """
    return Timeframe(date_range=date_range, start_date=start_date, end_date=end_date)


PageDep = Depends(page_options)
SpanDep = Depends(timeframe)
