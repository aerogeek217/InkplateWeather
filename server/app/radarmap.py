"""Radar panel SVG: base map + classified echoes + overlays, in the panel's map units (viewBox −214.4 −228 428.8 456).

Drawing order and styles follow the approved boards (SPEC *Radar*):

- intensity legend (fall board): white, lakes, county lines, dashed state line, interstates, echoes, range ring,
  towns, home
- types legend (winter board): the interstates move above the echoes as a thin gray line with a white halo, and
  the home square gets a white outline, so both read on snow dots and ice hatching

The base map comes from `assets/basemap/basemap.geojson` (`tools/build_basemap.py`).
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from xml.sax.saxutils import escape

from .mapgeo import HALF_H, HALF_W, RING_MILES, UNITS_PER_MILE, Extent

BASEMAP = Path(__file__).resolve().parents[1] / "assets" / "basemap" / "basemap.geojson"
FONT = "Inter Tight, Arial, sans-serif"
VIEW = f'x="{-HALF_W:g}" y="{-HALF_H:g}" width="{2 * HALF_W:g}" height="{2 * HALF_H:g}"'

FILLS = {
    "rain-light": "#DBDBDB", "rain-moderate": "#929292", "rain-heavy": "#494949", "rain-extreme": "#000000",
    "rain": "#B6B6B6", "ice": "url(#rdIce)", "snow": "url(#rdSnow)", "snow-heavy": "url(#rdSnowHeavy)",
}
PATTERNS = (
    '<pattern id="rdSnow" width="6" height="6" patternUnits="userSpaceOnUse"><circle cx="3" cy="3" r="1.2" '
    'fill="#000000"></circle></pattern>'
    '<pattern id="rdSnowHeavy" width="3.6" height="3.6" patternUnits="userSpaceOnUse"><rect width="3.6" '
    'height="3.6" fill="#B6B6B6"></rect><circle cx="1.8" cy="1.8" r="1.2" fill="#000000"></circle></pattern>'
    '<pattern id="rdIce" width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
    '<line x1="0" y1="0" x2="0" y2="5" stroke="#000000" stroke-width="1.6"></line></pattern>'
)


@lru_cache(maxsize=4)
def load(path: Path = BASEMAP) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def extent(basemap: dict | None = None) -> Extent:
    """The map is centered where the base map was built (home)."""
    lon, lat = (basemap or load())["properties"]["center"]
    return Extent(lat, lon)


def _f(v: float) -> str:
    s = f"{v:.1f}"
    return "0" if s in ("0.0", "-0.0") else s.removesuffix(".0")


def _path(ext: Extent, lines) -> str:
    out = []
    for line in lines:
        pts = [ext.project(lon, lat) for lon, lat in line]
        out.append("M" + " L".join(f"{_f(x)},{_f(y)}" for x, y in pts))
    return " ".join(out)


@lru_cache(maxsize=4)
def _base(path: Path = BASEMAP) -> dict[str, str]:
    """Base map layers as SVG fragments (cached; the map never changes at run time)."""
    bm = load(path)
    ext = extent(bm)
    by: dict[str, list] = {}
    for f in bm["features"]:
        by.setdefault(f["properties"]["layer"], []).append(f)

    def lines(layer: str) -> str:
        return " ".join(_path(ext, f["geometry"]["coordinates"]) for f in by.get(layer, []))

    water = " ".join(_path(ext, [ring for poly in f["geometry"]["coordinates"] for ring in poly]) + " Z"
                     for f in by.get("water", []))
    state = lines("state")
    roads = lines("interstate")
    places = []
    for f in by.get("place", []):
        p = f["properties"]
        x, y = ext.project(*f["geometry"]["coordinates"])
        if p["dot"]:
            places.append(f'<circle cx="{_f(x)}" cy="{_f(y)}" r="{p["dot"]}"></circle>')
        anchor = "" if p["anchor"] == "start" else f' text-anchor="{p["anchor"]}"'
        places.append(f'<text x="{_f(x + p["dx"])}" y="{_f(y + p["dy"])}"{anchor}>{escape(p["name"])}</text>')
    return {
        "under": (f'<rect {VIEW} fill="#FFFFFF"></rect>'
                  + (f'<path d="{water}" fill="#929292"></path>' if water else "")
                  + f'<path d="{lines("county")}" fill="none" stroke="#DBDBDB" stroke-width="1"></path>'
                  + (f'<path d="{state}" fill="none" stroke="#FFFFFF" stroke-width="1.6"></path>'
                     f'<path d="{state}" fill="none" stroke="#929292" stroke-width="1.2" stroke-dasharray="8 4">'
                     '</path>' if state else "")),
        "roads": f'<path d="{roads}" fill="none" stroke="#B6B6B6" stroke-width="3.5" stroke-linejoin="round"></path>',
        "roads_over": (f'<path d="{roads}" fill="none" stroke="#FFFFFF" stroke-width="5" stroke-linejoin="round">'
                       f'</path><path d="{roads}" fill="none" stroke="#6D6D6D" stroke-width="2" '
                       'stroke-linejoin="round"></path>'),
        "places": "".join(places),
    }


def echoes(frame: dict) -> str:
    out = []
    for i, layer in enumerate(frame.get("layers") or []):
        fill = FILLS.get(layer.get("fill", ""))
        if not fill or not layer.get("png"):
            continue
        out.append(f'<mask id="rdM{i}" maskUnits="userSpaceOnUse" {VIEW}><image href="data:image/png;base64,'
                   f'{layer["png"]}" {VIEW} preserveAspectRatio="none" style="image-rendering: pixelated">'
                   f'</image></mask><rect {VIEW} fill="{fill}" mask="url(#rdM{i})"></rect>')
    return "".join(out)


def layers(frame: dict | None, home_name: str, home_xy: tuple[float, float] = (0.0, 0.0),
           path: Path = BASEMAP) -> str:
    """The SVG inside the radar panel's <svg>: base map, echoes (if any), overlays."""
    base = _base(path)
    typed = bool(frame) and frame.get("legend") == "types"
    r = RING_MILES * UNITS_PER_MILE
    hx, hy = home_xy
    home_stroke = ' stroke="#FFFFFF" stroke-width="2"' if typed else ""
    return "\n".join(x for x in (
        f"<defs>{PATTERNS}</defs>" if typed else "",
        base["under"],
        "" if typed else base["roads"],
        echoes(frame) if frame else "",
        base["roads_over"] if typed else "",
        f'<circle cx="{_f(hx)}" cy="{_f(hy)}" r="{_f(r)}" fill="none" stroke="#6D6D6D" stroke-width="1"></circle>',
        f'<g font-family="{FONT}" font-size="11.5" font-weight="600" fill="#000000" stroke="#FFFFFF" '
        f'stroke-width="4" paint-order="stroke">{base["places"]}'
        f'<text x="{_f(hx + 102)}" y="{_f(hy - 100)}" font-weight="400" fill="#494949">{RING_MILES} mi</text></g>',
        f'<rect x="{_f(hx - 6)}" y="{_f(hy - 6)}" width="12" height="12" fill="#000000"{home_stroke}></rect>',
        f'<text x="{_f(hx + 12)}" y="{_f(hy + 15)}" font-family="{FONT}" font-size="11.5" font-weight="800" '
        f'fill="#000000" stroke="#FFFFFF" stroke-width="4" paint-order="stroke">{escape(home_name)}</text>',
    ) if x)
