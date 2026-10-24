"""Shared pieces of the NWS providers: HTTP client, errors, unit conversion, and the /points cache.

api.weather.gov wants a User-Agent that identifies the app (config `nws.user_agent`; the environment variable
`INKPLATE_NWS_USER_AGENT` overrides it). Requests retry once on a 5xx; a 429, a second 5xx, a timeout or an
unreachable host raises `NwsError`, whose `note` is a machine-friendly reason code for the resolver.

`/points/{lat},{lon}` (grid id/x/y, forecast URLs, forecast zone, county) almost never changes, so it is cached in
memory and as JSON under the cache directory (`INKPLATE_CACHE_DIR`, else `server.cache_dir`) and refreshed weekly;
when a refresh fails, the cached copy keeps being used.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from ..config import AppConfig

log = logging.getLogger(__name__)

BASE_URL = "https://api.weather.gov"
TIMEOUT = httpx.Timeout(6.0, connect=4.0)
RETRY_DELAY_S = 0.5
POINTS_MAX_AGE = timedelta(days=7)
POINTS_FILE = "nws_points.json"


class NwsError(RuntimeError):
    """A request to api.weather.gov failed; `note` is the reason code (e.g. nws_rate_limited)."""

    def __init__(self, note: str, detail: str = ""):
        super().__init__(f"{note}: {detail}" if detail else note)
        self.note = note


def user_agent(app: AppConfig) -> str:
    return os.environ.get("INKPLATE_NWS_USER_AGENT") or app.nws.user_agent


def client(app: AppConfig) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=BASE_URL,
        headers={"User-Agent": user_agent(app), "Accept": "application/geo+json"},
        timeout=TIMEOUT,
        follow_redirects=True,
    )


async def get_json(http: httpx.AsyncClient, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """GET a JSON document; retry once on a 5xx, raise NwsError on anything else that isn't a 200."""
    for attempt in (1, 2):
        try:
            r = await http.get(url, params=params)
        except httpx.TimeoutException as e:
            raise NwsError("nws_timeout", url) from e
        except httpx.HTTPError as e:
            raise NwsError("nws_unreachable", f"{url}: {e}") from e
        if r.status_code >= 500 and attempt == 1:
            log.info("NWS %s answered %s; retrying once", url, r.status_code)
            await asyncio.sleep(RETRY_DELAY_S)
            continue
        if r.status_code == 429:
            raise NwsError("nws_rate_limited", url)
        if r.status_code >= 500:
            raise NwsError("nws_server_error", f"{url}: HTTP {r.status_code}")
        if r.status_code != 200:
            raise NwsError(f"nws_http_{r.status_code}", url)
        try:
            data = r.json()
        except ValueError as e:
            raise NwsError("nws_bad_response", f"{url}: not JSON") from e
        if not isinstance(data, dict):
            raise NwsError("nws_bad_response", f"{url}: unexpected document")
        return data
    raise AssertionError("unreachable")


# ------------------------------------------------------------------ units

def to_f(q: dict[str, Any] | None) -> float | None:
    """A {unitCode, value} temperature → °F."""
    v, unit = _qv(q)
    if v is None:
        return None
    if unit.endswith("degF"):
        return v
    if unit.endswith("K"):
        v -= 273.15
    return v * 9 / 5 + 32


def c_to_f(v: float | None) -> float | None:
    return None if v is None else v * 9 / 5 + 32


def to_mph(q: dict[str, Any] | None) -> float | None:
    v, unit = _qv(q)
    if v is None:
        return None
    if unit.endswith("m_s-1"):
        return v * 2.2369363
    if unit.endswith("kt"):
        return v * 1.1507794
    if unit.endswith("mi_h-1"):
        return v
    return v / 1.609344  # km_h-1 (the default for observations and the grid)


def to_inhg(q: dict[str, Any] | None) -> float | None:
    v, unit = _qv(q)
    if v is None:
        return None
    if unit.endswith("hPa"):
        v *= 100
    return v / 3386.389


def mm_to_in(v: float | None) -> float | None:
    return None if v is None else v / 25.4


def plain(q: dict[str, Any] | None) -> float | None:
    """Value of a {unitCode, value} quantity whose unit needs no conversion (percent, degrees)."""
    return _qv(q)[0]


def _qv(q: dict[str, Any] | None) -> tuple[float | None, str]:
    if not isinstance(q, dict):
        return None, ""
    v = q.get("value")
    if v is None:
        return None, ""
    try:
        return float(v), str(q.get("unitCode") or "")
    except (TypeError, ValueError):
        return None, ""


def rnd(v: float | None, digits: int = 1) -> float | None:
    if v is None:
        return None
    r = round(v, digits)
    return 0.0 if r == 0 else r  # no -0.0


COMPASS = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")


def compass_deg(s: str | None) -> float | None:
    """'NW' → 315.0; None for anything else (e.g. 'VRB')."""
    s = (s or "").strip().upper()
    return COMPASS.index(s) * 22.5 if s in COMPASS else None


_NUM = re.compile(r"\d+(?:\.\d+)?")


def parse_wind_speed(s: str | None) -> float | None:
    """'10 to 15 mph' → 15 (the upper value of a range); 'Calm' → 0; None when there's no number."""
    if not s:
        return None
    nums = [float(x) for x in _NUM.findall(s)]
    if not nums:
        return 0.0 if "calm" in s.lower() else None
    v = max(nums)
    if "km/h" in s:
        v /= 1.609344
    elif "kt" in s.split():
        v *= 1.1507794
    return v


