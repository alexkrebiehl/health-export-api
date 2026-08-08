"""Self-contained HTML page rendering a route-coverage FeatureCollection.

Built to be embedded in a Home Assistant Webpage (``iframe``) card. Two
constraints shaped it:

* **Everything is inlined or same-origin.** The GeoJSON is embedded in the
  document rather than fetched, so the page is one request with no CORS and no
  second round of authentication — an iframe cannot send an Authorization
  header, so a client-side fetch would have nowhere to put the credential.
  Leaflet is served from ``/static`` rather than a CDN.

* **The basemap is the only outbound dependency.** Tiles come from CARTO
  (OpenStreetMap data). Without network access the routes still draw, just
  over an empty background.
"""

from __future__ import annotations

import json
from string import Template
from typing import Any

# Perceptual ramp from cool to hot, walked from least to most travelled.
_RAMP = [(43, 58, 103), (42, 127, 168), (63, 174, 142), (224, 195, 65), (232, 80, 58)]

_TEMPLATE = Template("""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>$title</title>
<link rel="stylesheet" href="/static/leaflet.css">
<style>
  html,body{margin:0;height:100%;background:#0b0e14}
  #map{position:absolute;inset:0;background:#0b0e14}
  .leaflet-container{background:#0b0e14;font:12px system-ui,sans-serif}
  .info{background:rgba(11,14,20,.82);color:#c9d1d9;padding:6px 9px;border-radius:6px;
        line-height:1.5;box-shadow:0 1px 4px rgba(0,0,0,.5)}
  .info b{color:#e6edf3;font-weight:600}
  .scale{display:flex;align-items:center;gap:5px;margin-top:5px}
  .scale i{width:52px;height:5px;border-radius:3px;display:block;
           background:linear-gradient(90deg,$gradient)}
  .empty{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
         color:#8b949e;font:14px system-ui,sans-serif;text-align:center;padding:20px}
</style>
</head>
<body>
<!-- Basemap tiles (c) CARTO, map data (c) OpenStreetMap contributors.
     https://www.openstreetmap.org/copyright https://carto.com/attributions
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
  var dark = !window.matchMedia || window.matchMedia('(prefers-color-scheme: dark)').matches;
  L.tileLayer(
    'https://{s}.basemaps.cartocdn.com/' + (dark ? 'dark_all' : 'light_all') +
    '/{z}/{x}/{y}{r}.png',
    { maxZoom: 20, subdomains: 'abcd',
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> ' +
                   '&copy; <a href="https://carto.com/attributions">CARTO</a>' }
  ).addTo(map);

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
  if (feats.length) {
    // Fit the routes, not the query box. The box is a filter and is usually
    // much larger than the area actually walked, which would leave the tile
    // mostly empty map.
    map.fitBounds(L.featureGroup(drawn).getBounds(), { padding: [16, 16] });
  } else {
    map.setView([(bbox[1] + bbox[3]) / 2, (bbox[0] + bbox[2]) / 2], 14);
    var d = document.createElement('div');
    d.className = 'empty';
    d.textContent = 'No routes recorded in this area yet.';
    document.body.appendChild(d);
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

  // New workouts arrive by export, so refresh periodically rather than leaving
  // a stale tile up indefinitely.
  setTimeout(function () { location.reload(); }, $refresh_ms);
})();
</script>
</body>
</html>
""")


def render_map_page(
    collection: dict[str, Any],
    *,
    title: str = "Exercise coverage",
    refresh_minutes: int = 30,
    zoom_control: bool = False,
    attribution: bool = True,
    interactive: bool = False,
    weight: float | None = None,
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

    ``attribution`` is on by default and should stay on. OpenStreetMap and
    CARTO both require credit for their data and tiles, so turning it off is a
    deliberate choice for the caller to make, not a default. The credit stays
    in an HTML comment either way.

    ``weight`` pins every line to one stroke width. Left unset, width scales
    with traversal count alongside colour; set, frequency is carried by colour
    alone, which reads more evenly when the map is mostly one kind of route.
    """
    # `<` only ever appears inside JSON strings, so escaping it keeps the
    # document valid while making it impossible for a workout name to close
    # the <script> block early.
    data = json.dumps(collection, separators=(",", ":")).replace("<", "\\u003c")
    gradient = ",".join(f"rgb({r},{g},{b})" for r, g, b in _RAMP)
    return _TEMPLATE.substitute(
        title=title,
        data=data,
        ramp=json.dumps([list(c) for c in _RAMP]),
        gradient=gradient,
        refresh_ms=refresh_minutes * 60_000,
        zoom_control="true" if zoom_control else "false",
        attribution="true" if attribution else "false",
        interactive="true" if interactive else "false",
        weight="null" if weight is None else repr(float(weight)),
    )
