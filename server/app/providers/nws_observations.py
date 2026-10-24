"""`nws_observations`: the NWS station's recent observations (KCMI by default, config `nws.station`).

One request per refresh: `/stations/{station}/observations?start=…` returns every report since `start`, newest first.
KCMI files a 5-minute report (whole °C, often no sea-level pressure) plus the hourly METAR at :53 (0.1 °C, sea-level
pressure), so each value is taken from the newest report that has it, with that report's time as `observed_at`; the
resolver's max_age then decides whether it's fresh. The same list gives the 24 h temperature history and the 3 h
pressure trend. Reports are kept in memory between refreshes, so after the first fetch (~26 h) only the last few
hours are requested again.

Readings (keys = field names): outdoor.temperature, outdoor.humidity, outdoor.dew_point, outdoor.feels_like,
outdoor.temperature_history, wind.speed, wind.gust, wind.direction, pressure.sea_level, pressure.trend_3h,
conditions.current. A value the station didn't report becomes a missing Reading with a note.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from .. import conditions, shapes
from ..config import AppConfig, ProviderCfg
from ..readings import Reading, Status, missing
from .base import RequestContext, register
from .nws_common import client, get_json, parse_time, plain, rnd, to_f, to_inhg, to_mph

HISTORY = timedelta(hours=24)
KEEP = timedelta(hours=26)  # what the first request asks for (a little margin for the 3 h trend)
REFETCH_OVERLAP = timedelta(hours=2)  # later requests re-read this much (late reports and QC corrections)
HISTORY_STEP = timedelta(minutes=19)  # at most one history point per ~20 min
TREND_SPAN = timedelta(hours=3)
TREND_SLACK = timedelta(minutes=40)  # how far the "3 h ago" report may be from exactly 3 h
PRESSURE_PAIR_SLACK = timedelta(minutes=75)  # prefer sea-level pressure unless it's this much older than station

KEYS = ("outdoor.temperature", "outdoor.humidity", "outdoor.dew_point", "outdoor.feels_like",
        "outdoor.temperature_history", "wind.speed", "wind.gust", "wind.direction", "pressure.sea_level",
        "pressure.trend_3h", "conditions.current")

UNITS = {"outdoor.temperature": "°F", "outdoor.humidity": "%", "outdoor.dew_point": "°F", "outdoor.feels_like": "°F",
         "outdoor.temperature_history": "°F", "wind.speed": "mph", "wind.gust": "mph", "wind.direction": "deg",
         "pressure.sea_level": "inHg", "pressure.trend_3h": "inHg", "conditions.current": None}

Obs = tuple[datetime, dict[str, Any]]  # (timestamp, properties)


@register("nws_observations")
class NwsObservationsProvider:
    """Config: `nws_obs: {type: nws_observations, ttl: 10m}`; optional `station:` overrides `nws.station`."""

    def __init__(self, name: str, cfg: ProviderCfg, app: AppConfig):
        self.name = name
        self.ttl = cfg.ttl
        self.app = app
        self.station = str(cfg.options.get("station") or app.nws.station).upper()
        self._obs: dict[datetime, dict[str, Any]] = {}  # reports seen so far, by timestamp

    async def fetch(self, ctx: RequestContext) -> dict[str, Reading]:
        now = ctx.now
        start = now - KEEP
        if self._obs:
            start = max(start, max(self._obs) - REFETCH_OVERLAP)
        async with client(self.app) as http:
            doc = await get_json(http, f"/stations/{self.station}/observations",
                                 params={"start": start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")})
        for f in doc.get("features") or []:
            p = (f or {}).get("properties") or {}
            t = parse_time(p.get("timestamp"))
            if t is not None and t <= now + timedelta(minutes=5):
                self._obs[t] = p
        for t in [t for t in self._obs if t < now - KEEP]:
            del self._obs[t]
        obs = sorted(self._obs.items(), key=lambda o: o[0], reverse=True)  # newest first
        return parse(obs, now, self.name)


def parse(obs: list[Obs], now: datetime, source: str) -> dict[str, Reading]:
    """Readings from observations sorted newest first."""
    out: dict[str, Reading] = {k: missing(source, "no_observations") for k in KEYS}
    if not obs:
        return out

    def ok(key: str, value: Any, t: datetime) -> None:
        out[key] = Reading(value=value, unit=UNITS[key], observed_at=t, source=source)

    def newest(get) -> tuple[datetime, Any, dict[str, Any]] | None:
        for t, p in obs:
            v = get(p)
            if v is not None:
                return t, v, p
        return None

    if hit := newest(lambda p: to_f(p.get("temperature"))):
        t, temp, p = hit
        ok("outdoor.temperature", rnd(temp), t)
        feels = to_f(p.get("heatIndex"))
        if feels is None:
            feels = to_f(p.get("windChill"))
        ok("outdoor.feels_like", rnd(temp if feels is None else feels), t)
    if hit := newest(lambda p: plain(p.get("relativeHumidity"))):
        ok("outdoor.humidity", rnd(hit[1], 0), hit[0])
    if hit := newest(lambda p: to_f(p.get("dewpoint"))):
        ok("outdoor.dew_point", rnd(hit[1]), hit[0])

    if hit := newest(lambda p: to_mph(p.get("windSpeed"))):
        t, speed, p = hit
        ok("wind.speed", rnd(speed), t)
        gust = to_mph(p.get("windGust"))
        # METARs only report a gust when it's gusting; otherwise the peak is the sustained speed.
        ok("wind.gust", rnd(max(speed, gust) if gust is not None else speed), t)
        direction = plain(p.get("windDirection"))
        if direction is not None:
            ok("wind.direction", rnd(direction % 360, 0), t)
        else:
            out["wind.direction"] = Reading(source=source, status=Status.missing, observed_at=t,
                                            note="wind_calm" if speed < 0.5 else "wind_variable")

    sea = newest(lambda p: to_inhg(p.get("seaLevelPressure")))
    stn = newest(lambda p: to_inhg(p.get("barometricPressure")))
    if sea and (stn is None or sea[0] >= stn[0] - PRESSURE_PAIR_SLACK):
        ok("pressure.sea_level", rnd(sea[1], 2), sea[0])
    elif stn:
        ok("pressure.sea_level", rnd(stn[1], 2), stn[0])
    trend = _trend(obs, "seaLevelPressure") or _trend(obs, "barometricPressure")
    if trend:
        ok("pressure.trend_3h", rnd(trend[1], 2), trend[0])

    if hit := newest(lambda p: (p.get("icon") or p.get("textDescription")) or None):
        t, _, p = hit
        ok("conditions.current", conditions.from_nws(p.get("icon"), p.get("textDescription")).name, t)

    history = _history(obs, now)
    if history:
        ok("outdoor.temperature_history", shapes.dump(history), history[-1].t)
    return out


def _trend(obs: list[Obs], prop: str) -> tuple[datetime, float] | None:
    """(time of the newest value, change over ~3 h in inHg) using one pressure property for both ends."""
    series = [(t, v) for t, p in obs if (v := to_inhg(p.get(prop))) is not None]
    if not series:
        return None
    t1, v1 = series[0]
    target = t1 - TREND_SPAN
    t0, v0 = min(series, key=lambda s: abs(s[0] - target))
    if abs(t0 - target) > TREND_SLACK:
        return None
    return t1, v1 - v0


def _history(obs: list[Obs], now: datetime) -> list[shapes.HistoryPoint]:
    """Last 24 h of temperatures, oldest first, at most one point per ~20 min (always ending at the newest)."""
    points: list[shapes.HistoryPoint] = []
    last: datetime | None = None
    for t, p in obs:  # newest first
        if t < now - HISTORY:
            break
        v = to_f(p.get("temperature"))
        if v is None or (last is not None and last - t < HISTORY_STEP):
            continue
        points.append(shapes.HistoryPoint(t=t, v=rnd(v)))
        last = t
    return points[::-1]
