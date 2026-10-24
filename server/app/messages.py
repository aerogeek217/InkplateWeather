"""Every plain-language notice the screen can show, in one place so wording stays consistent.

ARCHITECTURE *Error handling*: each field is ok, stale or missing. Stale values are drawn in gray with
"as of 1:12 PM"; a missing value is replaced by a short sentence; sections that can't draw say so; the header
sums up when two or more sections are missing. Provider note codes (Reading.note / Attempt.note) are turned into
friendly causes here ("NWS didn't respond"), so no code or exception name ever reaches the screen.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from zoneinfo import ZoneInfo

from . import fields
from .readings import Status
from .resolver import Attempt, Resolved

# ------------------------------------------------------------------ one slot (value replaced by a sentence)

MISSING = "{label} unavailable."
NEVER_REPORTED = "{label} hasn't reported."
NOT_SINCE = "{label} hasn't reported since {time}."
SOURCE_DOWN = "{label} unavailable: {cause}. Retrying next update."
SOURCE_PROBLEM = "{label} unavailable: {cause}."
AS_OF = "as of {time}"

# The same sentences for several slots at once (notes box under the radar legend).
NEVER_REPORTED_MANY = "{labels} haven't reported."
NOT_SINCE_MANY = "{labels} haven't reported since {time}."
MISSING_MANY = "{labels} unavailable."
SOURCE_DOWN_MANY = "{labels} unavailable: {cause}. Retrying next update."
SOURCE_PROBLEM_MANY = "{labels} unavailable: {cause}."
STALE_NOTE = "{labels} as of {time}."

# Short forms for narrow slots (readings cells ~100 px, gauge cells): "No data" over a short cause.
NO_DATA = "No data"
SHORT_SINCE = "since {time}"
SHORT_DOWN = "no response"
SHORT_NEVER = "not reported"

# ------------------------------------------------------------------ sections

RADAR_LATER = "Radar arrives in a later update."  # phase 2: no radar source configured yet
NO_HOURLY = "No hourly forecast"  # precip chart (one centred line, like the dry-period sentence)
NO_TREND = "No temperature data"  # trend chart with neither history nor forecast
NO_HISTORY = "No history yet"  # trend chart: past 24 h not recorded yet (forecast still drawn)

# Fallback used: "Forecast from Open-Meteo (NWS unavailable)."
FALLBACK = "{what} from {source} ({primary} unavailable)."
FALLBACK_PLAIN = "{what} from {source}."
STALE_SECTION = "{what} as of {time}."  # e.g. a forecast older than its max_age

# What a fallback note calls a group of fields.
FALLBACK_GROUPS = {
    "forecast.daily": "Forecast",
    "forecast.hourly": "Forecast",
    "outdoor.temperature": "Outdoor readings",
    "outdoor.humidity": "Outdoor readings",
    "outdoor.feels_like": "Outdoor readings",
    "outdoor.dew_point": "Outdoor readings",
    "wind.speed": "Outdoor readings",
    "wind.gust": "Outdoor readings",
    "wind.direction": "Outdoor readings",
    "pressure.sea_level": "Outdoor readings",
    "uv.index": "UV index",
    "air.aqi": "Air quality",
    "conditions.current": "Current conditions",
}

# ------------------------------------------------------------------ header

SECTIONS_MISSING = "Some data is unavailable: {sections}."
SECTIONS_MISSING_MORE = "Some data is unavailable: {sections} and {n} more."
SECTIONS_MISSING_SHORT = "Some data is unavailable."
# Section names used in that sentence, in the order they are listed.
SECTION_NAMES = {
    "alerts": "alerts",
    "radar": "radar",
    "forecast": "forecast",
    "hourly": "hourly forecast",
    "current": "current conditions",
    "readings": "readings",
    "gauge": "rain gauge",
    "sun_moon": "sun and moon",
}
BATTERY_LOW = "Battery low: charge soon"
BATTERY_STRIP = "Display battery {pct}%: charge it now or the screen will stop updating."  # one line in 506 px

# Header alert tag: word substitutions applied (in this order) to an NWS event name that doesn't fit.
# "Warning" and "Watch" stay spelled out (user decision 2026-10-24).
ALERT_ABBREVIATIONS = [
    ("Severe Thunderstorm", "Svr T-storm"),
    ("Thunderstorm", "T-storm"),
    ("Winter Weather", "Winter Wx"),
    ("Special Weather", "Special Wx"),
    ("Hazardous Weather", "Hazardous Wx"),
    ("Fire Weather", "Fire Wx"),
    ("Marine Weather", "Marine Wx"),
    ("Excessive", "Exc."),
    ("Extreme", "Ext."),
    ("Freezing", "Frz."),
    ("Hurricane Force", "Hurr. Force"),
    ("Advisory", "Adv."),
    ("Statement", "Stmt"),
    ("Information", "Info"),
]

# ------------------------------------------------------------------ whole screen

RENDER_FAILED = "Display couldn't update at {time}: {reason}. Showing the screen from {shown}."
RENDER_FAILED_NO_TIME = "Display couldn't update at {time}: {reason}. Showing the last good screen."
RENDER_REASON_TIMEOUT = "drawing the screen took too long"
RENDER_REASON_BROWSER = "the screen renderer stopped working"
RENDER_REASON_OTHER = "the server hit an error"
SERVER_UNREACHABLE = "Can't reach the weather server at {host}. Will retry at {time}."  # firmware's own screen
# Precip chart when every hour of the next 12 is under 10 % (approved 2026-10-24); _COLD when it gets to ≤ 34 °F.
NO_PRECIP = "No rain expected in the next 12 h"
NO_PRECIP_COLD = "No rain or snow expected in the next 12 h"

# ------------------------------------------------------------------ sources and causes

# Friendly names for provider types (and the example config's instance names), used in "… didn't respond."
SOURCE_NAMES = {
    "ecowitt_local": "the weather station",
    "nws_observations": "NWS",
    "nws_forecast": "NWS",
    "nws_alerts": "NWS",
    "open_meteo": "Open-Meteo",
    "acis": "NOAA climate data",
    "home_assistant": "Home Assistant",
    "history": "the station history",
    "device": "the display",
    "astro": "the sun and moon calculation",
    "radar_mrms": "NOAA's radar feed",
    "radar_iem": "the radar feed",
    "fixture": "the test data",
}
INSTANCE_TYPES = {  # config/config.example.yaml instance names → provider type
    "ecowitt": "ecowitt_local", "nws_obs": "nws_observations", "nws_fc": "nws_forecast", "nws_alerts": "nws_alerts",
    "om": "open_meteo", "acis": "acis", "astro": "astro", "history": "history", "device": "device",
    "ha": "home_assistant", "mrms": "radar_mrms", "iem": "radar_iem",
}

# note code → (who, what happened). "{source}" is the friendly source name; codes that name their provider (nws_*)
# don't use it. Causes that share what happened are merged: "NWS and Open-Meteo didn't respond".
CAUSES = {
    # NWS (any NWS endpoint)
    "nws_rate_limited": ("NWS", "is busy"),
    "nws_server_error": ("NWS", "didn't respond"),
    "nws_timeout": ("NWS", "didn't respond"),
    "nws_unreachable": ("NWS", "couldn't be reached"),
    "nws_bad_response": ("NWS", "sent data we couldn't read"),
    "nws_grid_unavailable": ("NWS", "didn't respond"),
    "nws_empty_forecast": ("NWS", "sent an empty forecast"),
    "no_observations": ("the airport station", "hasn't reported"),
    # Ecowitt gateway
    "no_value": ("{source}", "didn't report it"),
    "bad_value": ("{source}", "sent a bad value"),
    "unknown_unit": ("{source}", "sent a value we can't read"),
    # Home Assistant entity states
    "unavailable": ("Home Assistant", "is unavailable"),
    "unknown": ("Home Assistant", "has no reading yet"),
    # device provider (values from the Inkplate's request)
    "not_in_request": ("the display", "didn't send it"),
    # Open-Meteo and other HTTP providers
    "timeout": ("{source}", "didn't respond"),
    "unreachable": ("{source}", "couldn't be reached"),
    "bad_response": ("{source}", "sent data we couldn't read"),
    # resolver / history
    "not_fetched": ("{source}", "isn't available"),
    "no_such_key": ("{source}", "doesn't provide it"),
    "insufficient_history": ("{source}", "hasn't recorded enough yet"),
}
CAUSE_PREFIXES = [  # codes with a variable tail
    ("nws_http_", ("NWS", "didn't respond")),
    ("http_", ("{source}", "didn't respond")),
    ("provider_error:", ("{source}", "didn't respond")),
]
GENERIC_CAUSE = ("{source}", "had a problem")
_MERGEABLE = ("didn't", "couldn't", "sent", "had")  # past tense: reads right with a plural subject
# Short names for the fallback note ("NWS unavailable", "weather station unavailable").
SHORT_SOURCE_NAMES = {"the weather station": "weather station", "the radar feed": "radar feed",
                      "the station history": "station history", "the display": "display",
                      "the test data": "test data"}

# Outcomes that mean the source failed (vs. simply having no value): those messages say "Retrying next update."
_TRANSIENT_PREFIXES = ("nws_", "http_", "provider_error:")
_TRANSIENT = {"timeout", "unreachable", "bad_response"}


# ------------------------------------------------------------------ helpers


def clock(t: datetime, tz: str) -> str:
    """'1:12 PM' in the display timezone (no leading zero)."""
    local = t.astimezone(ZoneInfo(tz))
    return local.strftime("%I:%M %p").lstrip("0")


def join_and(words: list[str]) -> str:
    """'a', 'a and b', 'a, b and c'."""
    if not words:
        return ""
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def label_of(field: str) -> str:
    f = fields.FIELDS.get(field)
    return f.label if f else field


def labels_text(field_names: list[str]) -> str:
    """'Inside sensor, garage sensor and pressure' (first capitalised, the rest lower-case)."""
    labels = [label_of(f) for f in field_names]
    return cap(join_and([labels[0], *(lb[:1].lower() + lb[1:] for lb in labels[1:])]))


def source_type(source: str, source_types: Mapping[str, str] | None) -> str:
    """Provider type of a source instance; the example config's instance names are known without a config."""
    kind = (source_types or {}).get(source)
    if kind and kind != "fixture":
        return kind
    return INSTANCE_TYPES.get(source) or kind or source


