"""NWS shared helpers: HTTP behavior (User-Agent, retry, errors), units, time parsing, the /points cache."""
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from app.config import AppConfig
from app.providers import nws_common as nc

RECORDED = Path(__file__).resolve().parent / "data" / "recorded"
NOW = datetime(2026, 10, 24, 16, 38, 10, tzinfo=UTC)
UA = "(InkplateWeather, https://github.com/aerogeek217/InkplateWeather)"


def recorded(name: str) -> dict:
    return json.loads((RECORDED / name).read_text(encoding="utf-8"))


@pytest.fixture
def app(tmp_path, monkeypatch) -> AppConfig:
    monkeypatch.setenv("INKPLATE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("INKPLATE_NWS_USER_AGENT", raising=False)
    monkeypatch.setattr(nc, "_points_mem", {})
    monkeypatch.setattr(nc, "RETRY_DELAY_S", 0)
    return AppConfig.model_validate({"location": {"name": "Savoy, IL", "lat": 40.0617, "lon": -88.2517}})


# ------------------------------------------------------------------ HTTP

@respx.mock
async def test_headers_and_json(app):
    route = respx.get("https://api.weather.gov/x").respond(json={"ok": 1})
    async with nc.client(app) as http:
        assert await nc.get_json(http, "/x") == {"ok": 1}
    req = route.calls.last.request
    assert req.headers["User-Agent"] == UA
    assert req.headers["Accept"] == "application/geo+json"


@respx.mock
async def test_user_agent_env_override(app, monkeypatch):
    monkeypatch.setenv("INKPLATE_NWS_USER_AGENT", "(Test, test@example.com)")
    route = respx.get("https://api.weather.gov/x").respond(json={})
    async with nc.client(app) as http:
        await nc.get_json(http, "/x")
    assert route.calls.last.request.headers["User-Agent"] == "(Test, test@example.com)"


@respx.mock
async def test_retries_once_on_5xx(app):
    route = respx.get("https://api.weather.gov/x").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json={"ok": 2})])
    async with nc.client(app) as http:
        assert await nc.get_json(http, "/x") == {"ok": 2}
    assert route.call_count == 2


@pytest.mark.parametrize("responses,note,calls", [
    ([httpx.Response(500), httpx.Response(502)], "nws_server_error", 2),
    ([httpx.Response(429)], "nws_rate_limited", 1),
    ([httpx.Response(404)], "nws_http_404", 1),
    ([httpx.Response(200, text="<html>")], "nws_bad_response", 1),
    ([httpx.ReadTimeout("slow")], "nws_timeout", 1),
    ([httpx.ConnectError("down")], "nws_unreachable", 1),
])
@respx.mock
async def test_errors(app, responses, note, calls):
    route = respx.get("https://api.weather.gov/x").mock(side_effect=responses)
    async with nc.client(app) as http:
        with pytest.raises(nc.NwsError) as e:
            await nc.get_json(http, "/x")
    assert e.value.note == note
    assert route.call_count == calls


# ------------------------------------------------------------------ units and parsing

def test_units():
    assert nc.to_f({"unitCode": "wmoUnit:degC", "value": 18}) == pytest.approx(64.4)
    assert nc.to_f({"unitCode": "wmoUnit:degF", "value": 50}) == 50
    assert nc.to_f({"unitCode": "wmoUnit:degC", "value": None}) is None
    assert nc.to_mph({"unitCode": "wmoUnit:km_h-1", "value": 9.252}) == pytest.approx(5.749, abs=1e-3)
    assert nc.to_mph({"unitCode": "wmoUnit:m_s-1", "value": 10}) == pytest.approx(22.37, abs=1e-2)
    assert nc.to_inhg({"unitCode": "wmoUnit:Pa", "value": 103000}) == pytest.approx(30.416, abs=1e-3)
    assert nc.mm_to_in(25.4) == 1
    assert nc.plain({"unitCode": "wmoUnit:percent", "value": 52.01}) == 52.01
    assert nc.plain(None) is None
    assert nc.rnd(-0.001, 2) == 0.0 and str(nc.rnd(-0.001, 2)) == "0.0"


def test_wind_text():
    assert nc.parse_wind_speed("10 to 15 mph") == 15
    assert nc.parse_wind_speed("6 mph") == 6
    assert nc.parse_wind_speed("Calm") == 0
    assert nc.parse_wind_speed("") is None
    assert nc.compass_deg("NW") == 315
    assert nc.compass_deg("SSE") == 157.5
    assert nc.compass_deg("VRB") is None


def test_intervals():
    s, e = nc.parse_interval("2026-10-24T18:00:00+00:00/PT6H")
    assert s == datetime(2026, 10, 24, 18, tzinfo=UTC) and e - s == timedelta(hours=6)
    s, e = nc.parse_interval("2026-10-24T10:00:00+00:00/P6DT20H")
    assert e - s == timedelta(days=6, hours=20)
    assert nc.parse_duration("P1D") == timedelta(days=1)
    with pytest.raises(ValueError):
        nc.parse_interval("garbage")


def test_point_key():
    assert nc.point_key(40.0617, -88.2517) == "40.0617,-88.2517"
    assert nc.point_key(40.06, -88.25001) == "40.06,-88.25"


# ------------------------------------------------------------------ /points cache

@respx.mock
async def test_points_cached_in_memory_and_on_disk(app):
    route = respx.get("https://api.weather.gov/points/40.0617,-88.2517").respond(json=recorded("nws_points.json"))
    async with nc.client(app) as http:
        pt = await nc.get_point(http, app, NOW)
        assert (pt.grid_id, pt.grid_x, pt.grid_y) == ("ILX", 95, 70)
        assert pt.forecast_zone == "ILZ045" and pt.county == "ILC019"
        assert pt.forecast_grid_data == "https://api.weather.gov/gridpoints/ILX/95,70"
        await nc.get_point(http, app, NOW)
        assert route.call_count == 1
        nc._points_mem.clear()  # a restart: the file still answers
        assert await nc.get_point(http, app, NOW + timedelta(days=1)) == pt
        assert route.call_count == 1
    saved = json.loads((nc.cache_dir(app) / nc.POINTS_FILE).read_text(encoding="utf-8"))
    assert saved["40.0617,-88.2517"]["grid_id"] == "ILX"
    assert nc.cached_point(app) == pt


@respx.mock
async def test_points_refresh_weekly_and_keep_old_copy_on_failure(app):
    route = respx.get("https://api.weather.gov/points/40.0617,-88.2517").mock(side_effect=[
        httpx.Response(200, json=recorded("nws_points.json")), httpx.Response(500), httpx.Response(500)])
    async with nc.client(app) as http:
        pt = await nc.get_point(http, app, NOW)
        later = NOW + nc.POINTS_MAX_AGE + timedelta(hours=1)
        assert await nc.get_point(http, app, later) == pt  # refresh failed (twice), old copy used
    assert route.call_count == 3


@respx.mock
async def test_points_failure_without_cache_raises(app):
    respx.get("https://api.weather.gov/points/40.0617,-88.2517").respond(429)
    async with nc.client(app) as http:
        with pytest.raises(nc.NwsError):
            await nc.get_point(http, app, NOW)


@respx.mock
async def test_forget_point(app):
    respx.get("https://api.weather.gov/points/40.0617,-88.2517").respond(json=recorded("nws_points.json"))
    async with nc.client(app) as http:
        await nc.get_point(http, app, NOW)
    nc.forget_point(app)
    assert nc.cached_point(app) is None
