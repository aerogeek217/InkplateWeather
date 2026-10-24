"""`ecowitt_local` provider: polls an Ecowitt gateway's `GET http://<host>/get_livedata_info` (GW1100/GW2000).

Config: `ecowitt: {type: ecowitt_local, host: 192.168.1.135, timeout: 4s, ttl: 30s}`.

The gateway answers with JSON (served as text/html) whose values are strings with units, e.g. `"2.24 mph"`,
`"72%"`, `"29.62 inHg"`, `"0.00 in/Hr"`, `"26.91 W/m2"`; temperatures carry the unit in a separate
`"unit": "F"` key. Readings are keyed by the gateway's own names (config `key:` refers to these):

- `common_list`: each entry's `id`, verbatim: 0x02 outdoor temp, 0x07 RH, 0x03 dew point, 3 feels-like,
  0x0A wind direction, 0x0B wind, 0x0C gust, 0x19 day max wind, 0x15 solar radiation, 0x17 UV index.
- `rain`: each entry's `id`: 0x0D event, 0x0E rate, 0x10 day, 0x11 week, 0x12 month, 0x13 year.
- `wh25`: `wh25.intemp`, `wh25.inhumi`, `wh25.abs`, `wh25.rel` (indoor temp/RH, absolute/relative pressure).
- `ch_aisle`: `ch<channel>.temp`, `ch<channel>.humidity` (ch1 = garage).

The `lightning` block is ignored by decision (lightning comes from NOAA GLM). Other unknown blocks are ignored.

Values are converted to the canonical units of `app/fields.py` (°F, %, mph, inHg, in, in/h, W/m2, deg; UV
unitless), so a gateway switched to metric (°C, m/s, km/h, knots, hPa, mmHg, mm, mm/Hr, Klux) still works.
A placeholder such as `"--"` (sensor lost) becomes a missing Reading with note `no_value`; an unparsable value
`bad_value`; an unknown unit `unknown_unit`. Network failure, a non-200 answer or bad JSON raises, and the resolver
records it as an error for every field using this provider.
"""
from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from ..config import AppConfig, ProviderCfg, parse_duration
from ..readings import Reading, Status
from .base import RequestContext, register

DEFAULT_TIMEOUT_S = 4.0

# ---------------------------------------------------------------------------------------------------------------
# Unit conversion to the canonical units of app/fields.py. Also used by the home_assistant provider.
# ---------------------------------------------------------------------------------------------------------------

_LUX_PER_WM2 = 126.7  # the approximation Ecowitt itself uses for sunlight

# normalized unit -> (canonical unit, converter, digits to round to after a real conversion)
_UNITS: dict[str, tuple[str, Callable[[float], float], int]] = {}


def _add(names: tuple[str, ...], canonical: str, fn: Callable[[float], float], digits: int) -> None:
    for n in names:
        _UNITS[n] = (canonical, fn, digits)


def _same(v: float) -> float:
    return v


_add(("f", "degf"), "°F", _same, 1)
_add(("c", "degc"), "°F", lambda v: v * 9 / 5 + 32, 1)
_add(("%",), "%", _same, 1)
_add(("mph", "mi/h"), "mph", _same, 2)
_add(("m/s",), "mph", lambda v: v / 0.44704, 2)
_add(("km/h", "kmh", "kph"), "mph", lambda v: v / 1.609344, 2)
_add(("knots", "knot", "kn", "kt", "kts"), "mph", lambda v: v * 1.150779, 2)
_add(("ft/s",), "mph", lambda v: v * 0.681818, 2)
_add(("fpm", "ft/min"), "mph", lambda v: v * 0.0113636, 2)
_add(("inhg",), "inHg", _same, 2)
_add(("hpa", "mbar", "mb"), "inHg", lambda v: v / 33.8639, 2)
_add(("kpa",), "inHg", lambda v: v / 3.38639, 2)
_add(("mmhg",), "inHg", lambda v: v / 25.4, 2)
_add(("pa",), "inHg", lambda v: v / 3386.39, 2)
_add(("in",), "in", _same, 2)
_add(("mm",), "in", lambda v: v / 25.4, 3)
_add(("cm",), "in", lambda v: v / 2.54, 3)
_add(("in/hr", "in/h"), "in/h", _same, 2)
_add(("mm/hr", "mm/h"), "in/h", lambda v: v / 25.4, 3)
_add(("w/m2", "w/m^2"), "W/m2", _same, 1)
_add(("klux",), "W/m2", lambda v: v * 1000 / _LUX_PER_WM2, 1)
_add(("lux", "lx"), "W/m2", lambda v: v / _LUX_PER_WM2, 1)
_add(("kfc",), "W/m2", lambda v: v * 1000 * 10.7639 / _LUX_PER_WM2, 1)
_add(("fc",), "W/m2", lambda v: v * 10.7639 / _LUX_PER_WM2, 1)
_add(("deg",), "deg", _same, 0)


def normalize_unit(unit: str | None) -> str | None:
    """'°C' -> 'c', 'in/Hr' -> 'in/hr', 'W/m²' -> 'w/m2', '' -> None."""
    if unit is None:
        return None
    u = unit.strip().lower().replace("°", "").replace("º", "").replace("²", "2").replace(" ", "")
    u = {"℃": "c", "℉": "f"}.get(u, u)
    return u or None


def to_canonical(value: float, unit: str | None) -> tuple[float, str] | None:
    """Convert a number to its canonical unit, or None when the unit isn't one we know.

    Values already in the canonical unit are returned unchanged (no rounding), converted values are rounded.
    """
    spec = _UNITS.get(normalize_unit(unit) or "")
    if spec is None:
        return None
    canonical, fn, digits = spec
    if fn is _same:
        return value, canonical
    return round(fn(value), digits), canonical