def source_name(source: str, source_types: Mapping[str, str] | None = None) -> str:
    """'the weather station', 'NWS', 'Open-Meteo' … (the instance name itself when unknown)."""
    return SOURCE_NAMES.get(source_type(source, source_types), source)


def short_source_name(source: str, source_types: Mapping[str, str] | None = None) -> str:
    name = source_name(source, source_types)
    return SHORT_SOURCE_NAMES.get(name, name)


def _cause_parts(note: str | None, source: str, source_types: Mapping[str, str] | None) -> tuple[str, str]:
    n = note or ""
    who, what = CAUSES.get(n) or next((c for p, c in CAUSE_PREFIXES if n.startswith(p)), GENERIC_CAUSE)
    return who.format(source=source_name(source, source_types)), what


def cause(note: str | None, source: str, source_types: Mapping[str, str] | None = None) -> str:
    """Friendly cause for a provider note code: 'NWS didn't respond', 'the weather station didn't report it'."""
    return " ".join(_cause_parts(note, source, source_types))


def _transient(a: Attempt) -> bool:
    n = a.note or ""
    return a.outcome is Status.error or n in _TRANSIENT or n.startswith(_TRANSIENT_PREFIXES)


def _join_causes(parts: list[tuple[str, str]]) -> str:
    """'NWS and Open-Meteo didn't respond' when the causes share what happened, else the first cause."""
    unique = list(dict.fromkeys(parts))
    whats = {w for _, w in unique}
    if len(unique) > 1 and len(whats) == 1 and unique[0][1].startswith(_MERGEABLE):
        return f"{join_and(list(dict.fromkeys(w for w, _ in unique)))} {unique[0][1]}"
    return " ".join(unique[0])


