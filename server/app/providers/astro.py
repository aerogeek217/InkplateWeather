"""`astro` provider: sun and moon for the display location, computed locally (no network, never raises).

- `astro.sun` (shapes.SunInfo): today's sunrise/sunset (astral, standard refraction, sea-level horizon), day length
  and its change since yesterday.
- `astro.moon` (shapes.MoonInfo): illuminated fraction and waxing/waning now (Meeus ch. 48), phase name, today's
  moonrise and the moonset that follows it, and the next four principal phases (Meeus ch. 49), as local dates.
  Moon rise/set come from astral's lunar position with our own horizon search (Meeus ch. 15 standard altitude):
  astral 3.2's own moonrise()/moonset() miss about two events a month around UTC midnight.

Verified against timeanddate.com for Savoy, IL (see tests/test_astro.py): times within 1 min, phases within minutes.
Readings are keyed by canonical field names.
"""
from __future__ import annotations

import logging
import math
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from astral import Observer
from astral.moon import moon_position
from astral.sun import sun

from .. import shapes
from ..config import AppConfig, ProviderCfg
from ..readings import Reading, missing
from .base import RequestContext, register

log = logging.getLogger(__name__)

PHASE_KINDS = ("new", "first", "full", "last")
PHASE_NAMES = {"new": "new moon", "first": "first quarter", "full": "full moon", "last": "last quarter"}
_J2000 = 2451545.0
_UNIX_EPOCH_JD = 2440587.5


# ------------------------------------------------------------------ time helpers

def julian_day(t: datetime) -> float:
    return t.timestamp() / 86400.0 + _UNIX_EPOCH_JD


def from_julian_day(jd: float) -> datetime:
    return datetime.fromtimestamp((jd - _UNIX_EPOCH_JD) * 86400.0, UTC)


def delta_t_seconds(year: float) -> float:
    """TT − UT. Observed ≈ 69 s in 2020–2026; a gentle extrapolation is plenty for phase dates."""
    return 69.0 + 0.3 * max(0.0, year - 2025.0)


def _sin(deg: float) -> float:
    return math.sin(math.radians(deg))


def _cos(deg: float) -> float:
    return math.cos(math.radians(deg))


# ------------------------------------------------------------------ moon: illumination (Meeus ch. 48, low precision)

def moon_phase_angle(t: datetime) -> tuple[float, float]:
    """(illuminated fraction 0–1, elongation of the moon east of the sun in degrees 0–360) at time t."""
    tc = (julian_day(t) + delta_t_seconds(t.year) / 86400.0 - _J2000) / 36525.0
    d = 297.8501921 + 445267.1114034 * tc - 0.0018819 * tc**2 + tc**3 / 545868 - tc**4 / 113065000
    m = 357.5291092 + 35999.0502909 * tc - 0.0001536 * tc**2 + tc**3 / 24490000
    mp = 134.9633964 + 477198.8675055 * tc + 0.0087414 * tc**2 + tc**3 / 69699 - tc**4 / 14712000
    i = (180 - d - 6.289 * _sin(mp) + 2.100 * _sin(m) - 1.274 * _sin(2 * d - mp) - 0.658 * _sin(2 * d)
         - 0.214 * _sin(2 * mp) - 0.110 * _sin(d))
    return (1 + _cos(i)) / 2, (180 - i) % 360


# ------------------------------------------------------------------ moon: principal phases (Meeus ch. 49)

_NEW = ((-0.40720, "Mp", 0), (0.17241, "M", 1), (0.01608, "2Mp", 0), (0.01039, "2F", 0), (0.00739, "Mp-M", 1),
        (-0.00514, "Mp+M", 1), (0.00208, "2M", 2), (-0.00111, "Mp-2F", 0), (-0.00057, "Mp+2F", 0),
        (0.00056, "2Mp+M", 1), (-0.00042, "3Mp", 0), (0.00042, "M+2F", 1), (0.00038, "M-2F", 1),
        (-0.00024, "2Mp-M", 1), (-0.00017, "O", 0), (-0.00007, "Mp+2M", 0), (0.00004, "2Mp-2F", 0),
        (0.00004, "3M", 0), (0.00003, "Mp+M-2F", 0), (0.00003, "2Mp+2F", 0), (-0.00003, "Mp+M+2F", 0),
        (0.00003, "Mp-M+2F", 0), (-0.00002, "Mp-M-2F", 0), (-0.00002, "3Mp+M", 0), (0.00002, "4Mp", 0))
_FULL = ((-0.40614, "Mp", 0), (0.17302, "M", 1), (0.01614, "2Mp", 0), (0.01043, "2F", 0), (0.00734, "Mp-M", 1),
         (-0.00515, "Mp+M", 1), (0.00209, "2M", 2)) + _NEW[7:]
