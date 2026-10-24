"""home_assistant provider against a synthesized /api/states response (shape per HA's REST API docs)."""
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from conftest import TESTS_DIR

from app import messages
from app.config import AppConfig, ProviderCfg
from app.providers import base
from app.providers.base import RequestContext
from app.providers.home_assistant import HomeAssistantConfigError, HomeAssistantProvider, parse_state, parse_states
from app.readings import Status
from app.resolver import resolve

STATES = TESTS_DIR / "data" / "recorded" / "ha_states_synthetic.json"
HA_URL = "http://ha.test:8123"
TOKEN = "test-token-not-real"
NOW = datetime(2026, 10, 24, 19, 30, tzinfo=UTC)


def states_text() -> str:
    return STATES.read_text(encoding="utf-8")


def provider(**opts) -> HomeAssistantProvider:
    return HomeAssistantProvider("ha", ProviderCfg(type="home_assistant", url=HA_URL + "/", token_env="TEST_HA_TOKEN",
                                                   **opts))


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setenv("TEST_HA_TOKEN", TOKEN)


def t(s: str) -> datetime:
    return datetime.fromisoformat(s)


def test_parse_states_values_units_and_times():
    out = parse_states(json.loads(states_text()), source="ha")
    got = {k: (r.value, r.unit, r.status, r.note) for k, r in out.items()}
    ok = Status.ok
    assert got == {
        "sensor.pms5003_aqi": (42.0, None, ok, None),
        "sensor.pms5003_pm2_5": (8.6, "µg/m³", ok, None),
        "sensor.pms5003_pm10": (None, None, Status.missing, "unavailable"),
        "sensor.porch_temperature": (54.5, "°F", ok, None),  # 12.5 °C
        "sensor.attic_temperature": (88.2, "°F", ok, None),
        "sensor.outdoor_humidity": (48.3, "%", ok, None),
        "sensor.barometer": (29.92, "inHg", ok, None),  # 1013.2 hPa
        "sensor.wind_speed": (10.0, "mph", ok, None),  # 16.09 km/h
        "sensor.wind_gust": (10.0, "mph", ok, None),  # 4.47 m/s
        "sensor.rain_today": (0.5, "in", ok, None),  # 12.7 mm
        "sensor.rain_rate": (0.1, "in/h", ok, None),  # 2.54 mm/h
        "sensor.yard_illuminance": (5300.0, "lx", ok, None),  # not converted
        "sensor.mystery": (None, None, Status.missing, "unknown"),
        "binary_sensor.garage_door": ("off", None, ok, None),
        "weather.forecast_home": ("partlycloudy", None, ok, None),
        "sun.sun": ("above_horizon", None, ok, None),
    }
    assert all(r.source == "ha" for r in out.values())
    # observed_at: last_reported (value unchanged but sensor still reporting), else last_updated
    assert out["sensor.pms5003_aqi"].observed_at == t("2026-10-24T19:28:43.120911+00:00")
    assert out["sensor.attic_temperature"].observed_at == t("2026-10-24T19:20:00+00:00")
    # unavailable: observed_at = when it stopped reporting
    assert out["sensor.pms5003_pm10"].observed_at == t("2026-10-24T18:12:00+00:00")


def test_parse_state_edge_cases():
    r = parse_state({"entity_id": "sensor.x", "state": "nan", "attributes": None, "last_updated": "garbage"})
    assert r.value == "nan" and r.observed_at is None
    r = parse_state({"entity_id": "sensor.x", "state": None})
    assert r.status is Status.missing and r.note == "no_value"
    r = parse_state({"entity_id": "sensor.x", "state": "-3.5", "attributes": {"unit_of_measurement": "°C"},
                     "last_updated": "2026-10-24T19:00:00Z"})
    assert (r.value, r.unit, r.observed_at) == (25.7, "°F", t("2026-10-24T19:00:00+00:00"))
    r = parse_state({"entity_id": "sensor.x", "state": "12", "last_updated": "2026-10-24T19:00:00"})
    assert r.value == 12.0 and r.observed_at is None  # naive timestamps are not trusted