# ---------------------------------------------------------------------------------------------------------------
# Gateway response parsing
# ---------------------------------------------------------------------------------------------------------------

_NUMBER = re.compile(r"^\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))\s*(.*?)\s*$")
# "--", "--.-", "--%", "---.- mph": the gateway's way of saying a sensor has no value.
_PLACEHOLDER = re.compile(r"^\s*(?:-+\.?-*\s*\D*|n/?a|none|null)?\s*$", re.IGNORECASE)

# Unit to assume when a value comes without one. Wind directions are plain degrees; humidity is always %.
# Temperatures normally carry a `unit` key; without one we assume the gateway default °F (note `unit_assumed`).
_TEMP = "F"
_DEFAULT_UNIT = {"0x0A": "deg", "0x6D": "deg", "0x06": "%", "0x07": "%",
                 **{k: _TEMP for k in ("0x01", "0x02", "0x03", "0x04", "0x05", "3", "4")}}


def parse_value(key: str, raw: Any, unit_hint: str | None, observed_at: datetime, source: str,
                default_unit: str | None = None) -> Reading:
    """Turn one gateway value ('2.24 mph', '72%', '59.5' + unit 'F') into a canonical Reading."""
    if raw is None or (isinstance(raw, str) and _PLACEHOLDER.match(raw)):
        return Reading(source=source, status=Status.missing, note="no_value")
    if isinstance(raw, bool):
        return Reading(source=source, status=Status.missing, note="bad_value")
    if isinstance(raw, (int, float)):
        number, unit = float(raw), None
    else:
        m = _NUMBER.match(str(raw))
        if not m:
            return Reading(source=source, status=Status.missing, note="bad_value")
        number, unit = float(m.group(1)), m.group(2) or None
    if not math.isfinite(number):
        return Reading(source=source, status=Status.missing, note="bad_value")
    unit = unit or unit_hint
    note = None
    if unit is None:
        unit = default_unit or _DEFAULT_UNIT.get(key)
        if unit is None:  # UV index, counts and other plain numbers
            return Reading(value=number, unit=None, observed_at=observed_at, source=source)
        if unit == _TEMP:
            note = "unit_assumed"
    conv = to_canonical(number, unit)
    if conv is None:
        return Reading(source=source, status=Status.missing, note="unknown_unit")
    value, canonical = conv
    return Reading(value=value, unit=canonical, observed_at=observed_at, source=source, note=note)


def _entries(data: dict[str, Any], block: str) -> list[dict[str, Any]]:
    items = data.get(block)
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        return []
    return [e for e in items if isinstance(e, dict)]


def _hint(entry: dict[str, Any]) -> str | None:
    u = entry.get("unit")
    return u if isinstance(u, str) and u.strip() else None


def parse_livedata(data: Any, observed_at: datetime, source: str = "ecowitt") -> dict[str, Reading]:
    """Parse a `get_livedata_info` JSON document into Readings keyed by native key."""
    if not isinstance(data, dict):
        raise ValueError(f"unexpected Ecowitt response: expected an object, got {type(data).__name__}")
    out: dict[str, Reading] = {}

    def put(key: str, raw: Any, hint: str | None, default_unit: str | None = None) -> None:
        out[key] = parse_value(key, raw, hint, observed_at, source, default_unit)

    for block in ("common_list", "rain"):
        for e in _entries(data, block):
            key = e.get("id")
            if key is None or "val" not in e:
                continue
            key = str(key)
            put(key, e["val"], _hint(e))

    for e in _entries(data, "wh25"):
        if "intemp" in e:
            put("wh25.intemp", e["intemp"], _hint(e), _TEMP)
        if "inhumi" in e:
            put("wh25.inhumi", e["inhumi"], None, "%")
        for name in ("abs", "rel"):  # pressures always carry their unit
            if name in e:
                put(f"wh25.{name}", e[name], None)

    for e in _entries(data, "ch_aisle"):
        ch = str(e.get("channel", "")).strip()
        if not ch:
            continue
        if "temp" in e:
            put(f"ch{ch}.temp", e["temp"], _hint(e), _TEMP)
        if "humidity" in e:
            put(f"ch{ch}.humidity", e["humidity"], None, "%")

    # "lightning" is deliberately ignored (user decision: lightning comes from NOAA GLM).
    return out


def _timeout(opts: dict[str, Any]) -> float:
    t = opts.get("timeout")
    if t is None:
        return DEFAULT_TIMEOUT_S
    return parse_duration(t).total_seconds()


@register("ecowitt_local")
class EcowittLocalProvider:
    """Config options: `host` (IP or name, optionally with :port), `timeout` (default 4s)."""

    def __init__(self, name: str, cfg: ProviderCfg, app: AppConfig | None = None):
        self.name = name
        self.ttl = cfg.ttl if cfg else timedelta(0)
        opts = cfg.options if cfg else {}
        self.host = str(opts.get("host") or "").strip()
        self.timeout = _timeout(opts)

    @property
    def url(self) -> str:
        base = self.host if self.host.startswith(("http://", "https://")) else f"http://{self.host}"
        return f"{base.rstrip('/')}/get_livedata_info"

    async def fetch(self, ctx: RequestContext) -> dict[str, Reading]:
        if not self.host:
            raise ValueError(f"provider {self.name!r} (ecowitt_local): no `host` configured")
        observed_at = datetime.now(UTC)  # the gateway's live data has no timestamp of its own
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(self.url)
        resp.raise_for_status()
        data = json.loads(resp.text)  # served as text/html, so don't rely on the content type
        return parse_livedata(data, observed_at, source=self.name)
