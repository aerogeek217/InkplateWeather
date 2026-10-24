"""Every plain-language notice the screen can show, in one place so wording stays consistent.

Phase 1 covers the core sentences; phase 2.6 extends this with per-section wording.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from . import fields
from .readings import Status
from .resolver import Resolved

MISSING = "{label} unavailable."
NEVER_REPORTED = "{label} hasn't reported."
NOT_SINCE = "{label} hasn't reported since {time}."
SOURCE_DOWN = "{label} unavailable: {source} didn't respond. Retrying next update."
AS_OF = "as of {time}"
FALLBACK = "{label} from {source}."
SECTIONS_MISSING = "Some data is unavailable: {sections}."
BATTERY_LOW = "Battery low: charge soon"
SERVER_UNREACHABLE = "Can't reach the weather server at {host}. Will retry at {time}."
# Precip chart when every hour of the next 12 is under 10 % (approved 2026-10-24); _COLD when it gets to ≤ 34 °F.
NO_PRECIP = "No rain expected in the next 12 h"
NO_PRECIP_COLD = "No rain or snow expected in the next 12 h"

# Friendly names for provider types, used in "… didn't respond."
SOURCE_NAMES = {
    "ecowitt_local": "the weather station",
    "nws_observations": "NWS",
    "nws_forecast": "NWS",
    "nws_alerts": "NWS",
    "open_meteo": "Open-Meteo",
    "acis": "NOAA climate data",
    "home_assistant": "Home Assistant",
    "radar_mrms": "NOAA's radar feed",
    "radar_iem": "the radar feed",
    "fixture": "the test data",
}


def clock(t: datetime, tz: str) -> str:
    """'1:12 PM' in the display timezone (no leading zero)."""
    local = t.astimezone(ZoneInfo(tz))
    return local.strftime("%I:%M %p").lstrip("0")


def as_of(r: Resolved, tz: str) -> str | None:
    """Small 'as of 1:12 PM' tag for stale values."""
    if r.reading.status is Status.stale and r.reading.observed_at:
        return AS_OF.format(time=clock(r.reading.observed_at, tz))
    return None


def explain(r: Resolved, tz: str, source_types: dict[str, str] | None = None) -> str | None:
    """Sentence to show in place of a missing value; None when the value is usable."""
    if r.reading.status in (Status.ok, Status.stale):
        return None
    label = fields.FIELDS[r.field].label if r.field in fields.FIELDS else r.field
    last_seen = max((a.last_seen for a in r.tried if a.last_seen is not None), default=None)
    if last_seen is not None:
        return NOT_SINCE.format(label=label, time=clock(last_seen, tz))
    errored = [a for a in r.tried if a.outcome is Status.error]
    if errored and len(errored) == len(r.tried):
        kind = (source_types or {}).get(errored[0].source, "")
        return SOURCE_DOWN.format(label=label, source=SOURCE_NAMES.get(kind, errored[0].source))
    if r.tried and all(a.note in ("no_such_key", "no_value") for a in r.tried):
        return NEVER_REPORTED.format(label=label)
    return MISSING.format(label=label)


def sections_missing(sections: list[str]) -> str:
    return SECTIONS_MISSING.format(sections=", ".join(sections))
