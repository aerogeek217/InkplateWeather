"""`nws_forecast`: NWS hourly and 7-day forecast plus gridpoint amounts (snow, ice, liquid) and precip type.

Requests per refresh: `/points` (cached for a week, see nws_common) and then, concurrently, `forecastHourly`,
`forecastGridData` and `forecast`. If only some of those fail, the others still produce their readings (without the
grid, amounts are zero and the type comes from the hourly icon); if all fail, the provider raises.

Readings (keys = field names, values = `shapes.dump(...)`):
- `forecast.hourly`: HourlyPoint list from the current hour, up to 72 h. Temperature, chance, wind and icon come from
  `forecastHourly`; precip type and amounts from the grid (see `HourTable`).
- `forecast.daily`: DailyForecast list, today first, 7 calendar days in `location.tz`, merged from the 12-hour
  periods: high/icon/text/wind from the day period, low from the night that follows it (evening: only "Tonight"
  remains, so today's high is None and icon/text come from the night), chance = the higher of the two, amounts
  summed from the grid for the rest of that local day.
"""
from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .. import conditions, shapes
from ..config import AppConfig, ProviderCfg
from ..readings import Reading, Status, error
from .base import RequestContext, register
from .nws_common import (
    NwsError,
    c_to_f,
    client,
    compass_deg,
    floor_hour,
    forget_point,
    get_json,
    get_point,
    mm_to_in,
    parse_interval,
    parse_time,
    parse_wind_speed,
    rnd,
    to_mph,
)

HOURS = 72  # hourly points returned (the contract asks for at least 48)
DAYS = 7
FREEZING_F = 32.0  # dry hours at or below this are typed "snow" (what would fall), above it "rain"

# ------------------------------------------------------------------ grid: weather type per hour

# How sure the forecaster is (grid `coverage`); entries with the highest rank decide the hour's type.
COVERAGE_RANK = {
    "slight_chance": 1, "isolated": 1, "few": 1,
    "chance": 2, "scattered": 2, "patchy": 2, "brief": 2, "intermittent": 2,
    "likely": 3, "numerous": 3, "areas": 3, "periods": 3, "occasional": 3, "frequent": 3,
    "definite": 4, "widespread": 4,
}

WEATHER_TYPE = {  # grid `weather` → precip type; anything else (fog, haze, smoke, frost…) is not precipitation
    "rain": "rain", "rain_showers": "rain", "drizzle": "rain", "thunderstorms": "rain", "hail": "rain",
    "snow": "snow", "snow_showers": "snow", "blowing_snow": "snow",
    "sleet": "ice", "freezing_rain": "ice", "freezing_drizzle": "ice", "freezing_spray": "ice",
}


def weather_type(entries: Any, temp_f: float | None) -> str | None:
    """Dominant precip type of one grid `weather` value (a list of {coverage, weather, …}), None when dry.

    The entries with the highest coverage decide: any ice → ice; snow and rain together → snow at or below
    freezing, else rain; otherwise the single type.
    """
    ranked: list[tuple[int, str]] = []
    for e in entries if isinstance(entries, list) else []:
        kind = WEATHER_TYPE.get((e or {}).get("weather") or "")
        if kind:
            ranked.append((COVERAGE_RANK.get(e.get("coverage") or "", 2), kind))
    if not ranked:
        return None
    top = max(r for r, _ in ranked)
    kinds = {k for r, k in ranked if r == top}
    if "ice" in kinds:
        return "ice"
    if kinds == {"snow", "rain"}:
        return "snow" if temp_f is not None and temp_f <= FREEZING_F else "rain"
    return kinds.pop()


Series = list[tuple[datetime, datetime, Any]]


