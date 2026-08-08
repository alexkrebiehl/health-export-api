"""Shared colour tokens for the embeddable pages.

Values come from the `dataviz` reference palette. Both modes are *selected* —
the dark column is the same hues stepped for the dark surface, not an automatic
inversion of the light one.

The dark values are declared under two scopes on purpose: the media query
covers the viewer's OS setting, and the `[data-theme]` scope covers an explicit
theme choice, which has to win in both directions. The `:not(...)` guard lets a
light stamp beat OS-dark.

Kept here rather than in one page module so the chart and the stat tiles cannot
drift apart.
"""

from __future__ import annotations

PALETTE_CSS = """
  :root{
    color-scheme: light;
    --surface:#fcfcfb; --ink:#0b0b0b; --ink-2:#52514e; --muted:#898781;
    --grid:#e1e0d9; --axis:#c3c2b7; --raw:#898781; --trend:#2a78d6;
    --good:#006300;
  }
  @media (prefers-color-scheme: dark){
    :root:not([data-theme="light"]){
      color-scheme: dark;
      --surface:#1a1a19; --ink:#ffffff; --ink-2:#c3c2b7; --muted:#898781;
      --grid:#2c2c2a; --axis:#383835; --raw:#898781; --trend:#3987e5;
      --good:#0ca30c;
    }
  }
  :root[data-theme="dark"]{
    color-scheme: dark;
    --surface:#1a1a19; --ink:#ffffff; --ink-2:#c3c2b7; --muted:#898781;
    --grid:#2c2c2a; --axis:#383835; --raw:#898781; --trend:#3987e5;
    --good:#0ca30c;
  }
"""

# The system sans, everywhere — no display or serif face.
FONT_STACK = 'system-ui,-apple-system,"Segoe UI",sans-serif'