def test_parse_states_skips_junk_and_rejects_non_list():
    assert set(parse_states([{"state": "1"}, "junk", {"entity_id": "a.b", "state": "1"}])) == {"a.b"}
    with pytest.raises(ValueError):
        parse_states({"message": "not a list"})


@respx.mock
async def test_fetch_sends_bearer_token(token):
    route = respx.get(f"{HA_URL}/api/states").mock(return_value=httpx.Response(200, text=states_text()))
    out = await provider().fetch(RequestContext(now=NOW))
    assert route.called
    assert route.calls.last.request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert out["sensor.pms5003_aqi"].value == 42.0


async def test_missing_token_env_raises_clear_error(monkeypatch):
    monkeypatch.delenv("TEST_HA_TOKEN", raising=False)
    with pytest.raises(HomeAssistantConfigError, match="TEST_HA_TOKEN is not set"):
        await provider().fetch(RequestContext(now=NOW))


async def test_missing_url_raises(token):
    p = HomeAssistantProvider("ha", ProviderCfg(type="home_assistant", token_env="TEST_HA_TOKEN"))
    with pytest.raises(HomeAssistantConfigError, match="url"):
        await p.fetch(RequestContext(now=NOW))


@respx.mock
@pytest.mark.parametrize("response,exc", [
    (httpx.Response(401, text="401: Unauthorized"), httpx.HTTPStatusError),
    (httpx.Response(502), httpx.HTTPStatusError),
    (httpx.Response(200, text="not json"), ValueError),
    (httpx.Response(200, json={"message": "API running."}), ValueError),
])
async def test_bad_responses_raise(token, response, exc):
    respx.get(f"{HA_URL}/api/states").mock(return_value=response)
    with pytest.raises(exc):
        await provider().fetch(RequestContext(now=NOW))


@respx.mock
async def test_network_failure_raises(token):
    respx.get(f"{HA_URL}/api/states").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(httpx.ConnectError):
        await provider().fetch(RequestContext(now=NOW))


def test_options_and_defaults():
    p = HomeAssistantProvider("ha", ProviderCfg(type="home_assistant", url=HA_URL, ttl="60s", timeout="3s"))
    assert p.token_env == "HA_TOKEN" and p.timeout == 3.0 and p.ttl == timedelta(seconds=60)
    assert provider().url == HA_URL


@respx.mock
async def test_resolves_field_via_key_with_fallback(token):
    """`{from: ha, key: sensor.xyz}` is how a field names an entity; unavailable falls back to the next source."""
    cfg = AppConfig.model_validate({
        "location": {"name": "x", "lat": 0, "lon": 0},
        "providers": {"ha": {"type": "home_assistant", "url": HA_URL, "token_env": "TEST_HA_TOKEN"},
                      "om": {"type": "fixture"}},
        "fields": {
            "air.aqi": [{"from": "ha", "key": "sensor.pms5003_aqi", "max_age": "20m"}, {"from": "om"}],
            "garage.temperature": [{"from": "ha", "key": "sensor.pms5003_pm10"}],
        },
    })
    respx.get(f"{HA_URL}/api/states").mock(return_value=httpx.Response(200, text=states_text()))
    p = base.build("ha", cfg.providers["ha"], cfg)
    readings = await p.fetch(RequestContext(now=NOW))
    out = resolve(cfg.fields, {"ha": readings, "om": {}}, NOW)
    assert out["air.aqi"].reading.value == 42.0 and out["air.aqi"].reading.source == "ha"
    assert out["air.aqi"].reading.status is Status.ok
    assert out["garage.temperature"].reading.status is Status.missing
    assert messages.explain(out["garage.temperature"], "America/Chicago") == (
        "Garage sensor hasn't reported since 1:12 PM.")
