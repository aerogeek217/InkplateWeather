"""nws_forecast: recorded Savoy forecast (2026-10-24, dry week) and a crafted winter storm (2027-01-19)."""
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from app import shapes
from app.config import AppConfig
from app.providers import base, nws_common
from app.providers import nws_forecast as fc
from app.providers.base import RequestContext
from app.providers.nws_common import NwsError
from app.readings import Status

RECORDED = Path(__file__).resolve().parent / "data" / "recorded"
TZ = ZoneInfo("America/Chicago")
FALL_NOW = datetime(2026, 10, 24, 16, 38, 10, tzinfo=UTC)  # 11:38 CDT, when the recordings were made
WINTER_NOW = datetime(2027, 1, 19, 15, 30, tzinfo=TZ)  # the winter board's time
API = "https://api.weather.gov"
GRID = f"{API}/gridpoints/ILX/95,70"


def recorded(name: str) -> dict:
    return json.loads((RECORDED / name).read_text(encoding="utf-8"))


@pytest.fixture
def app(tmp_path, monkeypatch) -> AppConfig:
    monkeypatch.setenv("INKPLATE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(nws_common, "_points_mem", {})
    monkeypatch.setattr(nws_common, "RETRY_DELAY_S", 0)
    return AppConfig.model_validate({
        "location": {"name": "Savoy, IL", "lat": 40.0617, "lon": -88.2517, "tz": "America/Chicago"},
        "providers": {"nws_fc": {"type": "nws_forecast", "ttl": "30m"}},
    })


def mock_api(season: str = "fall", hourly=None, grid=None, daily=None):
    prefix = "nws_" if season == "fall" else "nws_winter_"
    routes = {
        "points": respx.get(f"{API}/points/40.0617,-88.2517").respond(json=recorded("nws_points.json")),
        "hourly": respx.get(f"{GRID}/forecast/hourly").mock(
            side_effect=hourly or [httpx.Response(200, json=recorded(f"{prefix}forecast_hourly.json"))]),
        "daily": respx.get(f"{GRID}/forecast").mock(
            side_effect=daily or [httpx.Response(200, json=recorded(f"{prefix}forecast.json"))]),
        "grid": respx.get(GRID).mock(
            side_effect=grid or [httpx.Response(200, json=recorded(f"{prefix}griddata.json"))]),
    }
    return routes


async def fetch(app, now):
    p = base.build("nws_fc", app.providers["nws_fc"], app)
    return await p.fetch(RequestContext(now=now))


# ------------------------------------------------------------------ recorded (fall)

@respx.mock
async def test_recorded_fall(app):
    routes = mock_api("fall")
    r = await fetch(app, FALL_NOW)
    assert all(route.call_count == 1 for route in routes.values())
    hourly = shapes.validate("forecast.hourly", r["forecast.hourly"].value)
    daily = shapes.validate("forecast.daily", r["forecast.daily"].value)
    assert r["forecast.hourly"].observed_at == FALL_NOW and r["forecast.hourly"].note is None

    assert len(hourly) == fc.HOURS >= 48
    assert hourly[0].t == datetime(2026, 10, 24, 11, tzinfo=TZ)  # the 10:00 period has passed
    assert [h.t for h in hourly] == [hourly[0].t + timedelta(hours=i) for i in range(len(hourly))]
    h0 = hourly[0]
    assert (h0.temp, h0.pop, h0.precip_type, h0.wind_speed, h0.wind_dir, h0.icon) == (
        64.0, 1, "rain", 6.0, 157.5, "cloudy")
    assert h0.wind_gust == 8.1  # grid windGust 13 km/h
    assert sum(h.rain_in + h.snow_in + h.ice_in for h in hourly) == 0  # dry week

    assert [d.date for d in daily] == [date(2026, 10, 24) + timedelta(days=i) for i in range(7)]
    assert [(d.high, d.low) for d in daily] == [(72, 45), (75, 48), (75, 48), (73, 50), (77, 52), (75, 48), (74, 49)]
    assert [d.text for d in daily] == ["Partly sunny", "Mostly sunny", "Partly sunny", "Mostly cloudy",
                                       "Mostly sunny", "Sunny", "Mostly sunny"]
    assert [d.icon for d in daily] == ["cloudy", "partly-cloudy", "cloudy", "cloudy", "partly-cloudy", "sunny",
                                       "partly-cloudy"]
    assert [d.pop for d in daily] == [1, 1, 0, 1, 6, 6, 17]  # the higher of day and night
    assert (daily[4].wind_speed, daily[4].wind_dir) == (10, 180)  # "6 to 10 mph" S → upper value
    assert daily[0].detail.startswith("Partly sunny, with a high near 72.")
    assert all(d.precip_type == "rain" and d.snow_in == 0 for d in daily)


@respx.mock
async def test_points_looked_up_once(app):
    routes = mock_api("fall")
    await fetch(app, FALL_NOW)
    routes["hourly"].side_effect = None
    routes["hourly"].return_value = httpx.Response(200, json=recorded("nws_forecast_hourly.json"))
    routes["daily"].side_effect = None
    routes["daily"].return_value = httpx.Response(200, json=recorded("nws_forecast.json"))
    routes["grid"].side_effect = None
    routes["grid"].return_value = httpx.Response(200, json=recorded("nws_griddata.json"))
    await fetch(app, FALL_NOW + timedelta(minutes=30))  # a new provider instance, same cache
    assert routes["points"].call_count == 1
    assert routes["hourly"].call_count == 2


# ------------------------------------------------------------------ crafted winter storm

@respx.mock
async def test_winter_storm(app):
    mock_api("winter")
    r = await fetch(app, WINTER_NOW)
    hourly = shapes.validate("forecast.hourly", r["forecast.hourly"].value)
    daily = shapes.validate("forecast.daily", r["forecast.daily"].value)

    assert hourly[0].t == datetime(2027, 1, 19, 15, tzinfo=TZ)
    by_hour = {h.t.astimezone(TZ).strftime("%d %H"): h for h in hourly}
    # Sleet until 6 PM (the grid's ice accumulation goes there), then snow (the snowfall goes there).
    assert [by_hour[f"19 {h}"].precip_type for h in (15, 16, 17, 18, 23)] == ["ice", "ice", "ice", "snow", "snow"]
    assert all(by_hour[f"19 {h}"].ice_in > 0 and by_hour[f"19 {h}"].snow_in == 0 for h in (15, 16, 17))
    assert all(by_hour[f"19 {h}"].ice_in == 0 and by_hour[f"19 {h}"].snow_in > 0 for h in range(18, 24))
    assert all(h.rain_in == 0 for h in hourly[:20])
    # 1.27 mm of ice over 12–6 PM: the three hours already past keep their share.
    assert sum(h.ice_in for h in hourly) == pytest.approx(0.025, abs=0.002)
    # Next 12 hours (as the precip chart sums them): all 4 in of 6 PM–midnight, plus midnight–4 AM's share of the
    # 1 in for midnight–6 AM (chance 70 % for four hours, 40 % for two → 280/360 of it).
    nxt = [h for h in hourly if h.t > WINTER_NOW][:12]
    assert sum(h.snow_in for h in nxt) == pytest.approx(4.0 + 280 / 360, abs=0.01)
    assert by_hour["19 18"].wind_gust == 32.2  # 51.9 km/h

    d0, d1 = daily[0], daily[1]
    assert (d0.date, d0.text, d0.icon, d0.high, d0.low, d0.pop) == (date(2027, 1, 19), "Sleet → snow", "sleet", 31,
                                                                    19, 100)
    assert (d0.precip_type, d0.snow_in, d0.ice_in) == ("snow", 4.0, 0.03)
    assert (d0.wind_dir, d0.wind_speed) == (45, 15)
    assert (d1.text, d1.icon, d1.precip_type, d1.snow_in, d1.wind_speed) == (
        "Snow → blowing snow", "blowing-snow", "snow", 1.2, 25)
    assert [d.text for d in daily[2:]] == ["Sunny", "Mostly cloudy", "Chance freezing drizzle", "Rain/snow likely",
                                           "Slight chance flurries"]
    assert [d.precip_type for d in daily[2:]] == ["rain", "ice", "ice", "ice", "snow"]


@respx.mock
async def test_evening_only_tonight_remains(app):
    doc = recorded("nws_winter_forecast.json")
    doc["properties"]["periods"] = doc["properties"]["periods"][1:]  # "This Afternoon" has gone
    mock_api("winter", daily=[httpx.Response(200, json=doc)])
    r = await fetch(app, WINTER_NOW.replace(hour=18, minute=30))
    today = shapes.validate("forecast.daily", r["forecast.daily"].value)[0]
    assert (today.date, today.high, today.low, today.icon, today.text) == (date(2027, 1, 19), None, 19, "snow",
                                                                           "Snow")
    assert today.snow_in == 4.0  # 6 PM–midnight still to come


def test_overnight_period_belongs_to_the_previous_day():
    doc = {"properties": {"periods": [
        {"startTime": "2027-01-20T01:00:00-06:00", "isDaytime": False, "temperature": 12, "temperatureUnit": "F",
         "probabilityOfPrecipitation": {"value": 10}, "windSpeed": "5 mph", "windDirection": "N",
         "icon": "https://api.weather.gov/icons/land/night/few?size=medium", "shortForecast": "Mostly Clear"},
        {"startTime": "2027-01-20T06:00:00-06:00", "isDaytime": True, "temperature": 25, "temperatureUnit": "F",
         "probabilityOfPrecipitation": {"value": 0}, "windSpeed": "10 mph", "windDirection": "W",
         "icon": "https://api.weather.gov/icons/land/day/skc?size=medium", "shortForecast": "Sunny"},
    ]}}
    now = datetime(2027, 1, 20, 1, 30, tzinfo=TZ)
    days = fc.build_daily(doc, None, now, TZ)
    assert [(d.date, d.high, d.low) for d in days] == [(date(2027, 1, 20), 25, None)]


# ------------------------------------------------------------------ partial and total failure

@respx.mock
async def test_grid_down_keeps_hourly_and_daily(app):
    mock_api("winter", grid=[httpx.Response(503), httpx.Response(503)])
    r = await fetch(app, WINTER_NOW)
    assert r["forecast.hourly"].status is Status.ok and r["forecast.hourly"].note == "nws_grid_unavailable"
    hourly = shapes.validate("forecast.hourly", r["forecast.hourly"].value)
    assert all(h.snow_in == 0 and h.ice_in == 0 for h in hourly)
    assert [h.precip_type for h in hourly[:4]] == ["ice", "ice", "ice", "snow"]  # from the hourly icons
    assert r["forecast.daily"].status is Status.ok


@respx.mock
async def test_daily_rate_limited_is_an_error_reading(app):
    mock_api("fall", daily=[httpx.Response(429)])
    r = await fetch(app, FALL_NOW)
    assert (r["forecast.daily"].status, r["forecast.daily"].note) == (Status.error, "nws_rate_limited")
    assert r["forecast.hourly"].status is Status.ok


@respx.mock
async def test_everything_down_raises(app):
    bad = [httpx.Response(500), httpx.Response(500)]
    mock_api("fall", hourly=list(bad), grid=list(bad), daily=list(bad))
    with pytest.raises(NwsError):
        await fetch(app, FALL_NOW)


@respx.mock
async def test_points_down_raises(app):
    respx.get(f"{API}/points/40.0617,-88.2517").respond(429)
    with pytest.raises(NwsError) as e:
        await fetch(app, FALL_NOW)
    assert e.value.note == "nws_rate_limited"


@respx.mock
async def test_grid_404_forgets_the_cached_point(app):
    mock_api("fall", grid=[httpx.Response(404)])
    await fetch(app, FALL_NOW)
    assert nws_common.cached_point(app) is None


# ------------------------------------------------------------------ pieces

@pytest.mark.parametrize("entries,temp,expected", [
    ([{"coverage": "chance", "weather": "rain_showers"}], 50, "rain"),
    ([{"coverage": "likely", "weather": "sleet"}, {"coverage": "chance", "weather": "snow"}], 30, "ice"),
    ([{"coverage": "chance", "weather": "sleet"}, {"coverage": "likely", "weather": "snow"}], 30, "snow"),
    ([{"coverage": "chance", "weather": "rain"}, {"coverage": "chance", "weather": "snow"}], 31, "snow"),
    ([{"coverage": "chance", "weather": "rain"}, {"coverage": "chance", "weather": "snow"}], 36, "rain"),
    ([{"coverage": "areas", "weather": "freezing_drizzle"}], 30, "ice"),
    ([{"coverage": "patchy", "weather": "fog"}], 40, None),
    ([{"coverage": None, "weather": None}], 40, None),
    (None, 40, None),
])
def test_weather_type(entries, temp, expected):
    assert fc.weather_type(entries, temp) == expected


def test_spread_follows_chance_and_type():
    t0 = datetime(2027, 1, 20, tzinfo=UTC)
    series = [(t0, t0 + timedelta(hours=4), 40.0)]
    pops = {t0: 10, t0 + timedelta(hours=1): 30, t0 + timedelta(hours=2): 60, t0 + timedelta(hours=3): 0}
    out = fc.spread(series, lambda h: True, lambda h: pops[h], lambda v: v)
    assert [round(out[h], 3) for h in sorted(out)] == [4.0, 12.0, 24.0, 0.0]  # the 0 % hour gets nothing
    only_last_two = fc.spread(series, lambda h: h >= t0 + timedelta(hours=2), lambda h: 0, lambda v: v)
    assert dict(only_last_two) == {t0 + timedelta(hours=2): 20.0, t0 + timedelta(hours=3): 20.0}
    none_match = fc.spread(series, lambda h: False, lambda h: 1, lambda v: v)
    assert sum(none_match.values()) == 40 and len(none_match) == 4


def test_hourly_amounts_conserve_grid_totals():
    grid = recorded("nws_winter_griddata.json")["properties"]
    table = fc.build_hour_table(grid, fc.hourly_periods(recorded("nws_winter_forecast_hourly.json")))
    assert sum(table.snow.values()) == pytest.approx((101.6 + 25.4 + 5.08) / 25.4)
    assert sum(table.ice.values()) == pytest.approx(1.27 / 25.4)
    snow_hours = {h for h, v in table.snow.items() if v > 0}
    assert all(table.types[h] == "snow" for h in snow_hours)


# ------------------------------------------------------------------ shortForecast → text

TEXT_TABLE = [
    ("Sunny", "Sunny"), ("Mostly Sunny", "Mostly sunny"), ("Partly Sunny", "Partly sunny"),
    ("Partly Cloudy", "Partly cloudy"), ("Mostly Cloudy", "Mostly cloudy"), ("Cloudy", "Cloudy"),
    ("Clear", "Clear"), ("Mostly Clear", "Mostly clear"),
    ("Chance Showers And Thunderstorms", "Chance of storms"),
    ("Slight Chance Showers And Thunderstorms", "Slight chance of storms"),
    ("Showers And Thunderstorms Likely", "Storms likely"), ("Showers And Thunderstorms", "Storms"),
    ("Isolated Showers And Thunderstorms", "Isolated storms"), ("Scattered Showers And Thunderstorms",
                                                                 "Scattered storms"),
    ("Severe Thunderstorms", "Severe storms"),
    ("Slight Chance Rain Showers", "Slight chance of showers"), ("Chance Rain Showers", "Chance of showers"),
    ("Rain Showers Likely", "Showers likely"), ("Rain Showers", "Showers"), ("Light Rain Likely", "Light rain likely"),
    ("Rain", "Rain"), ("Heavy Rain", "Heavy rain"),
    ("Chance Rain And Snow", "Chance of rain/snow"), ("Rain And Snow Likely", "Rain/snow likely"),
    ("Snow Likely", "Snow likely"), ("Snow", "Snow"), ("Heavy Snow", "Heavy snow"),
    ("Chance Light Snow", "Chance of light snow"), ("Slight Chance Flurries", "Slight chance flurries"),
    ("Snow Showers Likely", "Snow showers likely"), ("Blowing Snow", "Blowing snow"), ("Sleet", "Sleet"),
    ("Freezing Rain", "Freezing rain"), ("Chance Freezing Drizzle", "Chance freezing drizzle"),
    ("Patchy Freezing Drizzle", "Patchy freezing drizzle"), ("Wintry Mix", "Wintry mix"),
    ("Patchy Fog", "Patchy fog"), ("Areas Of Fog", "Areas of fog"), ("Haze", "Haze"),
    ("Sunny and Breezy", "Sunny, breezy"), ("Mostly Sunny and Windy", "Mostly sunny, windy"),
    # "A then B": dry then wet → "… late", wet then dry → "… early", a slight chance doesn't count, wet → wet "A → b"
    ("Mostly Cloudy then Chance Showers And Thunderstorms", "Chance of storms late"),
    ("Partly Sunny then Showers And Thunderstorms Likely", "Storms likely late"),
    ("Mostly Cloudy then Slight Chance Rain Showers", "Mostly cloudy"),
    ("Chance Showers And Thunderstorms then Mostly Sunny", "Chance of storms early"),
    ("Patchy Fog then Mostly Sunny", "Patchy fog early"),
    ("Slight Chance Rain Showers then Mostly Sunny", "Mostly sunny"),
    ("Rain then Snow", "Rain → snow"), ("Sleet then Snow", "Sleet → snow"),
    ("Chance Rain then Rain Likely", "Rain likely"), ("Rain And Snow then Snow Likely", "Rain/snow → snow"),
    ("Snow Likely then Blowing Snow", "Snow → blowing snow"),
    ("Chance Rain Showers then Showers And Thunderstorms Likely", "Showers → storms"),
    ("Mostly Sunny then Partly Cloudy", "Mostly sunny"),
]


@pytest.mark.parametrize("short,text", TEXT_TABLE)
def test_short_text(short, text):
    assert fc.short_text(short, "cloudy") == text
    assert len(text) <= fc.MAX_TEXT


def test_short_text_fallback_is_the_condition_name():
    assert fc.short_text("Volcanic Ash Clouds Possible Over The Entire Region", "haze-smoke") == "Haze"
    assert fc.short_text("", "thunderstorm") == "Storms"
    assert fc.short_text(None, "snow") == "Snow"
