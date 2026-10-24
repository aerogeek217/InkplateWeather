"""nws_observations against recorded KCMI observations (2026-10-24 16:38Z) and crafted edge cases."""
import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from app import shapes
from app.config import AppConfig, ProviderCfg
from app.providers import base, nws_common, nws_observations
from app.providers.base import RequestContext
from app.providers.nws_common import NwsError, parse_time
from app.readings import Status

RECORDED = Path(__file__).resolve().parent / "data" / "recorded"
NOW = datetime(2026, 10, 24, 16, 38, 10, tzinfo=UTC)
URL = "https://api.weather.gov/stations/KCMI/observations"


def recorded(name: str) -> dict:
    return json.loads((RECORDED / name).read_text(encoding="utf-8"))


@pytest.fixture
def app(tmp_path, monkeypatch) -> AppConfig:
    monkeypatch.setenv("INKPLATE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(nws_common, "RETRY_DELAY_S", 0)
    return AppConfig.model_validate({
        "location": {"name": "Savoy, IL", "lat": 40.0617, "lon": -88.2517},
        "providers": {"nws_obs": {"type": "nws_observations", "ttl": "10m"}},
    })


def provider(app: AppConfig):
    return base.build("nws_obs", app.providers["nws_obs"], app)


def obs_list(doc: dict):
    feats = [(parse_time(f["properties"]["timestamp"]), f["properties"]) for f in doc["features"]]
    return sorted(feats, key=lambda o: o[0], reverse=True)


@respx.mock
async def test_recorded_kcmi(app):
    route = respx.get(URL).respond(json=recorded("nws_obs_history.json"))
    r = await provider(app).fetch(RequestContext(now=NOW))
    assert route.call_count == 1
    start = parse_time(route.calls.last.request.url.params["start"])
    assert start == (NOW - timedelta(hours=26)).replace(microsecond=0)

    t_5min = datetime(2026, 10, 24, 16, 5, tzinfo=UTC)
    t_metar = datetime(2026, 10, 24, 15, 53, tzinfo=UTC)
    expect = {  # newest 5-minute report (whole °C) at 16:05; sea-level pressure only in the 15:53 METAR
        "outdoor.temperature": (64.4, t_5min), "outdoor.feels_like": (64.4, t_5min),
        "outdoor.humidity": (52.0, t_5min), "outdoor.dew_point": (46.4, t_5min),
        "wind.speed": (5.7, t_5min), "wind.gust": (5.7, t_5min), "wind.direction": (160.0, t_5min),
        "pressure.sea_level": (30.42, t_metar), "pressure.trend_3h": (0.01, t_metar),
        "conditions.current": ("sunny", t_5min),
    }
    for key, (value, t) in expect.items():
        assert r[key].status is Status.ok, key
        assert (r[key].value, r[key].observed_at) == (value, t), key
        assert r[key].source == "nws_obs"
    assert r["outdoor.temperature"].unit == "°F" and r["pressure.sea_level"].unit == "inHg"

    hist = shapes.validate("outdoor.temperature_history", r["outdoor.temperature_history"].value)
    times = [p.t for p in hist]
    assert times == sorted(times)
    assert times[-1] == t_5min and times[0] >= NOW - timedelta(hours=24)
    assert all(b - a >= timedelta(minutes=19) for a, b in zip(times, times[1:], strict=False))
    assert 50 <= len(hist) <= 75
    assert all(40 < p.v < 90 for p in hist)


@respx.mock
async def test_later_fetches_only_ask_for_recent_reports(app):
    doc = recorded("nws_obs_history.json")
    route = respx.get(URL).mock(side_effect=[httpx.Response(200, json=doc),
                                             httpx.Response(200, json={"features": doc["features"][:3]})])
    p = provider(app)
    await p.fetch(RequestContext(now=NOW))
    r = await p.fetch(RequestContext(now=NOW + timedelta(minutes=10)))
    start = parse_time(route.calls.last.request.url.params["start"])
    assert start == datetime(2026, 10, 24, 14, 5, tzinfo=UTC)  # newest report (16:05) minus 2 h
    assert len(r["outdoor.temperature_history"].value) > 50  # history kept from the first fetch


def test_latest_observation_alone():
    """The /observations/latest document (same report as the list's newest) parses on its own."""
    doc = recorded("nws_obs_latest.json")
    r = nws_observations.parse([(parse_time(doc["properties"]["timestamp"]), doc["properties"])], NOW, "x")
    assert r["outdoor.temperature"].value == 64.4
    assert r["pressure.sea_level"].value == 30.42  # barometricPressure when there's no sea-level value
    assert r["pressure.trend_3h"].status is Status.missing  # needs a report ~3 h earlier
    assert len(r["outdoor.temperature_history"].value) == 1


def _report(t: datetime, **values) -> tuple[datetime, dict]:
    base_props = {
        "timestamp": t.isoformat(), "textDescription": "Light Snow",
        "icon": "https://api.weather.gov/icons/land/night/snow?size=medium",
        "temperature": {"unitCode": "wmoUnit:degC", "value": -10.0},
        "dewpoint": {"unitCode": "wmoUnit:degC", "value": -14.0},
        "relativeHumidity": {"unitCode": "wmoUnit:percent", "value": 72.4},
        "windChill": {"unitCode": "wmoUnit:degC", "value": -18.5},
        "heatIndex": {"unitCode": "wmoUnit:degC", "value": None},
        "windSpeed": {"unitCode": "wmoUnit:km_h-1", "value": 29.6},
        "windGust": {"unitCode": "wmoUnit:km_h-1", "value": 50.0},
        "windDirection": {"unitCode": "wmoUnit:degree_(angle)", "value": 320},
        "seaLevelPressure": {"unitCode": "wmoUnit:Pa", "value": 101200},
        "barometricPressure": {"unitCode": "wmoUnit:Pa", "value": 101150},
    }
    p = copy.deepcopy(base_props)
    for k, v in values.items():
        if isinstance(p.get(k), dict):
            p[k]["value"] = v
        else:
            p[k] = v
    return t, p


def test_wind_chill_gust_and_trend():
    t = datetime(2027, 1, 20, 6, 53, tzinfo=UTC)
    obs = [_report(t), _report(t - timedelta(hours=1)), _report(t - timedelta(hours=3), seaLevelPressure=101500)]
    r = nws_observations.parse(obs, t + timedelta(minutes=10), "x")
    assert r["outdoor.temperature"].value == 14.0
    assert r["outdoor.feels_like"].value == -1.3  # wind chill
    assert r["wind.speed"].value == 18.4 and r["wind.gust"].value == 31.1 and r["wind.direction"].value == 320
    assert r["pressure.trend_3h"].value == pytest.approx(-0.09)
    assert r["conditions.current"].value == "snow"


def test_missing_values_become_missing_readings():
    t = datetime(2027, 1, 20, 6, 53, tzinfo=UTC)
    obs = [_report(t, temperature=None, dewpoint=None, relativeHumidity=None, windDirection=None, windGust=None,
                   seaLevelPressure=None, barometricPressure=None, icon=None, textDescription="")]
    r = nws_observations.parse(obs, t, "x")
    for key in ("outdoor.temperature", "outdoor.feels_like", "outdoor.humidity", "outdoor.dew_point",
                "pressure.sea_level", "pressure.trend_3h", "conditions.current", "outdoor.temperature_history"):
        assert r[key].status is Status.missing, key
        assert r[key].note == "no_observations"
    assert r["wind.gust"].value == r["wind.speed"].value  # no gust reported → peak = sustained
    assert (r["wind.direction"].status, r["wind.direction"].note) == (Status.missing, "wind_variable")
    calm = nws_observations.parse([_report(t, windSpeed=0, windDirection=None)], t, "x")
    assert calm["wind.direction"].note == "wind_calm"


def test_no_reports_at_all():
    r = nws_observations.parse([], NOW, "x")
    assert set(r) == set(nws_observations.KEYS)
    assert all(v.status is Status.missing for v in r.values())


@pytest.mark.parametrize("responses,note", [
    ([httpx.Response(503), httpx.Response(503)], "nws_server_error"),
    ([httpx.Response(429)], "nws_rate_limited"),
])
@respx.mock
async def test_http_failure_raises(app, responses, note):
    respx.get(URL).mock(side_effect=responses)
    with pytest.raises(NwsError) as e:
        await provider(app).fetch(RequestContext(now=NOW))
    assert e.value.note == note


@respx.mock
async def test_station_option_overrides_config(app):
    route = respx.get("https://api.weather.gov/stations/KTIP/observations").respond(json={"features": []})
    p = nws_observations.NwsObservationsProvider("x", ProviderCfg(type="nws_observations", station="ktip"), app)
    r = await p.fetch(RequestContext(now=NOW))
    assert route.called and r["outdoor.temperature"].status is Status.missing
