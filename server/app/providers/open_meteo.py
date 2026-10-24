"""`open_meteo` provider: US AQI, UV index and a fallback forecast from Open-Meteo (no key).

Two requests per refresh, run concurrently:

- air-quality API → `air.aqi` (current `us_aqi`)
- forecast API (°F, mph, inches, unix times) → `uv.index`, `outdoor.temperature`, `conditions.current` (current
  block), `forecast.hourly` (from the current hour, `hours` entries) and `forecast.daily` (7 days, today first)

Readings are keyed by canonical field names. If one request fails, its fields come back as error Readings and the
other request's fields are still served; if both fail, `fetch` raises.

Config: `om: {type: open_meteo, ttl: 30m}`; optional `hours` (default 72) and `timeout` (seconds, default 6).
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .. import conditions, shapes
from ..config import AppConfig, ProviderCfg
from ..readings import Reading, error, missing
from .base import RequestContext, register

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
AIR_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"

CURRENT_VARS = ("temperature_2m", "weather_code", "is_day", "uv_index")
HOURLY_VARS = ("temperature_2m", "precipitation_probability", "rain", "showers", "snowfall", "weather_code",
               "wind_speed_10m", "wind_gusts_10m", "wind_direction_10m", "is_day")
DAILY_VARS = ("weather_code", "temperature_2m_max", "temperature_2m_min", "precipitation_probability_max",
              "snowfall_sum", "wind_speed_10m_max", "wind_gusts_10m_max", "wind_direction_10m_dominant")

AIR_KEYS = ("air.aqi",)
FORECAST_KEYS = ("uv.index", "outdoor.temperature", "conditions.current", "forecast.hourly", "forecast.daily")

# Conditions with no precipitation of their own (precip_type is then inferred from snowfall/temperature).
_DRY = {"sunny", "clear-night", "partly-cloudy", "partly-cloudy-night", "cloudy", "fog", "haze-smoke", "windy"}

# Short daily text: (plain, noun used in "Chance of …" / "… likely").
_TEXT = {
    "sunny": ("Sunny", None), "clear-night": ("Clear", None), "partly-cloudy": ("Partly cloudy", None),
    "partly-cloudy-night": ("Partly cloudy", None), "cloudy": ("Cloudy", None), "fog": ("Fog", None),
    "haze-smoke": ("Haze", None), "windy": ("Windy", None),
    "drizzle": ("Drizzle", "drizzle"), "rain": ("Rain", "rain"), "heavy-rain": ("Heavy rain", "heavy rain"),
    "thunderstorm": ("Storms", "storms"), "hail": ("Storms, hail", "storms"), "flurries": ("Flurries", "flurries"),
    "snow": ("Snow", "snow"), "heavy-snow": ("Heavy snow", "heavy snow"),
    "blowing-snow": ("Blowing snow", "blowing snow"), "sleet": ("Sleet", "sleet"),
    "freezing-rain": ("Freezing rain", "freezing rain"), "wintry-mix": ("Wintry mix", "wintry mix"),
}
TEXT_MAX = 24


class OpenMeteoError(RuntimeError):
    pass


MIN_POP = 20  # below this a precipitation code is not worth an icon or a mention (as NWS does)


def daily_icon(icon: str, pop: int | None) -> str:
    """The day's condition; Open-Meteo reports the worst code of the day, so a 4 % drizzle day becomes 'cloudy'."""
    if icon not in _DRY and pop is not None and pop < MIN_POP:
        return "cloudy"
    return icon


def daily_text(icon: str, pop: int | None) -> str:
    """'Partly cloudy', 'Rain likely' (pop ≥ 60), 'Chance of rain' (pop 30–59), 'Slight chance of rain' (< 30)."""
    plain, noun = _TEXT.get(icon, ("Cloudy", None))
    if noun is None or pop is None:
        return plain
    if pop >= 60:
        return f"{noun[0].upper()}{noun[1:]} likely"
    if pop >= 30:
        return f"Chance of {noun}"
    slight = f"Slight chance of {noun}"
    return slight if len(slight) <= TEXT_MAX else f"Chance of {noun}"


