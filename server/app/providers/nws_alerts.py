"""`nws_alerts`: active NWS alerts for the configured location → `alerts.active` (shapes.Alert list).

One request per refresh: `/alerts/active?point=<lat>,<lon>`. A point query is used rather than `?zone=<forecast
zone>,<county>` because NWS matches it against both the zones (forecast zone, county, fire zone) and the polygons of
storm-based warnings: a tornado or severe thunderstorm warning shows only when its polygon covers the house, not for
the whole county, and it needs no `/points` lookup first (one request fewer and one failure mode fewer). If the point
query fails and a cached /points result exists, the zone + county query is tried once as a fallback.

Processing: drop non-"Actual" messages, cancellations and alerts that have ended; drop alerts that a newer one in the
set references (updates supersede); keep one alert per event (the most severe, then the latest ending); sort most
severe first (severity, then warning > watch > advisory > statement, then the earliest onset). `impact` is a short key
impact parsed from the description when there is one (see `impact()`). No alerts → an ok Reading with [].
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from .. import shapes
from ..config import AppConfig, ProviderCfg
from ..readings import Reading
from .base import RequestContext, register
from .nws_common import NwsError, cached_point, client, get_json, parse_time, point_key

SEVERITY_RANK = {"Extreme": 4, "Severe": 3, "Moderate": 2, "Minor": 1, "Unknown": 0}
KIND_RANK = (("warning", 0), ("emergency", 0), ("watch", 1), ("advisory", 2), ("statement", 3))
MINUS = "−"
DASH = "–"


@register("nws_alerts")
class NwsAlertsProvider:
    """Config: `nws_alerts: {type: nws_alerts, ttl: 5m}` (location from `location`)."""

    def __init__(self, name: str, cfg: ProviderCfg, app: AppConfig):
        self.name = name
        self.ttl = cfg.ttl
        self.app = app

    async def fetch(self, ctx: RequestContext) -> dict[str, Reading]:
        key = point_key(self.app.location.lat, self.app.location.lon)
        async with client(self.app) as http:
            try:
                doc = await get_json(http, "/alerts/active", params={"point": key})
            except NwsError:
                pt = cached_point(self.app)
                if pt is None or not (pt.forecast_zone and pt.county):
                    raise
                doc = await get_json(http, "/alerts/active", params={"zone": f"{pt.forecast_zone},{pt.county}"})
        alerts = parse(doc, ctx.now)
        return {"alerts.active": Reading(value=shapes.dump(alerts), observed_at=ctx.now, source=self.name)}


def _ends(p: dict[str, Any]) -> datetime | None:
    ending = ((p.get("parameters") or {}).get("eventEndingTime") or [None])[0]
    return parse_time(p.get("ends")) or parse_time(ending) or parse_time(p.get("expires"))


def _kind_rank(event: str) -> int:
    e = event.lower()
    return next((r for word, r in KIND_RANK if word in e), 4)


def parse(doc: dict[str, Any], now: datetime) -> list[shapes.Alert]:
    props = [f.get("properties") or {} for f in doc.get("features") or [] if isinstance(f, dict)]
    live = []
    for p in props:
        if p.get("status", "Actual") != "Actual" or p.get("messageType") == "Cancel" or not p.get("event"):
            continue
        ends = _ends(p)
        if ends is not None and ends <= now:
            continue
        live.append((p, ends))
    referenced = {r.get("identifier") or r.get("@id") for p, _ in live for r in p.get("references") or []}
    live = [(p, e) for p, e in live if p.get("id") not in referenced and p.get("@id") not in referenced]

    best: dict[str, tuple[dict[str, Any], datetime | None]] = {}
    for p, ends in live:
        cur = best.get(p["event"])
        if cur is None or _dedup_key(p, ends) > _dedup_key(*cur):
            best[p["event"]] = (p, ends)

    alerts = []
    for p, ends in best.values():
        sev = p.get("severity") if p.get("severity") in SEVERITY_RANK else "Unknown"
        alerts.append(shapes.Alert(
            event=p["event"],
            ends=ends,
            onset=parse_time(p.get("onset")) or parse_time(p.get("effective")),
            headline=p.get("headline") or "",
            impact=impact(p["event"], " ".join(x for x in (p.get("description"), p.get("headline")) if x)),
            severity=sev,
            url=p.get("@id") or p.get("web"),
        ))
    far = datetime.max.replace(tzinfo=now.tzinfo)
    alerts.sort(key=lambda a: (-SEVERITY_RANK[a.severity], _kind_rank(a.event), a.onset or far))
    return alerts


def _dedup_key(p: dict[str, Any], ends: datetime | None) -> tuple:
    return (SEVERITY_RANK.get(p.get("severity") or "", 0), ends.timestamp() if ends else float("inf"))


# ------------------------------------------------------------------ impact

_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
          "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}
_N = r"(\d+(?:\.\d+)?|a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
_FRACTIONS = (  # spoken ice amounts, most specific first
    (re.compile(r"\b(one|two|three|four|five)\s+(?:to|and)\s+(two|three|four|five|six)\s+tenths?\b"), None),
    (re.compile(r"\b(?:a|one)\s+tenth\b"), 0.1), (re.compile(r"\btwo\s+tenths\b"), 0.2),
    (re.compile(r"\bthree\s+tenths\b"), 0.3), (re.compile(r"\bfour\s+tenths\b"), 0.4),
    (re.compile(r"\bthree\s+quarters?\b"), 0.75), (re.compile(r"\b(?:a|one)\s+quarter\b"), 0.25),
    (re.compile(r"\b(?:a|one)\s+third\b"), 0.33), (re.compile(r"\b(?:a|one)\s+half\b|\bhalf\s+an?\b"), 0.5),
)


def _num(s: str) -> float:
    return float(_WORDS[s]) if s in _WORDS else float(s)


def _fmt(v: float) -> str:
    return f"{v:g}"


def _sentence_after(text: str, rx: re.Pattern) -> str | None:
    m = rx.search(text)
    if not m:
        return None
    rest = text[m.end():]
    end = re.search(r"\.(?:\s|$)|;|\*", rest)
    return rest[: end.start()] if end else rest


_SNOW_CLAUSE = re.compile(r"\bsnow(?:fall)?(?: and sleet)? accumulations?\b")
_SNOW_INLINE = re.compile(rf"\b{_N}\s+(?:to|-)\s+{_N}\s+inch(?:es)?\s+of\s+(?:heavy\s+|wet\s+)?snow\b")


def _snow(text: str) -> str | None:
    if m := _SNOW_INLINE.search(text):
        return f"{_fmt(_num(m.group(1)))}{DASH}{_fmt(_num(m.group(2)))} in snow"
    clause = _sentence_after(text, _SNOW_CLAUSE)
    if clause is None:
        return None
    if m := re.search(rf"\b{_N}\s+(?:to|and|-)\s+{_N}\s+inch", clause):
        lo, hi = _num(m.group(1)), _num(m.group(2))
        return f"{_fmt(lo)}{DASH}{_fmt(hi)} in snow"
    if m := re.search(rf"\b(?:dusting|trace)\s+(?:to|and)\s+(?:up to\s+)?{_N}\s+inch", clause):
        return f"up to {_fmt(_num(m.group(1)))} in snow"
    if m := re.search(rf"\bup to\s+{_N}\s+inch", clause):
        return f"up to {_fmt(_num(m.group(1)))} in snow"
    if m := re.search(rf"\b{_N}\s+inch", clause):
        return f"{_fmt(_num(m.group(1)))} in snow"
    return None


_ICE_CLAUSE = re.compile(r"\bice accumulations?\b")


def _ice(text: str) -> str | None:
    clause = _sentence_after(text, _ICE_CLAUSE)
    if clause is None:
        return None
    if m := re.search(r"\b(\d*\.\d+|\d+)\s*(?:to|and|-)\s*(\d*\.\d+|\d+)\s+(?:of an\s+)?inch", clause):
        return f"ice {_fmt(float(m.group(1)))}{DASH}{_fmt(float(m.group(2)))} in"
    if m := re.search(r"\b(\d*\.\d+|\d+)\s+(?:of an\s+)?inch", clause):
        return f"ice {_fmt(float(m.group(1)))} in"
    found: list[float] = []
    for rx, v in _FRACTIONS:
        if m := rx.search(clause):
            if v is None:
                return f"ice {_fmt(_num(m.group(1)) / 10)}{DASH}{_fmt(_num(m.group(2)) / 10)} in"
            clause = clause[: m.start()] + " " + clause[m.end():]  # "three quarters" must not also be "a quarter"
            found.append(v)
    if found:
        lo, hi = min(found), max(found)
        return f"ice {_fmt(hi)} in" if lo == hi else f"ice {_fmt(lo)}{DASH}{_fmt(hi)} in"
    if re.search(r"\bglaze\b", clause):
        return None  # "a light glaze": nothing worth a number
    if re.search(r"\b(?:an|one)\s+inch\b", clause):
        return "ice 1 in"
    return None


_GUST = re.compile(r"\bgust(?:s|ing)?\b[^.\d]{0,40}?(\d+)(?:\s*(?:to|and|-)\s*(\d+))?\s*mph")
_GUST_BEFORE = re.compile(r"\b(\d+)\s*mph\s+(?:wind\s+)?gusts\b")


def _wind(text: str) -> str | None:
    speeds = [int(x) for m in _GUST.finditer(text) for x in m.groups() if x]
    speeds += [int(m.group(1)) for m in _GUST_BEFORE.finditer(text)]
    return f"gusts to {max(speeds)} mph" if speeds else None


_CHILL = re.compile(r"\bwind chills?(?: values)?\b[^.\d]{0,30}?(-?\d+)\s*(below|above)?(?:\s*zero)?")


def _chill(text: str) -> str | None:
    vals = []
    for m in _CHILL.finditer(text):
        v = int(m.group(1))
        vals.append(-abs(v) if m.group(2) == "below" else v)
    if not vals:
        return None
    v = min(vals)
    return f"wind chill {MINUS}{abs(v)}°" if v < 0 else f"wind chill {v}°"


_HEAT = re.compile(r"\bheat index(?: values)?\b[^.\d]{0,30}?(\d+)(?:\s*(?:to|and|-)\s*(\d+))?")


def _heat(text: str) -> str | None:
    vals = [int(x) for m in _HEAT.finditer(text) for x in m.groups() if x]
    return f"heat index {max(vals)}°" if vals else None


_HAIL_SIZES = {"pea": 0.25, "half inch": 0.5, "penny": 0.75, "nickel": 0.88, "quarter": 1.0, "half dollar": 1.25,
               "ping pong ball": 1.5, "golf ball": 1.75, "hen egg": 2.0, "egg": 2.0, "tennis ball": 2.5,
               "baseball": 2.75, "tea cup": 3.0, "grapefruit": 4.0, "softball": 4.0}
_HAIL = re.compile(r"\b(" + "|".join(sorted(map(re.escape, _HAIL_SIZES), key=len, reverse=True)) + r") size hail")


def _hail(text: str) -> str | None:
    sizes = [_HAIL_SIZES[m.group(1)] for m in _HAIL.finditer(text)]
    return f"hail {_fmt(max(sizes))} in" if sizes else None


_LOW = re.compile(r"\btemperatures?\b[^.\d]{0,30}?(?:as low as|of|near|around|in the)\s+(?:lower\s+|mid\s+|upper\s+)?"
                  r"(-?\d+)")


def _low(text: str) -> str | None:
    vals = [int(m.group(1)) for m in _LOW.finditer(text)]
    if not vals:
        return None
    v = min(vals)
    return f"low {MINUS}{abs(v)}°" if v < 0 else f"low {v}°"


_RULES = {"snow": _snow, "ice": _ice, "wind": _wind, "chill": _chill, "heat": _heat, "hail": _hail, "low": _low}


def impact(event: str, description: str) -> str | None:
    """The key impact for the header, or None. The event decides which kind is looked for first:

    ice storm / freezing → ice; wind chill / cold → wind chill; freeze / frost → low temperature; heat → heat index;
    (high) wind → gusts;
    thunderstorm / tornado → hail, then gusts; everything else → snow, ice, gusts, wind chill, heat index, hail.
    """
    e = event.lower()
    if "ice storm" in e or "freezing" in e or "sleet" in e:
        order = ["ice", "snow", "wind"]
    elif "chill" in e or "cold" in e:
        order = ["chill", "low"]
    elif "freeze" in e or "frost" in e:
        order = ["low"]
    elif "heat" in e:
        order = ["heat"]
    elif "thunderstorm" in e or "tornado" in e:
        order = ["hail", "wind"]
    elif "wind" in e:
        order = ["wind"]
    else:
        order = ["snow", "ice", "wind", "chill", "heat", "hail"]
    text = " ".join(description.lower().split())
    for kind in order:
        if found := _RULES[kind](text):
            return found
    return None