_QUARTER = ((-0.62801, "Mp", 0), (0.17172, "M", 1), (-0.01183, "Mp+M", 1), (0.00862, "2Mp", 0), (0.00804, "2F", 0),
            (0.00454, "Mp-M", 1), (0.00204, "2M", 2), (-0.00180, "Mp-2F", 0), (-0.00070, "Mp+2F", 0),
            (-0.00040, "3Mp", 0), (-0.00034, "2Mp-M", 1), (0.00032, "M+2F", 1), (0.00032, "M-2F", 1),
            (-0.00028, "Mp+2M", 2), (0.00027, "2Mp+M", 1), (-0.00017, "O", 0), (-0.00005, "Mp-M-2F", 0),
            (0.00004, "2Mp+2F", 0), (-0.00004, "Mp+M+2F", 0), (0.00004, "Mp-2M", 0), (0.00003, "Mp+M-2F", 0),
            (0.00003, "3M", 0), (0.00002, "2Mp-2F", 0), (0.00002, "Mp-M+2F", 0), (-0.00002, "3Mp+M", 0))


def _arg(expr: str, a: dict[str, float]) -> float:
    """Evaluate e.g. 'Mp+M-2F' with the argument values in `a` (degrees)."""
    total, sign, i = 0.0, 1.0, 0
    while i < len(expr):
        ch = expr[i]
        if ch in "+-":
            sign = 1.0 if ch == "+" else -1.0
            i += 1
            continue
        n = 1.0
        if ch.isdigit():
            n, i = float(ch), i + 1
        name = "Mp" if expr.startswith("Mp", i) else expr[i]
        total += sign * n * a[name]
        i += len(name)
    return total


def phase_time(k: float) -> datetime:
    """UTC time of the principal phase for lunation number k (…, 0, 0.25, 0.5, 0.75, … from the 2000-01-06 new moon)."""
    t = k / 1236.85
    jde = 2451550.09766 + 29.530588861 * k + 0.00015437 * t**2 - 0.000000150 * t**3 + 0.00000000073 * t**4
    e = 1 - 0.002516 * t - 0.0000074 * t**2
    a = {
        "M": 2.5534 + 29.10535670 * k - 0.0000014 * t**2 - 0.00000011 * t**3,
        "Mp": 201.5643 + 385.81693528 * k + 0.0107582 * t**2 + 0.00001238 * t**3 - 0.000000058 * t**4,
        "F": 160.7108 + 390.67050284 * k - 0.0016118 * t**2 - 0.00000227 * t**3 + 0.000000011 * t**4,
        "O": 124.7746 - 1.56375588 * k + 0.0020672 * t**2 + 0.00000215 * t**3,
    }
    frac = round((k % 1) * 4) % 4
    terms = (_NEW, _QUARTER, _FULL, _QUARTER)[frac]
    corr = sum(c * e**p * _sin(_arg(expr, a)) for c, expr, p in terms)
    if frac in (1, 3):
        w = (0.00306 - 0.00038 * e * _cos(a["M"]) + 0.00026 * _cos(a["Mp"]) - 0.00002 * _cos(a["Mp"] - a["M"])
             + 0.00002 * _cos(a["Mp"] + a["M"]) + 0.00002 * _cos(2 * a["F"]))
        corr += w if frac == 1 else -w
    jd_ut = jde + corr - delta_t_seconds(2000 + k / 12.3685) / 86400.0
    return from_julian_day(jd_ut)


def principal_phases(after: datetime, count: int = 4) -> list[tuple[str, datetime]]:
    """The next `count` principal phases strictly after `after`: [(kind, utc time)], in order."""
    years = (after.astimezone(UTC) - datetime(2000, 1, 6, 18, 14, tzinfo=UTC)).days / 365.25
    k = math.floor(years * 12.3685) - 1.0
    out: list[tuple[str, datetime]] = []
    while len(out) < count:
        when = phase_time(k)
        if when > after:
            out.append((PHASE_KINDS[round((k % 1) * 4) % 4], when))
        k += 0.25
    return out


def phases_between(start: datetime, end: datetime) -> list[tuple[str, datetime]]:
    return [p for p in principal_phases(start - timedelta(seconds=1), 2) if p[1] < end]


# ------------------------------------------------------------------ sun

def sun_info(lat: float, lon: float, day: date, tz: ZoneInfo) -> shapes.SunInfo:
    obs = Observer(lat, lon, 0)
    today = sun(obs, day, tzinfo=tz)
    yesterday = sun(obs, day - timedelta(days=1), tzinfo=tz)
    length = (today["sunset"] - today["sunrise"]).total_seconds() / 60
    prev = (yesterday["sunset"] - yesterday["sunrise"]).total_seconds() / 60
    return shapes.SunInfo(sunrise=_minute(today["sunrise"]), sunset=_minute(today["sunset"]),
                          day_length_min=round(length), delta_min_per_day=round(length - prev))


def _minute(t: datetime) -> datetime:
    return (t + timedelta(seconds=30)).replace(second=0, microsecond=0)


# ------------------------------------------------------------------ moon: rise and set

