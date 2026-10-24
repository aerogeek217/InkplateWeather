"""open_meteo provider against responses recorded for Savoy, IL on 2026-10-24 (offline, respx)."""
import copy
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from conftest import TESTS_DIR

from app import shapes
from app.config import AppConfig, LocationCfg, ProviderCfg
from app.providers import base, open_meteo
from app.providers.base import RequestContext
from app.readings import Status

TZ = ZoneInfo("America/Chicago")
NOW = datetime(2026, 10, 24, 11, 43, tzinfo=TZ)  # when the responses were recorded
RECORDED = TESTS_DIR / "data" / "recorded"


def load(name: str) -> dict:
    return json.loads((RECORDED / name).read_text(encoding="utf-8"))


FORECAST = load("om_forecast_2026-10-24.json")
AIR = load("om_air_2026-10-24.json")


@pytest.fixture
def provider():
    app = AppConfig(location=LocationCfg(name="Savoy, IL", lat=40.0617, lon=-88.2517, tz="America/Chicago"))
    return base.build("om", ProviderCfg(type="open_meteo", ttl="30m"), app)


def mock(router, forecast=FORECAST, air=AIR):
    fc = router.get(open_meteo.FORECAST_URL).mock(
        return_value=forecast if isinstance(forecast, httpx.Response) else httpx.Response(200, json=forecast))
    aq = router.get(open_meteo.AIR_URL).mock(
        return_value=air if isinstance(air, httpx.Response) else httpx.Response(200, json=air))
    return fc, aq


async def test_recorded_current_values(provider):
    assert isinstance(provider, open_meteo.OpenMeteoProvider)
    assert provider.ttl == timedelta(minutes=30)
    with respx.mock(assert_all_called=True) as router:
        fc, aq = mock(router)
        out = await provider.fetch(RequestContext(now=NOW))
    assert out["air.aqi"].value == 30 and out["air.aqi"].unit == "AQI"
    assert out["air.aqi"].observed_at == datetime(2026, 10, 24, 11, 0, tzinfo=TZ)
    assert out["uv.index"].value == 3.8
    assert out["outdoor.temperature"].value == 65.3 and out["outdoor.temperature"].unit == "°F"
    assert out["conditions.current"].value == "partly-cloudy"
    cur_at = datetime(2026, 10, 24, 11, 30, tzinfo=TZ)
    assert all(out[k].observed_at == cur_at for k in ("uv.index", "outdoor.temperature", "conditions.current"))
    assert all(r.status is Status.ok and r.source == "om" for r in out.values())
    assert set(out) == set(open_meteo.AIR_KEYS + open_meteo.FORECAST_KEYS)
    # Two requests, in the units the board uses, local timezone, unix times.
    assert fc.call_count == 1 and aq.call_count == 1
    q = dict(fc.calls.last.request.url.params)
    assert q["temperature_unit"] == "fahrenheit" and q["wind_speed_unit"] == "mph"
    assert q["precipitation_unit"] == "inch" and q["timezone"] == "America/Chicago"
    assert q["timeformat"] == "unixtime" and int(q["forecast_days"]) >= 7
    assert "uv_index" in q["current"] and "snowfall" in q["hourly"]
    assert dict(aq.calls.last.request.url.params)["current"] == "us_aqi"
    assert "InkplateWeather" in fc.calls.last.request.headers["user-agent"]


async def test_recorded_forecasts_match_shapes(provider):
    with respx.mock as router:
        mock(router)
        out = await provider.fetch(RequestContext(now=NOW))
    hourly = out["forecast.hourly"]
    assert hourly.observed_at == NOW
    pts = shapes.validate("forecast.hourly", hourly.value)
    assert len(pts) == 72 >= 48
    assert pts[0].t == datetime(2026, 10, 24, 11, 0, tzinfo=TZ)  # from the current hour
    assert all(b.t - a.t == timedelta(hours=1) for a, b in zip(pts, pts[1:], strict=False))
    assert pts[0].temp == 63.2 and pts[0].pop == 0 and pts[0].precip_type == "rain"
    assert pts[0].wind_speed == 5.5 and pts[0].wind_gust == 7.6 and pts[0].wind_dir == 151
    assert pts[0].icon == "cloudy"

    daily = shapes.validate("forecast.daily", out["forecast.daily"].value)
    assert [d.date.isoformat() for d in daily] == [f"2026-10-{n}" for n in range(24, 31)]
    today = daily[0]
    assert (today.high, today.low, today.wind_dir, today.wind_speed, today.wind_gust) == (71.2, 43.4, 124, 6.1, 13.6)
    assert (today.icon, today.text, today.pop) == ("cloudy", "Cloudy", 1)
    # Day 7 is WMO 51 (drizzle) at 4 %: not worth a drizzle icon.
    assert (daily[6].icon, daily[6].text, daily[6].pop) == ("cloudy", "Cloudy", 4)


async def test_air_quality_failure_keeps_the_forecast(provider):
    with respx.mock as router:
        mock(router, air=httpx.Response(503))
        out = await provider.fetch(RequestContext(now=NOW))
    assert out["air.aqi"].status is Status.error and out["air.aqi"].note == "http_503"
    assert out["uv.index"].status is Status.ok and out["forecast.daily"].usable