def _inches(v: Any, unit: str | None) -> float:
    """Precipitation/snow amount → inches (Open-Meteo reports snowfall in cm unless asked for inches)."""
    if v is None:
        return 0.0
    x = float(v)
    u = (unit or "inch").lower()
    if u == "cm":
        x /= 2.54
    elif u == "mm":
        x /= 25.4
    return round(x, 2)


def _num(v: Any) -> float | None:
    return None if v is None else float(v)


def _pop(v: Any) -> int:
    return 0 if v is None else max(0, min(100, round(float(v))))


def _ptype(icon: str, snow_in: float, temp_f: float | None) -> str:
    """What would fall: from the condition when it is a precipitation one, else snowfall / freezing temperature."""
    if icon not in _DRY:
        return conditions.precip_type(icon)
    if snow_in > 0 or (temp_f is not None and temp_f <= 32):
        return "snow"
    return "rain"


def _ts(v: Any, tz: ZoneInfo) -> datetime:
    return datetime.fromtimestamp(int(v), tz)


def parse_air(data: dict[str, Any], source: str, tz: ZoneInfo) -> dict[str, Reading]:
    cur = data.get("current") or {}
    aqi = cur.get("us_aqi")
    if aqi is None:
        return {"air.aqi": missing(source, "no_value")}
    return {"air.aqi": Reading(value=float(aqi), unit="AQI", observed_at=_ts(cur["time"], tz), source=source)}


def parse_hourly(data: dict[str, Any], tz: ZoneInfo, now: datetime, hours: int) -> list[dict[str, Any]]:
    h = data.get("hourly") or {}
    units = data.get("hourly_units") or {}
    times = h.get("time") or []
    start = now.replace(minute=0, second=0, microsecond=0).timestamp()

    def col(name: str, i: int) -> Any:
        c = h.get(name)
        return c[i] if c is not None and i < len(c) else None

    out: list[shapes.HourlyPoint] = []
    for i, t in enumerate(times):
        if int(t) < start:
            continue
        temp = col("temperature_2m", i)
        if temp is None:
            break  # past the end of the model run
        code = col("weather_code", i)
        is_day = col("is_day", i)
        icon = conditions.from_wmo(code, True if is_day is None else bool(is_day)).name
        liquid = _inches(col("rain", i), units.get("rain")) + _inches(col("showers", i), units.get("showers"))
        snow = _inches(col("snowfall", i), units.get("snowfall"))
        ptype = _ptype(icon, snow, temp)
        out.append(shapes.HourlyPoint(
            t=_ts(t, tz), temp=float(temp), pop=_pop(col("precipitation_probability", i)), precip_type=ptype,
            rain_in=0.0 if ptype == "ice" else round(liquid, 2), snow_in=snow,
            ice_in=round(liquid, 2) if ptype == "ice" else 0.0,
            wind_speed=_num(col("wind_speed_10m", i)), wind_gust=_num(col("wind_gusts_10m", i)),
            wind_dir=_num(col("wind_direction_10m", i)), icon=icon,
        ))
        if len(out) >= hours:
            break
    return shapes.dump(out)


def parse_daily(data: dict[str, Any], tz: ZoneInfo, days: int = 7) -> list[dict[str, Any]]:
    d = data.get("daily") or {}
    units = data.get("daily_units") or {}
    out: list[shapes.DailyForecast] = []
    for i, t in enumerate((d.get("time") or [])[:days]):
        def col(name: str, i: int = i) -> Any:
            c = d.get(name)
            return c[i] if c is not None and i < len(c) else None

        raw_pop = col("precipitation_probability_max")
        pop = None if raw_pop is None else _pop(raw_pop)
        code_icon = conditions.from_wmo(col("weather_code"), True).name
        icon = daily_icon(code_icon, pop)
        snow = _inches(col("snowfall_sum"), units.get("snowfall_sum"))
        high = _num(col("temperature_2m_max"))
        out.append(shapes.DailyForecast(
            date=_ts(t, tz).date(), icon=icon, text=daily_text(icon, pop),
            high=high, low=_num(col("temperature_2m_min")),
            wind_dir=_num(col("wind_direction_10m_dominant")), wind_speed=_num(col("wind_speed_10m_max")),
            wind_gust=_num(col("wind_gusts_10m_max")), pop=_pop(raw_pop),
            precip_type=_ptype(code_icon, snow, high), snow_in=snow,
        ))
    return shapes.dump(out)