_STEP = timedelta(minutes=10)


def _moon_above(lat: float, lon: float, t: datetime) -> float:
    """sin(altitude) − sin(h0) for the moon's centre; > 0 when the moon is up (Meeus ch. 15, h0 = 0.7275π − 34′)."""
    d = julian_day(t) - _J2000
    pos = moon_position(d)
    lst = math.radians((280.46061837 + 360.98564736629 * d + lon) % 360)
    phi = math.radians(lat)
    sin_alt = (math.sin(phi) * math.sin(pos.declination)
               + math.cos(phi) * math.cos(pos.declination) * math.cos(lst - pos.right_ascension))
    parallax = math.degrees(math.asin(1 / pos.distance))  # distance in Earth radii
    return sin_alt - _sin(0.7275 * parallax - 0.5667)


def moon_events(lat: float, lon: float, day: date, tz: ZoneInfo) -> list[tuple[str, datetime]]:
    """[("rise" | "set", local time)] during the local calendar day, in order (to the nearest minute)."""
    # Scan a little past both midnights: an event at 23:59:50 rounds into the next day.
    start = datetime.combine(day, datetime.min.time(), tz).astimezone(UTC) - _STEP
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tz).astimezone(UTC) + _STEP
    out: list[tuple[str, datetime]] = []
    t0, f0 = start, _moon_above(lat, lon, start)
    while t0 < end:
        t1 = min(t0 + _STEP, end)
        f1 = _moon_above(lat, lon, t1)
        if (f0 < 0) != (f1 < 0):
            a, b, fa = t0, t1, f0
            while b - a > timedelta(seconds=5):
                mid = a + (b - a) / 2
                fm = _moon_above(lat, lon, mid)
                if (fa < 0) == (fm < 0):
                    a, fa = mid, fm
                else:
                    b = mid
            when = _minute(a + (b - a) / 2).astimezone(tz)
            if when.date() == day:
                out.append(("rise" if f0 < 0 else "set", when))
        t0, f0 = t1, f1
    return out


def moon_rise_set(lat: float, lon: float, day: date, tz: ZoneInfo) -> tuple[datetime | None, datetime | None]:
    """Today's moonrise and the moonset that follows it (usually tomorrow's); without a rise today, today's set."""
    today = moon_events(lat, lon, day, tz)
    rise = next((t for k, t in today if k == "rise"), None)
    if rise is None:
        return None, next((t for k, t in today if k == "set"), None)
    later = [t for k, t in today + moon_events(lat, lon, day + timedelta(days=1), tz) if k == "set" and t > rise]
    return rise, (later[0] if later else None)


# ------------------------------------------------------------------ moon info

def phase_name(now: datetime, tz: ZoneInfo, illumination: float, waxing: bool) -> str:
    """Principal phase name on the local day it happens; otherwise crescent/gibbous by illumination."""
    local = now.astimezone(tz)
    start = datetime.combine(local.date(), datetime.min.time(), tz)
    for kind, when in phases_between(start, start + timedelta(days=1)):
        if when.astimezone(tz).date() == local.date():
            return PHASE_NAMES[kind]
    shape = "gibbous" if illumination >= 0.5 else "crescent"
    return f"{'waxing' if waxing else 'waning'} {shape}"


def moon_info(lat: float, lon: float, now: datetime, tz: ZoneInfo) -> shapes.MoonInfo:
    illum, elong = moon_phase_angle(now)
    waxing = elong < 180
    rise, set_ = moon_rise_set(lat, lon, now.astimezone(tz).date(), tz)
    nxt = [shapes.MoonPhaseDate(kind=k, date=t.astimezone(tz).date()) for k, t in principal_phases(now, 4)]
    return shapes.MoonInfo(
        phase_name=phase_name(now, tz, illum, waxing), illumination=round(illum * 100), waxing=waxing,
        rise=rise, set=set_, next=nxt,
    )


@register("astro")
class AstroProvider:
    def __init__(self, name: str, cfg: ProviderCfg | None, app: AppConfig):
        self.name = name
        self.ttl = cfg.ttl if cfg else timedelta(0)
        self.lat, self.lon = app.location.lat, app.location.lon
        self.tz = ZoneInfo(app.location.tz)

    async def fetch(self, ctx: RequestContext) -> dict[str, Reading]:
        now = ctx.now.astimezone(self.tz)
        out: dict[str, Reading] = {}
        for key, compute in (("astro.sun", lambda: sun_info(self.lat, self.lon, now.date(), self.tz)),
                             ("astro.moon", lambda: moon_info(self.lat, self.lon, now, self.tz))):
            try:
                value: Any = shapes.dump(compute())
                out[key] = Reading(value=value, observed_at=ctx.now, source=self.name)
            except Exception:  # e.g. no sunrise at polar latitudes; never take the board down
                log.exception("astro: %s failed", key)
                out[key] = missing(self.name, "not_computable")
        return out
