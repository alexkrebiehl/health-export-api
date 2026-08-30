"""Self-contained HTML page rendering a route-coverage FeatureCollection.

Built to be embedded in a Home Assistant Webpage (``iframe``) card. Two
constraints shaped it:

* **Everything is inlined or same-origin.** The GeoJSON is embedded in the
  document rather than fetched, so the page is one request with no CORS and no
  second round of authentication — an iframe cannot send an Authorization
  header, so a client-side fetch would have nowhere to put the credential.
  Leaflet is served from ``/static`` rather than a CDN.

* **The basemap is the only outbound dependency.** Tiles come from whichever
  provider ``basemap`` selects — CARTO by default, OpenTopoMap for ``topo``,
  both over OpenStreetMap data. Without network access the routes still draw,
  just over an empty background.
"""

from __future__ import annotations

import json
from string import Template
from typing import Any

from health_export_api.page_shell import PageOptions, render_page

# Perceptual ramp from cool to hot, walked from least to most travelled.
_RAMP = [(43, 58, 103), (42, 127, 168), (63, 174, 142), (224, 195, 65), (232, 80, 58)]

_HEAD = '<link rel="stylesheet" href="/static/leaflet.css">'

# Almost nothing about the two providers is interchangeable — the retina
# suffix, the subdomain set, the zoom ceiling and the licence all differ — so
# the choice is a descriptor rather than a URL swap. `urlDark` absent means the
# provider has only the one cartography, drawn as-is; `credits` is
# the plain-text form that survives in the HTML comment when the on-map control
# is hidden, so it carries no markup and no `--`.
DEFAULT_BASEMAP = "street"

_BASEMAPS: dict[str, dict[str, Any]] = {
    "street": {
        "url": "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
        "urlDark": "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
        "subdomains": "abcd",
        "maxNativeZoom": 20,
        "attribution": (
            '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> '
            '&copy; <a href="https://carto.com/attributions">CARTO</a>'
        ),
        "credits": (
            "Basemap tiles (c) CARTO, map data (c) OpenStreetMap contributors.\n"
            "     https://www.openstreetmap.org/copyright https://carto.com/attributions"
        ),
    },
    # OpenTopoMap is CC-BY-SA and requires crediting the *style*, not just the
    # data, which is a stricter ask than CARTO's. It tops out at z17 and serves
    # a repeated placeholder above that, hence maxNativeZoom.
    "topo": {
        "url": "https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
        "subdomains": "abc",
        "maxNativeZoom": 17,
        "attribution": (
            'Map data: &copy; <a href="https://www.openstreetmap.org/copyright">'
            "OpenStreetMap</a> contributors, SRTM | Map style: &copy; "
            '<a href="https://opentopomap.org">OpenTopoMap</a> '
            '(<a href="https://creativecommons.org/licenses/by-sa/3.0/">CC-BY-SA</a>)'
        ),
        "credits": (
            "Map data (c) OpenStreetMap contributors, SRTM.\n"
            "     Map style (c) OpenTopoMap (CC-BY-SA).\n"
            "     https://www.openstreetmap.org/copyright https://opentopomap.org"
        ),
    },
}

# Was a hardcoded slate palette that ignored `theme.py` entirely, which made
# this the one card that never followed the viewer's light/dark setting even
# though its basemap tiles always did.
_STYLE = Template("""  #map{position:absolute;inset:0;background:var(--surface)}
  .leaflet-container{background:var(--surface);font:12px system-ui,sans-serif}
  .info{background:color-mix(in srgb,var(--surface) 82%, transparent);
        color:var(--ink-2);padding:6px 9px;border-radius:6px;
        line-height:1.5;box-shadow:0 1px 4px rgba(0,0,0,.5)}
  .info b{color:var(--ink);font-weight:600}
  .scale{display:flex;align-items:center;gap:5px;margin-top:5px}
  .scale i{width:52px;height:5px;border-radius:3px;display:block;
           background:linear-gradient(90deg,$gradient)}
  .empty{position:absolute;inset:0;display:flex;align-items:center;
         justify-content:center;color:var(--muted);text-align:center;padding:20px}
""")