# ------------------------------------------------------------------ time

def parse_time(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


_DURATION = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


def parse_duration(s: str) -> timedelta:
    """ISO 8601 duration as used by the grid ('PT6H', 'P1D', 'P2DT6H')."""
    m = _DURATION.match(s.strip())
    if not m or not any(m.groups()):
        raise ValueError(f"bad ISO 8601 duration {s!r}")
    d, h, mi, se = (int(x or 0) for x in m.groups())
    return timedelta(days=d, hours=h, minutes=mi, seconds=se)


def parse_interval(s: str) -> tuple[datetime, datetime]:
    """Grid validTime '2026-10-24T18:00:00+00:00/PT6H' → (start, end), timezone-aware."""
    start_s, _, dur = s.partition("/")
    start = parse_time(start_s)
    if start is None or not dur:
        raise ValueError(f"bad validTime {s!r}")
    end = parse_time(dur) if dur[:1].isdigit() else start + parse_duration(dur)
    if end is None:
        raise ValueError(f"bad validTime {s!r}")
    return start, end


def floor_hour(t: datetime) -> datetime:
    return t.replace(minute=0, second=0, microsecond=0)


# ------------------------------------------------------------------ /points cache

@dataclass(frozen=True)
class PointInfo:
    grid_id: str
    grid_x: int
    grid_y: int
    forecast: str
    forecast_hourly: str
    forecast_grid_data: str
    forecast_zone: str  # zone id, e.g. ILZ045
    county: str  # county zone id, e.g. ILC019
    time_zone: str | None
    fetched_at: str  # ISO time of the /points request

    @classmethod
    def from_points(cls, doc: dict[str, Any], fetched_at: datetime) -> PointInfo:
        p = doc.get("properties") or {}
        try:
            return cls(
                grid_id=str(p["gridId"]), grid_x=int(p["gridX"]), grid_y=int(p["gridY"]),
                forecast=str(p["forecast"]), forecast_hourly=str(p["forecastHourly"]),
                forecast_grid_data=str(p["forecastGridData"]),
                forecast_zone=_zone_id(p.get("forecastZone")), county=_zone_id(p.get("county")),
                time_zone=p.get("timeZone"), fetched_at=fetched_at.isoformat(),
            )
        except (KeyError, TypeError, ValueError) as e:
            raise NwsError("nws_bad_response", f"/points is missing {e}") from e


def _zone_id(url: str | None) -> str:
    return (url or "").rstrip("/").rsplit("/", 1)[-1]


def point_key(lat: float, lon: float) -> str:
    """NWS accepts at most 4 decimals (more → a redirect), without trailing zeros."""

    def f(v: float) -> str:
        return f"{v:.4f}".rstrip("0").rstrip(".")

    return f"{f(lat)},{f(lon)}"


def cache_dir(app: AppConfig) -> Path:
    return Path(os.environ.get("INKPLATE_CACHE_DIR") or app.server.cache_dir)


_points_mem: dict[tuple[str, str], PointInfo] = {}  # (cache file, point key) → info


def _read_points_file(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_points_file(path: Path, data: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        tmp.replace(path)
    except OSError as e:  # the cache is an optimisation; never fail a fetch over it
        log.warning("could not write %s: %s", path, e)


def _cached_point(path: Path, key: str) -> PointInfo | None:
    hit = _points_mem.get((str(path), key))
    if hit is not None:
        return hit
    raw = _read_points_file(path).get(key)
    if not isinstance(raw, dict):
        return None
    try:
        info = PointInfo(**raw)
    except TypeError:
        return None
    _points_mem[(str(path), key)] = info
    return info


def cached_point(app: AppConfig) -> PointInfo | None:
    """The cached /points result for the configured location (any age), without a request."""
    return _cached_point(cache_dir(app) / POINTS_FILE, point_key(app.location.lat, app.location.lon))


def forget_point(app: AppConfig) -> None:
    """Drop the cached /points result (e.g. after the grid URLs answer 404)."""
    path = cache_dir(app) / POINTS_FILE
    key = point_key(app.location.lat, app.location.lon)
    _points_mem.pop((str(path), key), None)
    data = _read_points_file(path)
    if data.pop(key, None) is not None:
        _write_points_file(path, data)


async def get_point(http: httpx.AsyncClient, app: AppConfig, now: datetime) -> PointInfo:
    """The /points result for the configured location, from the cache when it's younger than a week."""
    path = cache_dir(app) / POINTS_FILE
    key = point_key(app.location.lat, app.location.lon)
    cached = _cached_point(path, key)
    if cached is not None:
        age = now - (parse_time(cached.fetched_at) or now)
        if age < POINTS_MAX_AGE:
            return cached
    try:
        info = PointInfo.from_points(await get_json(http, f"/points/{key}"), now)
    except NwsError as e:
        if cached is not None:
            log.warning("NWS /points refresh failed (%s); using the cached grid", e.note)
            return cached
        raise
    _points_mem[(str(path), key)] = info
    data = _read_points_file(path)
    data[key] = asdict(info)
    _write_points_file(path, data)
    return info
