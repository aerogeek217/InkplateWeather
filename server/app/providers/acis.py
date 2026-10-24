"""`acis` provider: climate normals and snow from RCC-ACIS (https://data.rcc-acis.org/StnData), no key.

One request per refresh covers the snow season so far (July 1 → today) for one co-op station:

- `normals.high` / `normals.low`: 1991–2020 daily normals of maxt / mint for today (°F)
- `snow.depth`: latest reported snow depth within the last `depth_days` days (in; trace = 0)
- `snow.season`: snowfall summed since July 1 (in; trace = 0, missing days skipped)

Co-op observations are made once a day (CHAMPAIGN 3S at 8 AM local); `observed_at` is that observation time on
the report's date, so `max_age` works as for any other source. Readings are keyed by canonical field names.

Config: `acis: {type: acis, ttl: 12h, station: "USC00118740"}`; optional `depth_days` (default 3) and `timeout`
(seconds, default 6). `station` is any id ACIS accepts (GHCN `USC00118740`, co-op `118740 2`, …).
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from ..config import AppConfig, ProviderCfg
from ..readings import Reading, missing
from .base import RequestContext, register

URL = "https://data.rcc-acis.org/StnData"
DEFAULT_OBS_HOUR = 7  # when the response gives no observation time
_NUM = re.compile(r"^\s*(-?\d+(?:\.\d+)?)")


class AcisError(RuntimeError):
    pass


def season_start(today: date) -> date:
    """Snow seasons run July 1 → June 30."""
    return date(today.year if today.month >= 7 else today.year - 1, 7, 1)


def parse_value(v: Any) -> float | None:
    """ACIS daily value → float. 'T' (trace) → 0.0; 'M' (missing), 'S' (in a later total) → None; '0.40A' → 0.4."""
    if v is None:
        return None
    s = str(v).strip()
    if s.upper() == "T":
        return 0.0
    m = _NUM.match(s)
    return float(m.group(1)) if m else None


def _split(cell: Any) -> tuple[Any, Any]:
    """A cell is a bare value, or [value, obs_hour] when requested with add=t."""
    if isinstance(cell, list):
        return (cell[0] if cell else None), (cell[1] if len(cell) > 1 else None)
    return cell, None


def _obs_time(day: date, hour: Any, tz: ZoneInfo) -> datetime:
    """ACIS gives the observation hour in local standard time (CHAMPAIGN 3S: 7 in summer = 8 AM CDT, 8 in winter)."""
    try:
        h = int(hour)
    except (TypeError, ValueError):
        h = DEFAULT_OBS_HOUR
    if not 0 <= h <= 24:
        h = DEFAULT_OBS_HOUR
    noon = datetime.combine(day, time(12), tz)
    standard = timezone(noon.utcoffset() - (noon.dst() or timedelta(0)))
    return (datetime.combine(day, time(0), standard) + timedelta(hours=h)).astimezone(tz)


def build_params(station: str, today: date) -> dict[str, Any]:
    return {
        "sid": station,
        "sdate": season_start(today).isoformat(),
        "edate": today.isoformat(),
        "elems": [
            {"name": "maxt", "interval": "dly", "duration": "dly", "normal": "91"},
            {"name": "mint", "interval": "dly", "duration": "dly", "normal": "91"},
            {"name": "snow", "interval": "dly", "duration": "dly", "add": "t"},
            {"name": "snwd", "interval": "dly", "duration": "dly", "add": "t"},
        ],
        "meta": "name,sids",
    }


def parse(data: dict[str, Any], source: str, today: date, tz: ZoneInfo, fetched_at: datetime,
          depth_days: int = 3) -> dict[str, Reading]:
    if not isinstance(data, dict) or "error" in data:
        raise AcisError(str(data.get("error") if isinstance(data, dict) else data))
    rows = data.get("data")
    if not isinstance(rows, list):
        raise AcisError("no data")

    parsed = []
    for row in rows:
        d = date.fromisoformat(row[0])
        snow, snow_t = _split(row[3]) if len(row) > 3 else (None, None)
        depth, depth_t = _split(row[4]) if len(row) > 4 else (None, None)
        parsed.append((d, parse_value(_split(row[1])[0]), parse_value(_split(row[2])[0]),
                       parse_value(snow), snow_t, parse_value(depth), depth_t))

    out: dict[str, Reading] = {}
    today_row = next((p for p in parsed if p[0] == today), None)
    for key, idx in (("normals.high", 1), ("normals.low", 2)):
        v = today_row[idx] if today_row else None
        out[key] = (Reading(value=round(v), unit="°F", observed_at=fetched_at, source=source) if v is not None
                    else missing(source, "no_value"))

    reported = [p for p in parsed if p[3] is not None]
    if reported:
        last = reported[-1]
        out["snow.season"] = Reading(value=round(sum(p[3] for p in reported), 1), unit="in",
                                     observed_at=_obs_time(last[0], last[4], tz), source=source)
    else:
        out["snow.season"] = missing(source, "no_value")

    recent = [p for p in parsed if p[5] is not None and (today - p[0]).days < depth_days]
    if recent:
        last = recent[-1]
        out["snow.depth"] = Reading(value=last[5], unit="in", observed_at=_obs_time(last[0], last[6], tz),
                                    source=source)
    else:
        out["snow.depth"] = missing(source, "no_recent_report")
    return out


@register("acis")
class AcisProvider:
    def __init__(self, name: str, cfg: ProviderCfg, app: AppConfig):
        self.name = name
        self.ttl = cfg.ttl
        opts = cfg.options
        if not opts.get("station"):
            raise ValueError(f"provider {name!r}: acis needs a `station` id")
        self.station = str(opts["station"])
        self.depth_days = int(opts.get("depth_days", 3))
        self.timeout = float(opts.get("timeout", 6))
        self.tz = ZoneInfo(app.location.tz)
        self.user_agent = app.nws.user_agent

    async def fetch(self, ctx: RequestContext) -> dict[str, Reading]:
        now = ctx.now.astimezone(self.tz)
        today = now.date()
        async with httpx.AsyncClient(timeout=self.timeout, headers={"User-Agent": self.user_agent}) as client:
            r = await client.post(URL, json=build_params(self.station, today))
            r.raise_for_status()
            data = r.json()
        return parse(data, self.name, today, self.tz, now, self.depth_days)