_BODY = Template("""<!-- $credits
     Kept here so the credit survives even when the on-map control is hidden. -->
<div id="map"></div>
<script type="application/json" id="coverage">$data</script>
<script src="/static/leaflet.js"></script>
<script>
(function () {
  var fc = JSON.parse(document.getElementById('coverage').textContent);
  var meta = fc.properties || {};
  var feats = fc.features || [];

  // A dashboard tile is something you glance at, not something you drive, and
  // a map that swallows scroll is actively annoying inside a scrolling
  // dashboard. Every gesture is opt-in.
  var interactive = $interactive;
  var map = L.map('map', {
    zoomControl: $zoom_control, attributionControl: $attribution,
    dragging: interactive, scrollWheelZoom: interactive,
    doubleClickZoom: interactive, touchZoom: interactive,
    boxZoom: interactive, keyboard: interactive
  });
  // An explicit ?theme wins; otherwise follow the viewer, as before.
  var stamped = document.documentElement.getAttribute('data-theme');
  var dark = stamped ? stamped === 'dark'
    : (!window.matchMedia || window.matchMedia('(prefers-color-scheme: dark)').matches);
  // The provider is chosen server-side, but light-versus-dark cannot be: with
  // ?theme=auto nothing is stamped and only the browser knows. So the server
  // hands over both URLs and this picks. A provider with no dark cartography
  // supplies only `url`, and its tiles are used as drawn.
  var base = $basemap;
  L.tileLayer(dark && base.urlDark ? base.urlDark : base.url, {
    // Above a provider's ceiling Leaflet upscales the last real tile rather
    // than requesting ones that do not exist.
    maxZoom: 20, maxNativeZoom: base.maxNativeZoom,
    subdomains: base.subdomains,
    attribution: base.attribution
  }).addTo(map);

  var stops = $ramp;
  function colour(t) {
    var x = Math.max(0, Math.min(1, t)) * (stops.length - 1);
    var i = Math.min(Math.floor(x), stops.length - 2), k = x - i;
    var a = stops[i], b = stops[i + 1];
    return 'rgb(' + Math.round(a[0] + (b[0] - a[0]) * k) + ',' +
                    Math.round(a[1] + (b[1] - a[1]) * k) + ',' +
                    Math.round(a[2] + (b[2] - a[2]) * k) + ')';
  }

  // Log scale: traversal counts are heavily skewed, so a linear ramp would put
  // almost every path at the cold end.
  var max = 1;
  feats.forEach(function (f) { max = Math.max(max, f.properties.count || 1); });
  var denom = Math.log(max) || 1;
  function level(c) { return Math.log(Math.max(1, c)) / denom; }

  // null unless the caller pinned a weight, in which case every line draws at
  // the same thickness and frequency is carried by colour alone.
  var fixedWeight = $weight;
  function strokeWeight(t) {
    return fixedWeight === null ? 1.2 + t * 4 : fixedWeight;
  }

  // Least-travelled first so the routes that matter draw on top.
  var drawn = [];
  feats.slice().sort(function (a, b) {
    return (a.properties.count || 0) - (b.properties.count || 0);
  }).forEach(function (f) {
    var t = level(f.properties.count || 1);
    // `interactive: false` also spares Leaflet wiring pointer handlers to
    // every path — and there can be thousands of them at a fine tolerance.
    var layer = L.geoJSON(f, {
      interactive: interactive,
      style: { color: colour(t), weight: strokeWeight(t), opacity: 0.9, lineCap: 'round' }
    });
    if (interactive) {
      layer.bindTooltip(
        f.properties.count + '&times; &middot; ' +
        (f.properties.workout_types || []).join(', ') + '<br>' +
        f.properties.first_seen + ' &rarr; ' + f.properties.last_seen
      );
    }
    layer.addTo(map);
    drawn.push(layer);
  });

  var bbox = fc.bbox;
  // Fit the routes, not the query box. The box is a filter and is usually
  // much larger than the area actually walked, which would leave the tile
  // mostly empty map.
  var fitting = false, touched = false;
  function fit() {
    fitting = true;
    if (feats.length) {
      map.fitBounds(L.featureGroup(drawn).getBounds(),
                    { padding: [16, 16], animate: false });
    } else {
      map.setView([(bbox[1] + bbox[3]) / 2, (bbox[0] + bbox[2]) / 2], 14,
                  { animate: false });
    }
    fitting = false;
  }
  fit();
  if (!feats.length) {
    var d = document.createElement('div');
    d.className = 'empty';
    d.textContent = 'No routes recorded in this area yet.';
    document.body.appendChild(d);
  }

  // Leaflet measures the container once, at construction. Embedded in a
  // dashboard the frame is routinely still being laid out — or hidden behind
  // a card visibility condition, so zero-sized — when this runs, and a fit
  // against a zero-sized box clamps to max zoom and stays there. Re-measure
  // and re-fit on every size change instead, which also covers a card that is
  // revealed later or a browser window being resized.
  var box = document.getElementById('map');
  var seenW = box.clientWidth, seenH = box.clientHeight;
  function remeasure() {
    var w = box.clientWidth, h = box.clientHeight;
    if (!w || !h || (w === seenW && h === seenH)) return;
    seenW = w; seenH = h;
    map.invalidateSize({ animate: false });
    // Don't yank the view back from under someone who has panned or zoomed.
    if (!touched) fit();
  }
  map.on('dragstart zoomstart', function () { if (!fitting) touched = true; });
  if (window.ResizeObserver) {
    new ResizeObserver(remeasure).observe(box);
  } else {
    window.addEventListener('resize', remeasure);
  }

  var info = L.control({ position: 'bottomleft' });
  info.onAdd = function () {
    var el = L.DomUtil.create('div', 'info');
    el.innerHTML =
      '<b>' + meta.workout_count + '</b> workouts &middot; <b>' +
      feats.length + '</b> paths' +
      (meta.min_count > 1 ? ' &middot; ' + meta.min_count + '+ passes' : '') +
      '<div class="scale"><span>1</span><i></i><span>' + max + '&times;</span></div>';
    return el;
  };
  info.addTo(map);

})();
</script>
""")