def parse_forecast(data: dict[str, Any], source: str, tz: ZoneInfo, now: datetime,
                   hours: int = 72) -> dict[str, Reading]:
    out: dict[str, Reading] = {}
    cur = data.get("current") or {}
    cur_at = _ts(cur["time"], tz) if cur.get("time") is not None else None

    def current(key: str, value: Any, unit: str | None) -> None:
        out[key] = (Reading(value=value, unit=unit, observed_at=cur_at, source=source)
                    if value is not None and cur_at is not None else missing(source, "no_value"))

    uv = cur.get("uv_index")
    current("uv.index", None if uv is None else round(float(uv), 1), None)
    temp = cur.get("temperature_2m")
    current("outdoor.temperature", None if temp is None else float(temp), "°F")
    code = cur.get("weather_code")
    is_day = bool(cur.get("is_day", 1))
    current("conditions.current", None if code is None else conditions.from_wmo(code, is_day).name, None)

    hourly = parse_hourly(data, tz, now, hours)
    out["forecast.hourly"] = (Reading(value=hourly, observed_at=now, source=source) if hourly
                              else missing(source, "no_value"))
    daily = parse_daily(data, tz)
    out["forecast.daily"] = (Reading(value=daily, observed_at=now, source=source) if daily
                             else missing(source, "no_value"))
    return out


def _note(e: BaseException) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        return f"http_{e.response.status_code}"
    if isinstance(e, httpx.TimeoutException):
        return "timeout"
    if isinstance(e, httpx.HTTPError):
        return "unreachable"
    return "bad_response"


@register("open_meteo")
class OpenMeteoProvider:
    def __init__(self, name: str, cfg: ProviderCfg, app: AppConfig):
        self.name = name
        self.ttl = cfg.ttl
        opts = cfg.options
        self.lat, self.lon = app.location.lat, app.location.lon
        self.tz_name = app.location.tz
        self.tz = ZoneInfo(app.location.tz)
        self.hours = int(opts.get("hours", 72))
        self.timeout = float(opts.get("timeout", 6))
        self.user_agent = app.nws.user_agent

    def forecast_params(self) -> dict[str, Any]:
        days = max(7, (self.hours + 47) // 24)  # enough days to cover `hours` from late in the day
        return {
            "latitude": self.lat, "longitude": self.lon,
            "current": ",".join(CURRENT_VARS), "hourly": ",".join(HOURLY_VARS), "daily": ",".join(DAILY_VARS),
            "temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "precipitation_unit": "inch",
            "timezone": self.tz_name, "timeformat": "unixtime", "forecast_days": days,
        }

    def air_params(self) -> dict[str, Any]:
        return {"latitude": self.lat, "longitude": self.lon, "current": "us_aqi", "timezone": self.tz_name,
                "timeformat": "unixtime"}

    async def _get(self, client: httpx.AsyncClient, url: str, params: dict[str, Any]) -> dict[str, Any]:
        r = await client.get(url, params=params)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict) or data.get("error"):
            raise OpenMeteoError(str(data.get("reason") if isinstance(data, dict) else data))
        return data

    async def fetch(self, ctx: RequestContext) -> dict[str, Reading]:
        async with httpx.AsyncClient(timeout=self.timeout, headers={"User-Agent": self.user_agent}) as client:
            air, fc = await asyncio.gather(self._get(client, AIR_URL, self.air_params()),
                                           self._get(client, FORECAST_URL, self.forecast_params()),
                                           return_exceptions=True)
        if isinstance(air, BaseException) and isinstance(fc, BaseException):
            raise fc
        out: dict[str, Reading] = {}
        now = ctx.now.astimezone(self.tz)
        for keys, res, parse in ((AIR_KEYS, air, lambda d: parse_air(d, self.name, self.tz)),
                                 (FORECAST_KEYS, fc, lambda d: parse_forecast(d, self.name, self.tz, now,
                                                                              self.hours))):
            if isinstance(res, BaseException):
                out.update({k: error(self.name, _note(res)) for k in keys})
                continue
            try:
                out.update(parse(res))
            except (KeyError, TypeError, ValueError) as e:
                out.update({k: error(self.name, _note(e)) for k in keys})
        return out

