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
import math
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from xml.sax.saxutils import escape

from .mapgeo import HALF_H, HALF_W, RING_MILES, UNITS_PER_MILE, Extent
from .textwidth import measure

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


# Lightning bolt (icons/markers/lightning-*.svg, viewBox 16 18.5 22 31) drawn 12 px (9.6 map units) tall.
BOLT_PATH = "M26 21 H35 L28 31 H34 L22 47 L26 35 H19 Z"
BOLT_SCALE = 9.6 / 26  # the path itself is 26 of the 31 icon units tall
BOLT_CELL = 6.4  # map units (8 px): flashes closer than this merge into one bolt, newest wins
RECENT = timedelta(minutes=10)
WINDOW = timedelta(minutes=30)


def bolts(flashes: list[dict], now: datetime, ext: Extent) -> list[tuple[float, float, bool]]:
    """Flashes of the last 30 min → [(x, y, recent)], one per ~8 px cell (the newest), older ones first."""
    cells: dict[tuple[int, int], tuple[datetime, float, float]] = {}
    for f in flashes:
        try:
            t = datetime.fromisoformat(f["t"])
            x, y = ext.project(float(f["lon"]), float(f["lat"]))
        except (KeyError, TypeError, ValueError):
            continue
        age = now - t
        if not (timedelta(minutes=-1) <= age <= WINDOW) or abs(x) > HALF_W or abs(y) > HALF_H:
            continue
        key = (int((x + HALF_W) // BOLT_CELL), int((y + HALF_H) // BOLT_CELL))
        if key not in cells or t > cells[key][0]:
            cells[key] = (t, x, y)
    marks = [(x, y, now - t <= RECENT, t) for t, x, y in cells.values()]
    marks.sort(key=lambda m: m[3])
    return [(x, y, recent) for x, y, recent, _ in marks]


def _bolt(x: float, y: float, recent: bool) -> str:
    style = ('fill="#000000" stroke="#FFFFFF" stroke-width="2.5" paint-order="stroke"' if recent
             else 'fill="#FFFFFF" stroke="#000000" stroke-width="2"')
    return (f'<path d="{BOLT_PATH}" transform="translate({_f(x)} {_f(y)}) scale({BOLT_SCALE:.4f}) translate(-27 -34)" '
            f'{style} stroke-linejoin="round"></path>')


# Storm-motion overlays (app.motion), styled like the boards: dashed arrival / snow lines with black time tags,
# a bold current snow line, the motion arrow with its speed.
TAG_SIZE, TAG_WEIGHT, TAG_PAD, TAG_H = 10, 800, 12, 14


def _tag_text(t: datetime) -> str:
    """'3 PM', '3:30 PM' (tags keep '12 PM' rather than 'noon': same shape as the others)."""
    m = f":{t.minute:02d}" if t.minute else ""
    return f"{t.hour % 12 or 12}{m} {'AM' if t.hour < 12 else 'PM'}"


def _tag(x: float, y: float, text: str) -> str:
    w = round(measure(text, TAG_SIZE, TAG_WEIGHT) + TAG_PAD)
    return (f'<g transform="translate({_f(x)},{_f(y)})"><rect x="{_f(-w / 2)}" y="-8" width="{w}" height="{TAG_H}" '
            f'fill="#000000"></rect><text y="3" fill="#FFFFFF">{escape(text)}</text></g>')


def _future(items: list[dict], now: datetime | None) -> list[tuple[datetime, dict]]:
    out = []
    for it in items or []:
        try:
            t = datetime.fromisoformat(it["t"])
        except (KeyError, TypeError, ValueError):
            continue
        if now is None or t > now:
            out.append((t, it))
    return out


def _points(pts) -> str:
    return "M" + " L".join(f"{_f(x)},{_f(y)}" for x, y in pts)


def motion_layers(frame: dict | None, now: datetime | None) -> dict[str, str]:
    """SVG fragments for the frame's motion overlays (lines that are already in the past are left out):
    `snow_line` (under the winter roads), `lines` (dashed lines + tags), `arrow`, `labels` (for the halo group)."""
    ov = (frame or {}).get("overlays") or {}
    out = {"snow_line": "", "lines": "", "arrow": "", "labels": ""}
    paths, tags = [], []
    for t, a in _future(ov.get("arrivals"), now):
        paths.append(f'<path d="M{_f(a["x1"])},{_f(a["y1"])} L{_f(a["x2"])},{_f(a["y2"])}"></path>')
        tags.append(_tag(*a["tag"], _tag_text(t)))
    width = "1.3"
    snow = ov.get("snow_line")
    if snow:
        width = "1.4"
        out["snow_line"] = (f'<path d="{_points(snow)}" fill="none" stroke="#000000" stroke-width="2" '
                            'stroke-linejoin="round"></path>')
        if ov.get("snow_label"):
            x, y = ov["snow_label"]
            out["labels"] = f'<text x="{_f(x)}" y="{_f(y)}" text-anchor="middle" font-weight="800">snow line</text>'
        for t, s in _future(ov.get("snow_lines"), now):
            paths.append(f'<path d="{_points(s["points"])}"></path>')
            tags.append(_tag(*s["tag"], f"SNOW {_tag_text(t)}"))
    if paths:
        out["lines"] = (f'<g fill="none" stroke="#000000" stroke-width="{width}" stroke-dasharray="6 4">'
                        + "".join(paths) + "</g>"
                        f'<g font-family="{FONT}" font-size="{TAG_SIZE}" font-weight="{TAG_WEIGHT}" '
                        'text-anchor="middle">' + "".join(tags) + "</g>")
    ar = ov.get("arrow")
    if ar:
        x1, y1, x2, y2 = ar["x1"], ar["y1"], ar["x2"], ar["y2"]
        d = math.hypot(x2 - x1, y2 - y1) or 1.0
        ux, uy = (x2 - x1) / d, (y2 - y1) / d
        bx, by = x2 - 14 * ux, y2 - 14 * uy  # head: 14 long, 15 wide (fall board)
        sx, sy = x2 - 11 * ux, y2 - 11 * uy
        out["arrow"] = (f'<path d="M{_f(x1)},{_f(y1)} L{_f(sx)},{_f(sy)}" fill="none" stroke="#000000" '
                        'stroke-width="3.2" stroke-linecap="round"></path>'
                        f'<path d="M{_f(x2)},{_f(y2)} L{_f(bx - 7.5 * uy)},{_f(by + 7.5 * ux)} '
                        f'L{_f(bx + 7.5 * uy)},{_f(by - 7.5 * ux)} Z" fill="#000000"></path>')
        anchor = "" if ar.get("anchor", "start") == "start" else f' text-anchor="{ar["anchor"]}"'
        out["labels"] += f'<text x="{_f(ar["lx"])}" y="{_f(ar["ly"])}"{anchor}>{escape(ar["label"])}</text>'
    return out


def layers(frame: dict | None, home_name: str, home_xy: tuple[float, float] = (0.0, 0.0),
           marks: list[tuple[float, float, bool]] | None = None, path: Path = BASEMAP,
           now: datetime | None = None) -> str:
    """The SVG inside the radar panel's <svg>: base map, echoes (if any), motion overlays, lightning bolts."""
    base = _base(path)
    typed = bool(frame) and frame.get("legend") == "types"
    r = RING_MILES * UNITS_PER_MILE
    hx, hy = home_xy
    home_stroke = ' stroke="#FFFFFF" stroke-width="2"' if typed else ""
    mo = motion_layers(frame, now)
    return "\n".join(x for x in (
        f"<defs>{PATTERNS}</defs>" if typed else "",
        base["under"],
        "" if typed else base["roads"],
        echoes(frame) if frame else "",
        mo["snow_line"],
        base["roads_over"] if typed else "",
        mo["lines"],
        f'<circle cx="{_f(hx)}" cy="{_f(hy)}" r="{_f(r)}" fill="none" stroke="#6D6D6D" stroke-width="1"></circle>',
        mo["arrow"],
        "".join(_bolt(*m) for m in marks or ()),
        f'<g font-family="{FONT}" font-size="11.5" font-weight="600" fill="#000000" stroke="#FFFFFF" '
        f'stroke-width="4" paint-order="stroke">{mo["labels"]}{base["places"]}'
        f'<text x="{_f(hx + 102)}" y="{_f(hy - 100)}" font-weight="400" fill="#494949">{RING_MILES} mi</text></g>',
        f'<rect x="{_f(hx - 6)}" y="{_f(hy - 6)}" width="12" height="12" fill="#000000"{home_stroke}></rect>',
        f'<text x="{_f(hx + 12)}" y="{_f(hy + 15)}" font-family="{FONT}" font-size="11.5" font-weight="800" '
        f'fill="#000000" stroke="#FFFFFF" stroke-width="4" paint-order="stroke">{escape(home_name)}</text>',
    ) if x)