# ------------------------------------------------------------------ per field


def as_of(r: Resolved, tz: str) -> str | None:
    """Small 'as of 1:12 PM' tag for stale values."""
    if r.reading.status is Status.stale and r.reading.observed_at:
        return AS_OF.format(time=clock(r.reading.observed_at, tz))
    return None


def _reason(r: Resolved, tz: str, source_types: Mapping[str, str] | None) -> tuple[str, str]:
    """(kind, detail) for a missing field: since/<time>, down/<cause>, problem/<cause>, never/'', missing/''."""
    last_seen = max((a.last_seen for a in r.tried if a.last_seen is not None), default=None)
    if last_seen is not None:
        return "since", clock(last_seen, tz)
    if r.tried and all(a.note in ("no_such_key", "no_value") for a in r.tried):
        return "never", ""
    failed = [a for a in r.tried if a.outcome is Status.error or (a.note and a.note not in ("no_such_key",))]
    if failed:
        text = _join_causes([_cause_parts(a.note, a.source, source_types) for a in failed])
        return ("down" if any(_transient(a) for a in failed) else "problem"), text
    return "missing", ""


def explain(r: Resolved, tz: str, source_types: Mapping[str, str] | None = None) -> str | None:
    """Sentence to show in place of a missing value; None when the value is usable."""
    if r.reading.status in (Status.ok, Status.stale):
        return None
    return _sentence(_reason(r, tz, source_types), label_of(r.field))