def grid_series(props: dict[str, Any], name: str) -> Series:
    """One gridpoint layer as (start, end, value), skipping malformed entries."""
    out: Series = []
    for item in ((props.get(name) or {}).get("values") or []):
        try:
            start, end = parse_interval(item["validTime"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append((start, end, item.get("value")))
    return out


def grid_uom(props: dict[str, Any], name: str) -> str:
    return str((props.get(name) or {}).get("uom") or "")


def value_at(series: Series, t: datetime) -> Any:
    for start, end, v in series:
        if start <= t < end:
            return v
    return None


def hours_in(start: datetime, end: datetime) -> list[datetime]:
    out, h = [], floor_hour(start.astimezone(UTC))
    while h < end:
        out.append(h)
        h += timedelta(hours=1)
    return out


def spread(series: Series, eligible: Callable[[datetime], bool], weight: Callable[[datetime], float],
           convert: Callable[[float], float]) -> dict[datetime, float]:
    """Spread interval totals (e.g. 6-hour snowfall) over the hours they cover.

    Only hours of the matching type get a share (snowfall goes to the snow hours of the interval, ice to the ice
    hours) unless none of them match, then all hours do. Shares follow the hourly chance of precipitation (equal
    shares when the chance is zero throughout).
    """
    out: dict[datetime, float] = defaultdict(float)
    for start, end, v in series:
        if not isinstance(v, (int, float)) or v <= 0:
            continue
        hours = hours_in(start, end)
        cand = [h for h in hours if eligible(h)] or hours
        ws = [max(weight(h), 0.0) for h in cand]
        total = sum(ws)
        if total <= 0:
            ws, total = [1.0] * len(cand), float(len(cand))
        amount = convert(float(v))
        for h, w in zip(cand, ws, strict=True):
            out[h] += amount * w / total
    return out


def _amount_converter(uom: str) -> Callable[[float], float]:
    if uom.endswith(":m"):
        return lambda v: v / 0.0254
    if uom.endswith("in"):
        return lambda v: v
    return lambda v: mm_to_in(v) or 0.0  # wmoUnit:mm, the grid's unit for all three amounts


@dataclass
class HourTable:
    """Per-hour (UTC hour start) precip type and amounts in inches, from the grid plus the hourly forecast."""

    types: dict[datetime, str] = field(default_factory=dict)
    rain: dict[datetime, float] = field(default_factory=dict)
    snow: dict[datetime, float] = field(default_factory=dict)
    ice: dict[datetime, float] = field(default_factory=dict)
    gust: dict[datetime, float] = field(default_factory=dict)

    def type_at(self, h: datetime) -> str:
        return self.types.get(h, "rain")


def build_hour_table(grid: dict[str, Any] | None, hourly: dict[datetime, dict[str, Any]]) -> HourTable:
    """Types first (grid weather → hourly icon → temperature), then amounts spread onto hours of their type."""
    table = HourTable()
    props = grid or {}
    weather = grid_series(props, "weather")
    temps = grid_series(props, "temperature")
    pops = grid_series(props, "probabilityOfPrecipitation")
    amounts = {k: grid_series(props, k) for k in ("quantitativePrecipitation", "snowfallAmount", "iceAccumulation")}
    temp_c = grid_uom(props, "temperature").endswith("degC") or not grid_uom(props, "temperature")

    hours: set[datetime] = set(hourly)
    for s in (weather, *amounts.values()):
        for start, end, _ in s:
            hours.update(hours_in(start, end))
    for h in hours:
        p = hourly.get(h)
        temp = _period_temp_f(p) if p else None
        if temp is None:
            g = value_at(temps, h)
            temp = (c_to_f(g) if temp_c else g) if isinstance(g, (int, float)) else None
        kind = weather_type(value_at(weather, h), temp)
        if kind is None and p:
            cond = conditions.from_nws(p.get("icon"), p.get("shortForecast")).name
            if cond in conditions.SEVERITY and conditions.SEVERITY[cond] >= conditions.SEVERITY["drizzle"]:
                kind = conditions.precip_type(cond)
        if kind is None:
            kind = "snow" if temp is not None and temp <= FREEZING_F else "rain"
        table.types[h] = kind

    def weight(h: datetime) -> float:
        v = value_at(pops, h)
        if isinstance(v, (int, float)):
            return float(v)
        p = hourly.get(h)
        return float(_pop(p)) if p else 0.0

    table.snow = spread(amounts["snowfallAmount"], lambda h: table.type_at(h) == "snow", weight,
                        _amount_converter(grid_uom(props, "snowfallAmount")))
    table.ice = spread(amounts["iceAccumulation"], lambda h: table.type_at(h) == "ice", weight,
                       _amount_converter(grid_uom(props, "iceAccumulation")))
    qpf = spread(amounts["quantitativePrecipitation"], lambda h: True, weight,
                 _amount_converter(grid_uom(props, "quantitativePrecipitation")))
    # Liquid counts as rain only in hours without snow or ice (QPF is the melted total of everything).
    table.rain = {h: v for h, v in qpf.items() if not table.snow.get(h) and not table.ice.get(h)}
    for h, kind in table.types.items():  # an amount spread onto an hour of another type re-types it
        if kind == "rain" and table.snow.get(h, 0) > 0:
            table.types[h] = "snow"
        elif kind == "rain" and table.ice.get(h, 0) > 0:
            table.types[h] = "ice"
    uom = grid_uom(props, "windGust") or "wmoUnit:km_h-1"
    for start, end, v in grid_series(props, "windGust"):
        mph = to_mph({"value": v, "unitCode": uom})
        if mph is not None:
            for h in hours_in(start, end):
                table.gust[h] = mph
    return table


# ------------------------------------------------------------------ hourly

def _pop(p: dict[str, Any] | None) -> int:
    v = ((p or {}).get("probabilityOfPrecipitation") or {}).get("value")
    try:
        return max(0, min(100, round(float(v))))
    except (TypeError, ValueError):
        return 0


def _period_temp_f(p: dict[str, Any]) -> float | None:
    t = p.get("temperature")
    if isinstance(t, dict):  # some responses give a {unitCode, value} quantity
        v, unit = t.get("value"), str(t.get("unitCode") or "")
        if v is None:
            return None
        return float(v) if unit.endswith("degF") else c_to_f(float(v))
    if not isinstance(t, (int, float)):
        return None
    return float(t) if (p.get("temperatureUnit") or "F") == "F" else c_to_f(float(t))


def hourly_periods(doc: dict[str, Any] | None) -> dict[datetime, dict[str, Any]]:
    """forecastHourly periods keyed by their UTC hour start."""
    out: dict[datetime, dict[str, Any]] = {}
    for p in ((doc or {}).get("properties") or {}).get("periods") or []:
        t = parse_time(p.get("startTime"))
        if t is not None:
            out[floor_hour(t.astimezone(UTC))] = p
    return out


def build_hourly(periods: dict[datetime, dict[str, Any]], table: HourTable, now: datetime,
                 tz: ZoneInfo) -> list[shapes.HourlyPoint]:
    first = floor_hour(now.astimezone(UTC))
    out: list[shapes.HourlyPoint] = []
    for h in sorted(periods):
        if h < first:
            continue
        p = periods[h]
        temp = _period_temp_f(p)
        if temp is None:
            continue
        speed = parse_wind_speed(p.get("windSpeed"))
        gust = table.gust.get(h)
        out.append(shapes.HourlyPoint(
            t=h.astimezone(tz),
            temp=rnd(temp),
            pop=_pop(p),
            precip_type=table.type_at(h),
            rain_in=rnd(table.rain.get(h, 0.0), 3),
            snow_in=rnd(table.snow.get(h, 0.0), 3),
            ice_in=rnd(table.ice.get(h, 0.0), 3),
            wind_speed=rnd(speed),
            wind_gust=rnd(max(gust, speed or 0.0)) if gust is not None else None,
            wind_dir=compass_deg(p.get("windDirection")),
            icon=conditions.from_nws(p.get("icon"), p.get("shortForecast"), p.get("isDaytime")).name,
        ))
        if len(out) >= HOURS:
            break
    return out


# ------------------------------------------------------------------ daily

def build_daily(doc: dict[str, Any], table: HourTable | None, now: datetime,
                tz: ZoneInfo) -> list[shapes.DailyForecast]:
    """Merge 12-hour periods into calendar days (today first)."""
    days: dict[date, dict[str, dict[str, Any]]] = defaultdict(dict)
    for p in (doc.get("properties") or {}).get("periods") or []:
        start = parse_time(p.get("startTime"))
        if start is None:
            continue
        local = start.astimezone(tz)
        if p.get("isDaytime"):
            days[local.date()].setdefault("day", p)
        else:  # a night belongs to the day it follows ("Overnight" after midnight → the previous date)
            days[(local - timedelta(hours=12)).date()].setdefault("night", p)

    snow_by_day: dict[date, float] = defaultdict(float)
    ice_by_day: dict[date, float] = defaultdict(float)
    gust_by_day: dict[date, float] = {}
    first = floor_hour(now.astimezone(UTC))
    if table is not None:
        for src, dst in ((table.snow, snow_by_day), (table.ice, ice_by_day)):
            for h, v in src.items():
                if h >= first:
                    dst[h.astimezone(tz).date()] += v
        for h, v in table.gust.items():
            local = h.astimezone(tz)
            if h >= first and 6 <= local.hour < 18:  # daytime gusts, like the day period's wind
                gust_by_day[local.date()] = max(gust_by_day.get(local.date(), 0.0), v)

    out: list[shapes.DailyForecast] = []
    today = now.astimezone(tz).date()
    for i in range(DAYS):
        d = today + timedelta(days=i)
        day, night = days.get(d, {}).get("day"), days.get(d, {}).get("night")
        main = day or night
        if main is None:
            break
        cond = conditions.from_nws(main.get("icon"), main.get("shortForecast"), bool(day))
        pop = max(_pop(day), _pop(night)) if day and night else _pop(main)
        snow, ice = snow_by_day.get(d, 0.0), ice_by_day.get(d, 0.0)
        speed = parse_wind_speed(main.get("windSpeed"))
        gust = gust_by_day.get(d)
        out.append(shapes.DailyForecast(
            date=d,
            icon=cond.name,
            text=short_text(main.get("shortForecast"), cond.name),
            high=rnd(_period_temp_f(day), 0) if day else None,
            low=rnd(_period_temp_f(night), 0) if night else None,
            wind_dir=compass_deg(main.get("windDirection")),
            wind_speed=rnd(speed),
            wind_gust=rnd(max(gust, speed or 0.0)) if gust is not None else None,
            pop=pop,
            precip_type=_day_type(snow, ice, day, night),
            snow_in=rnd(snow, 1),
            ice_in=rnd(ice, 2),
            detail=main.get("detailedForecast") or None,
        ))
    return out


def _day_type(snow: float, ice: float, day: dict | None, night: dict | None) -> str:
    """Grid amounts first (meaningful ice beats light snow), else the icon of the wetter period."""
    if ice >= 0.05 and snow < 1.0:
        return "ice"
    if snow >= 0.1:
        return "snow"
    if ice >= 0.01:
        return "ice"
    periods = [p for p in (day, night) if p]
    wettest = max(periods, key=_pop)  # max() keeps the first (the day) on a tie
    return conditions.precip_type(conditions.from_nws(wettest.get("icon"), wettest.get("shortForecast")).name)


# ------------------------------------------------------------------ shortForecast → short text

MAX_TEXT = 24

SKY_TEXT = {
    "sunny": "Sunny", "mostly sunny": "Mostly sunny", "partly sunny": "Partly sunny", "clear": "Clear",
    "mostly clear": "Mostly clear", "partly cloudy": "Partly cloudy", "mostly cloudy": "Mostly cloudy",
    "cloudy": "Cloudy", "overcast": "Cloudy", "fair": "Fair", "increasing clouds": "Increasing clouds",
    "decreasing clouds": "Clearing", "becoming sunny": "Becoming sunny", "becoming cloudy": "Becoming cloudy",
    "hot": "Hot", "cold": "Cold",
}

CONDITION_TEXT = {  # last resort: the icon's own name
    "sunny": "Sunny", "clear-night": "Clear", "partly-cloudy": "Partly cloudy", "partly-cloudy-night": "Partly cloudy",
    "cloudy": "Cloudy", "fog": "Fog", "haze-smoke": "Haze", "drizzle": "Drizzle", "rain": "Rain",
    "heavy-rain": "Heavy rain", "thunderstorm": "Storms", "hail": "Storms, hail", "flurries": "Flurries",
    "snow": "Snow", "heavy-snow": "Heavy snow", "blowing-snow": "Blowing snow", "sleet": "Sleet",
    "freezing-rain": "Freezing rain", "wintry-mix": "Wintry mix", "windy": "Windy",
}

_OBSCURATION = re.compile(r"\b(fog|haze|smoke|dust|mist|ash)\b")
_PRECIP = re.compile(r"rain|snow|sleet|drizzle|shower|storm|flurr|hail|wintry|ice|precip")
_COVERAGE = re.compile(r"^(isolated|scattered|patchy|areas of|periods of|occasional|widespread|numerous|"
                       r"intermittent|frequent|brief)\s+")
_WIND = re.compile(r"\s+and\s+(breezy|windy|blustery)$")


@dataclass(frozen=True)
class _Part:
    kind: str  # sky | precip | obscuration
    prob: str | None  # slight | chance | likely | None
    noun: str  # lower case, e.g. "storms", "rain/snow", "patchy fog"; for sky the display text
    wind: str | None = None

    @property
    def bare(self) -> str:
        """Noun without coverage words ("scattered storms" → "storms")."""
        return _COVERAGE.sub("", self.noun)


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def _part(text: str) -> _Part:
    s = " ".join(text.lower().split())
    wind = None
    if m := _WIND.search(s):
        wind, s = m.group(1), s[:m.start()]
    if s in SKY_TEXT:
        return _Part("sky", None, SKY_TEXT[s], wind)
    prob = None
    if s.startswith("slight chance "):
        prob, s = "slight", s[len("slight chance "):]
    elif s.startswith("chance "):
        prob, s = "chance", s[len("chance "):]
    if s.endswith(" likely"):
        prob, s = "likely", s[: -len(" likely")]
    if "thunderstorm" in s or "t-storm" in s:
        m = _COVERAGE.match(s)
        noun = (m.group(1) + " " if m else "") + ("severe storms" if "severe" in s else "storms")
    else:
        noun = re.sub(r"\brain showers\b", "showers", s).replace(" and ", "/")
    kind = "obscuration" if _OBSCURATION.search(s) and not _PRECIP.search(s) else "precip"
    return _Part(kind, prob, noun, wind)


def _phrases(p: _Part) -> list[str]:
    """Ways to say one part, longest first."""
    if p.kind == "sky":
        base = [p.noun]
    else:
        n, b = p.noun, p.bare
        base = {
            "slight": [f"Slight chance of {n}", f"Slight chance {n}", f"{_cap(n)} possible", f"{_cap(b)} possible"],
            "chance": [f"Chance of {n}", f"Chance {n}", f"{_cap(n)} possible", f"{_cap(b)} possible"],
            "likely": [f"{_cap(n)} likely", f"{_cap(b)} likely", _cap(b)],
            None: [_cap(n), _cap(b)],
        }[p.prob]
    if p.wind:
        base = [f"{x}, {p.wind}" for x in base] + base
    return base


def _first_fit(cands: list[str]) -> str | None:
    return next((c for c in cands if len(c) <= MAX_TEXT), None)


def short_text(short_forecast: str | None, condition: str = "cloudy") -> str:
    """NWS shortForecast → a short description (≤ 24 chars) for the 7-day list. Deterministic; see tests."""
    fallback = CONDITION_TEXT.get(condition, "")
    if not short_forecast or not short_forecast.strip():
        return fallback
    parts = [_part(x) for x in re.split(r"\s+then\s+", short_forecast.strip(), flags=re.I) if x.strip()]
    if not parts:
        return fallback
    if len(parts) == 1:
        return _first_fit(_phrases(parts[0])) or fallback
    a, b = parts[0], parts[-1]
    if a.kind == "sky" and b.kind == "sky":
        cands = _phrases(a)
    elif a.kind == "sky":  # dry, then something later
        cands = _phrases(a) if b.prob == "slight" else [f"{x} late" for x in _phrases(b)]
    elif b.kind == "sky":  # something early, then dry
        cands = _phrases(b) if a.prob == "slight" else [f"{x} early" for x in _phrases(a)]
    elif a.bare == b.bare:  # "Chance Rain then Rain Likely"
        cands = _phrases(b if b.prob in ("likely", None) else a)
    else:  # one kind of precipitation turning into another
        cands = [f"{_cap(a.bare)} → {b.bare}", *_phrases(b), *_phrases(a)]
    return _first_fit(cands) or fallback


# ------------------------------------------------------------------ provider

@register("nws_forecast")
class NwsForecastProvider:
    """Config: `nws_fc: {type: nws_forecast, ttl: 30m}` (location from `location`, grid from /points)."""

    def __init__(self, name: str, cfg: ProviderCfg, app: AppConfig):
        self.name = name
        self.ttl = cfg.ttl
        self.app = app
        self.tz = ZoneInfo(app.location.tz)

    async def fetch(self, ctx: RequestContext) -> dict[str, Reading]:
        now = ctx.now
        async with client(self.app) as http:
            pt = await get_point(http, self.app, now)
            hourly_doc, grid_doc, daily_doc = await asyncio.gather(
                get_json(http, pt.forecast_hourly), get_json(http, pt.forecast_grid_data),
                get_json(http, pt.forecast), return_exceptions=True)
        results = (hourly_doc, grid_doc, daily_doc)
        for r in results:
            if isinstance(r, BaseException) and not isinstance(r, NwsError):
                raise r
        if any(isinstance(r, NwsError) and r.note == "nws_http_404" for r in results):
            forget_point(self.app)  # the grid moved; look it up again next time
        if all(isinstance(r, NwsError) for r in results):
            raise results[0]

        grid = None if isinstance(grid_doc, NwsError) else (grid_doc.get("properties") or {})
        periods = {} if isinstance(hourly_doc, NwsError) else hourly_periods(hourly_doc)
        table = build_hour_table(grid, periods)
        out: dict[str, Reading] = {}
        if isinstance(hourly_doc, NwsError):
            out["forecast.hourly"] = error(self.name, hourly_doc.note)
        else:
            out["forecast.hourly"] = self._reading(build_hourly(periods, table, now, self.tz), now, grid is None)
        if isinstance(daily_doc, NwsError):
            out["forecast.daily"] = error(self.name, daily_doc.note)
        else:
            daily = build_daily(daily_doc, table if grid is not None else None, now, self.tz)
            out["forecast.daily"] = self._reading(daily, now, grid is None)
        return out

    def _reading(self, items: list, now: datetime, no_grid: bool) -> Reading:
        if not items:
            return Reading(source=self.name, status=Status.missing, observed_at=now, note="nws_empty_forecast")
        return Reading(value=shapes.dump(items), observed_at=now, source=self.name,
                       note="nws_grid_unavailable" if no_grid else None)