async def test_forecast_failure_keeps_air_quality(provider):
    with respx.mock as router:
        mock(router, forecast=httpx.Response(429))
        out = await provider.fetch(RequestContext(now=NOW))
    assert out["air.aqi"].value == 30
    for k in open_meteo.FORECAST_KEYS:
        assert out[k].status is Status.error and out[k].note == "http_429"


async def test_both_failing_raises(provider):
    with respx.mock as router:
        mock(router, forecast=httpx.Response(500), air=httpx.Response(502))
        with pytest.raises(httpx.HTTPStatusError):
            await provider.fetch(RequestContext(now=NOW))


async def test_api_error_body_is_an_error(provider):
    with respx.mock as router:
        mock(router, forecast={"error": True, "reason": "Cannot initialize WeatherVariable"})
        out = await provider.fetch(RequestContext(now=NOW))
    assert out["forecast.hourly"].status is Status.error and out["forecast.hourly"].note == "bad_response"


async def test_timeout_is_reported(provider):
    with respx.mock as router:
        mock(router, air=httpx.Response(200, json=AIR))
        router.get(open_meteo.FORECAST_URL).mock(side_effect=httpx.ReadTimeout("slow"))
        out = await provider.fetch(RequestContext(now=NOW))
    assert out["forecast.daily"].note == "timeout"


def test_missing_aqi_value_is_missing():
    out = open_meteo.parse_air({"current": {"time": 1792857600, "us_aqi": None}}, "om", TZ)
    assert out["air.aqi"].status is Status.missing


def _winterize(data: dict) -> dict:
    """The recorded response with a snowy first hour (cm units) and a freezing-rain second hour."""
    d = copy.deepcopy(data)
    h = d["hourly"]
    i = next(n for n, t in enumerate(h["time"]) if t >= NOW.replace(minute=0).timestamp())
    d["hourly_units"]["snowfall"] = "cm"
    h["weather_code"][i], h["snowfall"][i], h["temperature_2m"][i] = 73, 2.54, 28.0
    h["weather_code"][i + 1], h["rain"][i + 1], h["showers"][i + 1] = 66, 0.03, 0.01
    h["weather_code"][i + 2], h["temperature_2m"][i + 2], h["snowfall"][i + 2] = 3, 30.0, 0.0
    dd = d["daily"]
    dd["weather_code"][1], dd["precipitation_probability_max"][1], dd["snowfall_sum"][1] = 75, 90, 5.2
    dd["weather_code"][2], dd["precipitation_probability_max"][2] = 66, 40
    dd["weather_code"][3], dd["precipitation_probability_max"][3] = 61, 25
    return d


def test_snow_and_ice_amounts():
    out = open_meteo.parse_forecast(_winterize(FORECAST), "om", TZ, NOW)
    pts = out["forecast.hourly"].value
    assert (pts[0]["icon"], pts[0]["precip_type"], pts[0]["snow_in"], pts[0]["rain_in"]) == ("snow", "snow", 1.0, 0)
    assert (pts[1]["icon"], pts[1]["precip_type"], pts[1]["ice_in"], pts[1]["rain_in"]) == (
        "freezing-rain", "ice", 0.04, 0)
    assert pts[2]["precip_type"] == "snow"  # dry hour below freezing: snow is what would fall
    days = out["forecast.daily"].value
    assert (days[1]["icon"], days[1]["text"], days[1]["precip_type"], days[1]["snow_in"]) == (
        "heavy-snow", "Heavy snow likely", "snow", 5.2)
    assert (days[2]["icon"], days[2]["text"], days[2]["precip_type"]) == ("freezing-rain", "Chance of freezing rain",
                                                                          "ice")
    assert (days[3]["icon"], days[3]["text"]) == ("rain", "Slight chance of rain")
    shapes.validate("forecast.hourly", pts)
    shapes.validate("forecast.daily", days)


def test_night_and_missing_current():
    d = copy.deepcopy(FORECAST)
    d["current"].update(weather_code=0, is_day=0, uv_index=None)
    out = open_meteo.parse_forecast(d, "om", TZ, NOW)
    assert out["conditions.current"].value == "clear-night"
    assert out["uv.index"].status is Status.missing


def test_hourly_times_cross_the_dst_change():
    start = int(datetime(2026, 11, 1, 0, 0, tzinfo=TZ).timestamp())
    n = 6
    d = {"hourly": {"time": [start + 3600 * i for i in range(n)], "temperature_2m": [40.0] * n,
                    "weather_code": [0] * n, "is_day": [0] * n}}
    pts = open_meteo.parse_hourly(d, TZ, datetime(2026, 11, 1, 0, 0, tzinfo=TZ), 48)
    offsets = [datetime.fromisoformat(p["t"]).utcoffset().total_seconds() / 3600 for p in pts]
    assert offsets == [-5, -5, -6, -6, -6, -6]  # 1 AM CDT, then 1 AM CST
    assert len({p["t"] for p in pts}) == n


@pytest.mark.parametrize("icon,pop,text", [
    ("partly-cloudy", 0, "Partly cloudy"), ("rain", 60, "Rain likely"), ("rain", 59, "Chance of rain"),
    ("thunderstorm", 70, "Storms likely"), ("snow", 20, "Slight chance of snow"), ("rain", None, "Rain"),
    ("wintry-mix", 25, "Chance of wintry mix"), ("sunny", 80, "Sunny"),
])
def test_daily_text(icon, pop, text):
    assert open_meteo.daily_text(icon, pop) == text
    assert len(text) <= open_meteo.TEXT_MAX
