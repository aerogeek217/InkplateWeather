"""Icon names → inline SVG from server/assets/icons, plus the phase-1 icon/modifier rules.

Icons are inlined (never fetched) so the renderer needs no file access for them. Names are paths under
assets/icons without ".svg", e.g. "conditions/thunderstorm".

`assets/icons` is a copy of the icon set in `design/icons`, which is canonical. Icons the boards needed but v1
lacked are part of icon set v1.1 (approved; see design/SPEC.md, Icons): modifiers/sunny-wind,
markers/callout-storm, markers/callout-snow, markers/alert-triangle-inverse.
"""
from __future__ import annotations

import re
from functools import lru_cache
from html import escape

from markupsafe import Markup

from .context import Icon
from .paths import ICONS_DIR

_SVG = re.compile(r"<svg\b([^>]*)>(.*)</svg>\s*$", re.S)
_VIEWBOX = re.compile(r'viewBox="([^"]+)"')

# Composite icons that exist as drawn artwork: (base condition, modifiers) → file.
COMPOSITES: dict[tuple[str, frozenset[str]], str] = {
    ("conditions/cloudy", frozenset({"wind"})): "modifiers/cloudy-wind",
    ("conditions/sunny", frozenset({"wind"})): "modifiers/sunny-wind",
    ("conditions/sleet", frozenset({"wind"})): "modifiers/sleet-wind",
    ("conditions/clear-night", frozenset({"frost"})): "modifiers/clear-night-frost",
    ("conditions/thunderstorm", frozenset({"alert"})): "modifiers/storm-nws-alert",
}

WIND_GUST_MODIFIER_MPH = 20
FROST_MAX_F = 34


class IconNotFound(KeyError):
    pass


@lru_cache(maxsize=256)
def load(name: str) -> tuple[str, str]:
    """(viewBox, inner markup) of an icon file."""
    if ".." in name or name.startswith(("/", "\\")):
        raise IconNotFound(name)
    p = ICONS_DIR / f"{name}.svg"
    if not p.is_file():
        raise IconNotFound(name)
    text = p.read_text(encoding="utf-8").strip()
    m = _SVG.search(text)
    if not m:
        raise ValueError(f"not an svg file: {p}")
    vb = _VIEWBOX.search(m.group(1))
    return (vb.group(1) if vb else "0 0 48 48"), m.group(2)


def exists(name: str) -> bool:
    try:
        load(name)
        return True
    except IconNotFound:
        return False


def inner(name: str) -> Markup:
    return Markup(load(name)[1])


def svg(name: str, width: float, height: float | None = None, label: str = "", style: str = "") -> Markup:
    """Inline <svg> element for an icon. With a label it is role=img, otherwise aria-hidden."""
    vb, body = load(name)
    h = width if height is None else height
    attrs = f'width="{_num(width)}" height="{_num(h)}" viewBox="{vb}"'
    attrs += f' role="img" aria-label="{escape(label)}"' if label else ' aria-hidden="true"'
    if style:
        attrs += f' style="{escape(style)}"'
    return Markup(f"<svg {attrs}>{body}</svg>")


def _num(v: float) -> str:
    return f"{v:g}"


# ------------------------------------------------------------------ choosing icons


def condition(code: str | None) -> str:
    """Condition code (`thunderstorm`, `conditions/rain`) → existing icon name."""
    if not code:
        return "conditions/cloudy"
    name = code if "/" in code else f"conditions/{code}"
    return name if exists(name) else "conditions/cloudy"


def forecast(code: str | None) -> str:
    """Forecast-row icon (same approved set as the header)."""
    return condition(code)


def resolve(icon: Icon) -> str:
    """Icon with modifiers → the file to draw (composite artwork when it exists, else the base)."""
    mods = frozenset(icon.modifiers)
    if not mods:
        return icon.name
    for k in range(len(icon.modifiers), 0, -1):  # prefer the composite with the most modifiers
        for (base, combo), file in COMPOSITES.items():
            if base == icon.name and len(combo) == k and combo <= mods:
                return file
    return icon.name


def header_modifiers(code: str, gust_mph: float | None, temp_f: float | None, alert_active: bool) -> list[str]:
    """Phase-1 modifier rules (SPEC conditional behavior); phase 2.5 refines them."""
    mods = []
    if gust_mph is not None and gust_mph >= WIND_GUST_MODIFIER_MPH:
        mods.append("wind")
    if code.endswith("clear-night") and temp_f is not None and temp_f <= FROST_MAX_F:
        mods.append("frost")
    if alert_active and code.endswith("thunderstorm"):
        mods.append("alert")
    return mods


WIND_DIRS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
WIND_NAMES = {"N": "north", "NE": "northeast", "E": "east", "SE": "southeast", "S": "south",
              "SW": "southwest", "W": "west", "NW": "northwest"}


def wind_dir(from_deg: float | None) -> str:
    if from_deg is None:
        return ""
    return WIND_DIRS[round((from_deg % 360) / 45) % 8]


def wind_arrow(dir_text: str) -> str:
    return f"wind/from-{dir_text.lower()}"


PRECIP_MARKERS = {"rain": ("markers/precip-rain", 8, 11), "snow": ("markers/precip-snow", 11, 11),
                  "ice": ("markers/precip-ice", 10, 10)}
MOON_SMALL = {"full": ("moon/full", "Full moon"), "last": ("moon/last-quarter", "Last quarter"),
              "new": ("moon/new", "New moon"), "first": ("moon/first-quarter", "First quarter")}
CALLOUT = {"storm": ("markers/callout-storm", 22, 30), "snow": ("markers/callout-snow", 28, 28)}
