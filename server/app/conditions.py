"""Weather codes from the sources → condition icon names (design/icons/conditions, icon set v1.1).

One place for the mapping so NWS and Open-Meteo forecasts look the same on screen. Contract shared by the
provider workstreams; change only with the orchestrator. Modifiers (wind, frost, alert) are added later by the
view model; `Condition.windy` only reports that the source itself flagged wind.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

CONDITIONS = (
    "sunny", "clear-night", "partly-cloudy", "partly-cloudy-night", "cloudy", "fog", "haze-smoke", "drizzle",
    "rain", "heavy-rain", "thunderstorm", "hail", "flurries", "snow", "heavy-snow", "blowing-snow", "sleet",
    "freezing-rain", "wintry-mix", "windy",
)

# Higher = more significant; used to pick one icon when a source lists several (NWS "rain,20/tsra,60").
SEVERITY = {name: i for i, name in enumerate((
    "sunny", "clear-night", "partly-cloudy", "partly-cloudy-night", "windy", "cloudy", "haze-smoke", "fog",
    "drizzle", "flurries", "rain", "snow", "blowing-snow", "sleet", "wintry-mix", "heavy-rain", "heavy-snow",
    "freezing-rain", "hail", "thunderstorm",
))}


@dataclass(frozen=True)
class Condition:
    name: str  # one of CONDITIONS
    windy: bool = False


def night_variant(name: str, is_day: bool) -> str:
    if is_day:
        return name
    return {"sunny": "clear-night", "partly-cloudy": "partly-cloudy-night"}.get(name, name)


def precip_type(name: str) -> str:
    """Dominant precipitation type implied by a condition: rain | snow | ice."""
    if name in ("flurries", "snow", "heavy-snow", "blowing-snow"):
        return "snow"
    if name in ("sleet", "freezing-rain", "wintry-mix"):
        return "ice"
    return "rain"


# ------------------------------------------------------------------ NWS

# https://api.weather.gov/icons — code → condition (day form; night handled by night_variant)
NWS_CODES = {
    "skc": "sunny", "few": "sunny", "sct": "partly-cloudy", "bkn": "cloudy", "ovc": "cloudy",
    "wind_skc": "sunny", "wind_few": "sunny", "wind_sct": "partly-cloudy", "wind_bkn": "cloudy", "wind_ovc": "cloudy",
    "snow": "snow", "rain_snow": "wintry-mix", "rain_sleet": "wintry-mix", "snow_sleet": "wintry-mix",
    "fzra": "freezing-rain", "rain_fzra": "freezing-rain", "snow_fzra": "wintry-mix", "sleet": "sleet",
    "rain": "rain", "rain_showers": "rain", "rain_showers_hi": "rain",
    "tsra": "thunderstorm", "tsra_sct": "thunderstorm", "tsra_hi": "thunderstorm",
    "tornado": "thunderstorm", "hurricane": "thunderstorm", "tropical_storm": "thunderstorm",
    "dust": "haze-smoke", "smoke": "haze-smoke", "haze": "haze-smoke",
    "hot": "sunny", "cold": "sunny", "blizzard": "blowing-snow", "fog": "fog",
}

_NWS_ICON = re.compile(r"/icons/\w+/(day|night)/(.+?)(?:\?|$)")

# Wording in NWS shortForecast that is more specific than the icon code (checked in order).
_TEXT_RULES = (
    (re.compile(r"freezing (rain|drizzle)", re.I), "freezing-rain"),
    (re.compile(r"blizzard|blowing snow", re.I), "blowing-snow"),
    (re.compile(r"hail", re.I), "hail"),
    (re.compile(r"thunder", re.I), "thunderstorm"),
    (re.compile(r"heavy snow", re.I), "heavy-snow"),
    (re.compile(r"heavy rain", re.I), "heavy-rain"),
    (re.compile(r"flurries", re.I), "flurries"),
    (re.compile(r"drizzle", re.I), "drizzle"),
)


def from_nws(icon_url: str | None, short_forecast: str | None = None, is_day: bool | None = None) -> Condition:
    """NWS icon URL (e.g. .../icons/land/night/rain,20/tsra,60?size=medium) + shortForecast → Condition."""
    name, windy, day = "cloudy", False, True
    m = _NWS_ICON.search(icon_url or "")
    if m:
        day = m.group(1) == "day"
        best = None
        for part in m.group(2).split("/"):
            code = part.split(",")[0]
            windy |= code.startswith("wind_")
            cand = NWS_CODES.get(code)
            if cand and (best is None or SEVERITY[cand] > SEVERITY[best]):
                best = cand
        name = best or name
    text = short_forecast or ""
    sky_only = name in ("sunny", "partly-cloudy", "cloudy")
    for rx, cand in _TEXT_RULES:  # wording refines the icon within the same precip family (or a sky-only icon)
        if rx.search(text) and (sky_only or precip_type(cand) == precip_type(name)):
            name = cand
            break
    else:
        # NWS draws "Partly Sunny" with the broken-cloud (bkn) icon; show it as partly cloudy, not cloudy.
        if sky_only and re.search(r"\bpartly (sunny|cloudy)\b", text, re.I):
            name = "partly-cloudy"
    windy |= bool(re.search(r"\bwindy|breezy\b", text, re.I))
    return Condition(night_variant(name, day if is_day is None else is_day), windy)


# ------------------------------------------------------------------ WMO (Open-Meteo weather_code)

WMO_CODES = {
    0: "sunny", 1: "sunny", 2: "partly-cloudy", 3: "cloudy", 45: "fog", 48: "fog",
    51: "drizzle", 53: "drizzle", 55: "drizzle", 56: "freezing-rain", 57: "freezing-rain",
    61: "rain", 63: "rain", 65: "heavy-rain", 66: "freezing-rain", 67: "freezing-rain",
    71: "flurries", 73: "snow", 75: "heavy-snow", 77: "flurries",
    80: "rain", 81: "rain", 82: "heavy-rain", 85: "flurries", 86: "heavy-snow",
    95: "thunderstorm", 96: "hail", 99: "hail",
}


def from_wmo(code: int | None, is_day: bool = True) -> Condition:
    return Condition(night_variant(WMO_CODES.get(int(code) if code is not None else -1, "cloudy"), is_day))