def render_map_page(
    collection: dict[str, Any],
    *,
    title: str = "Exercise coverage",
    zoom_control: bool = False,
    attribution: bool = True,
    interactive: bool = False,
    weight: float | None = None,
    basemap: str = DEFAULT_BASEMAP,
    options: PageOptions = PageOptions(),
) -> str:
    """Render a coverage FeatureCollection as a standalone Leaflet page.

    ``zoom_control`` is off by default: this is a dashboard tile, not a map to
    navigate. Turning it on without ``interactive`` gives buttons that work
    while dragging still does not, which is a reasonable "look closer, but
    stay put" combination.

    ``interactive`` is off by default: panning, zooming and the per-path
    tooltips are all disabled, so the tile cannot swallow a scroll gesture
    inside a scrolling dashboard. It also skips wiring pointer handlers to
    every path, which is not free when there are thousands.

    ``attribution`` is on by default and should stay on. Every provider
    requires credit for its data and tiles, so turning it off is a deliberate
    choice for the caller to make, not a default — and under ``basemap="topo"``
    it is a stricter ask, since OpenTopoMap is CC-BY-SA and wants its *style*
    credited too. The credit stays in an HTML comment either way.

    ``weight`` pins every line to one stroke width. Left unset, width scales
    with traversal count alongside colour; set, frequency is carried by colour
    alone, which reads more evenly when the map is mostly one kind of route.

    ``basemap`` picks the tile provider: ``"street"`` is CARTO, following the
    viewer's light/dark setting as the rest of the page does; ``"topo"`` is
    OpenTopoMap, which draws contours and shaded relief and bakes its own
    streets into the tile. It has only the one cartography, so ``theme`` moves
    the page chrome around it but leaves the tiles alone. An unknown value
    raises ``KeyError`` — the router constrains it to the known set before it
    gets here.
    """
    # `<` only ever appears inside JSON strings, so escaping it keeps the
    # document valid while making it impossible for a workout name to close
    # the <script> block early.
    data = json.dumps(collection, separators=(",", ":")).replace("<", "\\u003c")
    gradient = ",".join(f"rgb({r},{g},{b})" for r, g, b in _RAMP)
    base = _BASEMAPS[basemap]
    return render_page(
        head=_HEAD,
        style=_STYLE.substitute(gradient=gradient),
        body=_BODY.substitute(
            data=data,
            ramp=json.dumps([list(c) for c in _RAMP]),
            zoom_control="true" if zoom_control else "false",
            attribution="true" if attribution else "false",
            interactive="true" if interactive else "false",
            weight="null" if weight is None else repr(float(weight)),
            # Same `<` escape as the collection above: the attribution strings
            # carry anchor markup, and nothing embedded in a <script> block
            # should be able to spell a closing tag.
            basemap=json.dumps(
                {k: v for k, v in base.items() if k != "credits"},
                separators=(",", ":"),
            ).replace("<", "\\u003c"),
            credits=base["credits"],
        ),
        options=options.with_title(title),
    )