def _sentence(reason: tuple[str, str], label: str, many: bool = False) -> str:
    kind, detail = reason
    if kind == "since":
        return (NOT_SINCE_MANY if many else NOT_SINCE).format(label=label, labels=label, time=detail)
    if kind == "down":
        return (SOURCE_DOWN_MANY if many else SOURCE_DOWN).format(label=label, labels=label, cause=detail)
    if kind == "problem":
        return (SOURCE_PROBLEM_MANY if many else SOURCE_PROBLEM).format(label=label, labels=label, cause=detail)
    if kind == "never":
        return (NEVER_REPORTED_MANY if many else NEVER_REPORTED).format(label=label, labels=label)
    return (MISSING_MANY if many else MISSING).format(label=label, labels=label)


def short(r: Resolved | None, tz: str) -> str:
    """Second line under "No data" in a narrow slot: 'since 1:12 PM', 'no response', 'not reported' or ''."""
    if r is None:
        return ""
    kind, detail = _reason(r, tz, None)
    return {"since": SHORT_SINCE.format(time=detail), "down": SHORT_DOWN, "never": SHORT_NEVER}.get(kind, "")


def missing_notes(items: list[Resolved], tz: str, source_types: Mapping[str, str] | None = None) -> list[str]:
    """Full sentences for several missing slots, merged when they share a reason:
    'Inside sensor, garage sensor and pressure haven't reported since 11:40 AM.'"""
    groups: dict[tuple[str, str], list[str]] = {}
    for r in items:
        if r.reading.usable:
            continue
        groups.setdefault(_reason(r, tz, source_types), []).append(r.field)
    out = []
    for reason, names in groups.items():
        out.append(_sentence(reason, labels_text(names), many=len(names) > 1))
    return out


def stale_notes(items: list[Resolved], tz: str) -> list[str]:
    """'Inside sensor, garage sensor and pressure as of 1:50 PM.' (grouped by time)."""
    groups: dict[str, list[str]] = {}
    for r in items:
        t = as_of(r, tz)
        if t and r.reading.observed_at:
            groups.setdefault(clock(r.reading.observed_at, tz), []).append(r.field)
    return [STALE_NOTE.format(labels=labels_text(names), time=t) for t, names in groups.items()]


def fallback_note(what: str, winner: str, primary: str | None, source_types: Mapping[str, str] | None = None) -> str:
    src = source_name(winner, source_types)
    if primary is None:
        return FALLBACK_PLAIN.format(what=what, source=src)
    return FALLBACK.format(what=what, source=src, primary=short_source_name(primary, source_types))


def sections_missing(sections: list[str], fits=None) -> str:
    """'Some data is unavailable: radar, forecast.' Shortened ('… and 2 more', then no list) until `fits(text)`."""
    names = [SECTION_NAMES.get(s, s) for s in sections]
    cands = [SECTIONS_MISSING.format(sections=", ".join(names))]
    for k in range(len(names) - 1, 0, -1):
        cands.append(SECTIONS_MISSING_MORE.format(sections=", ".join(names[:k]), n=len(names) - k))
    cands.append(SECTIONS_MISSING_SHORT)
    if fits is None:
        return cands[0]
    return next((c for c in cands if fits(c)), SECTIONS_MISSING_SHORT)


def abbreviate_event(event: str) -> str:
    """'Severe Thunderstorm Warning' → 'Svr T-storm Warning' (ALERT_ABBREVIATIONS, whole words only)."""
    out = event
    for long, short_ in ALERT_ABBREVIATIONS:
        out = re.sub(rf"\b{re.escape(long)}\b", short_, out)
    return out


def render_failed(at: str, reason: str, shown: str | None) -> str:
    if shown:
        return RENDER_FAILED.format(time=at, reason=reason, shown=shown)
    return RENDER_FAILED_NO_TIME.format(time=at, reason=reason)
