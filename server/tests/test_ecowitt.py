import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from conftest import REPO_DIR, SERVER_DIR, TESTS_DIR

from app import fields
from app.config import AppConfig, ProviderCfg, load
from app.providers import base
from app.providers.base import RequestContext
from app.providers.ecowitt_local import EcowittLocalProvider, normalize_unit, parse_livedata, to_canonical
from app.readings import Status
from app.resolver import resolve

SAMPLE = SERVER_DIR / "fixtures" / "ecowitt_get_livedata_info.sample.json"
RECORDED = TESTS_DIR / "data" / "recorded" / "ecowitt_livedata_1.json"
NOW = datetime(2026, 10, 24, 19, 30, tzinfo=UTC)
HOST = "192.168.1.135"
URL = f"http://{HOST}/get_livedata_info"


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def provider(**opts) -> EcowittLocalProvider:
    return EcowittLocalProvider("ecowitt", ProviderCfg(type="ecowitt_local", host=HOST, **opts))


def ctx() -> RequestContext:
    return RequestContext(now=datetime.now(UTC))


# Every key the saved sample produces, with its canonical value and unit.
SAMPLE_EXPECTED = {
    "0x02": (59.5, "°F"),  # outdoor temperature
    "0x07": (72.0, "%"),  # outdoor humidity
    "3": (59.5, "°F"),  # feels like
    "0x03": (50.5, "°F"),  # dew point
    "0x0B": (0.0, "mph"),  # wind speed
    "0x0C": (0.0, "mph"),  # gust
    "0x19": (2.24, "mph"),  # day max wind
    "0x15": (26.91, "W/m2"),  # solar radiation
    "0x17": (0.0, None),  # UV index
    "0x0A": (47.0, "deg"),  # wind direction
    "0x0D": (0.0, "in"),  # rain event
    "0x0E": (0.0, "in/h"),  # rain rate
    "0x10": (0.0, "in"),  # rain day
    "0x11": (0.0, "in"),  # rain week
    "0x12": (1.22, "in"),  # rain month
    "0x13": (27.15, "in"),  # rain year
    "wh25.intemp": (71.6, "°F"),
    "wh25.inhumi": (52.0, "%"),
    "wh25.abs": (29.62, "inHg"),
    "wh25.rel": (29.62, "inHg"),
    "ch1.temp": (63.7, "°F"),  # garage
    "ch1.humidity": (52.0, "%"),
}


def test_parses_saved_sample_exactly():
    out = parse_livedata(load_json(SAMPLE), NOW, source="ecowitt")
    assert {k: (r.value, r.unit) for k, r in out.items()} == SAMPLE_EXPECTED
    for r in out.values():
        assert r.status is Status.ok and r.note is None
        assert r.observed_at == NOW and r.source == "ecowitt"


def test_lightning_block_is_ignored():
    out = parse_livedata(load_json(SAMPLE), NOW)
    assert not any("lightning" in k or k in ("distance", "count", "timestamp") for k in out)
    only_lightning = {"lightning": [{"distance": "8.7 mi", "timestamp": "10/24/2026 00:45:54", "count": "1"}]}
    assert parse_livedata(only_lightning, NOW) == {}


def test_parses_recorded_live_response():
    out = parse_livedata(load_json(RECORDED), NOW)
    assert set(out) == set(SAMPLE_EXPECTED)
    assert all(r.status is Status.ok for r in out.values())
    assert (out["0x02"].value, out["0x02"].unit) == (65.8, "°F")
    assert (out["0x0A"].value, out["0x0A"].unit) == (180.0, "deg")
    assert (out["0x15"].value, out["0x15"].unit) == (72.14, "W/m2")


METRIC = {
    "common_list": [
        {"id": "0x02", "val": "15.0", "unit": "C"},
        {"id": "0x07", "val": "72%"},
        {"id": "3", "val": "-10.0", "unit": "C"},
        {"id": "0x0B", "val": "1.0 m/s"},
        {"id": "0x0C", "val": "16.09 km/h"},
        {"id": "0x19", "val": "10.0 knots"},
        {"id": "0x15", "val": "12.67 Klux"},
        {"id": "0x17", "val": "3"},
        {"id": "0x0A", "val": "270"},
    ],
    "rain": [{"id": "0x0D", "val": "25.4 mm"}, {"id": "0x0E", "val": "2.5 mm/Hr"}, {"id": "0x10", "val": "0.2 mm"}],
    "wh25": [{"intemp": "21.5", "unit": "C", "inhumi": "40%", "abs": "1013.2 hPa", "rel": "760.0 mmHg"}],
    "ch_aisle": [{"channel": "1", "name": "Garage", "temp": "0.0", "unit": "C", "humidity": "50%"},
                 {"channel": "3", "temp": "68.0", "unit": "℉", "humidity": "45%"}],
}


def test_metric_gateway_is_converted():
    out = parse_livedata(METRIC, NOW)
    got = {k: (r.value, r.unit) for k, r in out.items()}
    assert got == {
        "0x02": (59.0, "°F"),
        "0x07": (72.0, "%"),
        "3": (14.0, "°F"),
        "0x0B": (2.24, "mph"),
        "0x0C": (10.0, "mph"),
        "0x19": (11.51, "mph"),
        "0x15": (100.0, "W/m2"),
        "0x17": (3.0, None),
        "0x0A": (270.0, "deg"),
        "0x0D": (1.0, "in"),
        "0x0E": (0.098, "in/h"),
        "0x10": (0.008, "in"),
        "wh25.intemp": (70.7, "°F"),
        "wh25.inhumi": (40.0, "%"),
        "wh25.abs": (29.92, "inHg"),
        "wh25.rel": (29.92, "inHg"),
        "ch1.temp": (32.0, "°F"),
        "ch1.humidity": (50.0, "%"),
        "ch3.temp": (68.0, "°F"),
        "ch3.humidity": (45.0, "%"),
    }


@pytest.mark.parametrize("value,unit,expected", [
    (10, "°C", (50.0, "°F")), (50, "F", (50, "°F")), (5000, "K", None),
    (1013.25, "mbar", (29.92, "inHg")), (101.325, "kPa", (29.92, "inHg")),
    (10, "kn", (11.51, "mph")), (3.6, "km/h", (2.24, "mph")),
    (10, "mm/h", (0.394, "in/h")), (1, "in/Hr", (1, "in/h")), (500, "W/m²", (500, "W/m2")),
    (5, "µg/m³", None), (5, None, None),
])
def test_to_canonical(value, unit, expected):
    assert to_canonical(value, unit) == expected


def test_normalize_unit():
    assert normalize_unit(" °C ") == "c"
    assert normalize_unit("in/Hr") == "in/hr"
    assert normalize_unit("") is None and normalize_unit(None) is None


def test_missing_blocks_and_keys_are_tolerated():
    assert parse_livedata({}, NOW) == {}
    data = {"common_list": [{"id": "0x02", "val": "60.1", "unit": "F"}, {"val": "1"}, {"id": "0x07"}],
            "rain": "not a list", "wh25": {"intemp": "70.0", "unit": "F"},
            "ch_aisle": [{"name": "no channel", "temp": "1"}, {"channel": "2", "humidity": "40%"}, "junk"]}
    out = parse_livedata(data, NOW)
    assert {k: (r.value, r.unit) for k, r in out.items()} == {
        "0x02": (60.1, "°F"), "wh25.intemp": (70.0, "°F"), "ch2.humidity": (40.0, "%")}


def test_garbage_values_become_missing_readings():
    data = {"common_list": [
        {"id": "0x02", "val": "--", "unit": "F"},
        {"id": "0x07", "val": "--%"},
        {"id": "0x0B", "val": "---.- mph"},
        {"id": "0x0C", "val": "fast"},
        {"id": "0x15", "val": "12 furlongs"},
        {"id": "0x17", "val": ""},
        {"id": "0x0A", "val": None},
        {"id": "0x03", "val": "NaN", "unit": "F"},
        {"id": "3", "val": "61.0"},  # temperature without a unit key: assume °F, say so
        {"id": "0x19", "val": 3.5},  # a bare number: no unit to convert
    ]}
    out = parse_livedata(data, NOW)
    notes = {k: (r.status, r.note) for k, r in out.items() if not r.usable}
    assert notes == {
        "0x02": (Status.missing, "no_value"),
        "0x07": (Status.missing, "no_value"),
        "0x0B": (Status.missing, "no_value"),
        "0x0C": (Status.missing, "bad_value"),
        "0x15": (Status.missing, "unknown_unit"),
        "0x17": (Status.missing, "no_value"),
        "0x0A": (Status.missing, "no_value"),
        "0x03": (Status.missing, "bad_value"),
    }
    assert all(out[k].observed_at is None for k in notes)  # no "since …" for values never seen
    assert (out["3"].value, out["3"].unit, out["3"].note) == (61.0, "°F", "unit_assumed")
    assert (out["0x19"].value, out["0x19"].unit) == (3.5, None)


@pytest.mark.parametrize("doc", [[], "text", 42])
def test_non_object_json_raises(doc):
    with pytest.raises(ValueError):
        parse_livedata(doc, NOW)


@respx.mock
async def test_fetch_ok_sets_request_time_and_source():
    respx.get(URL).mock(return_value=httpx.Response(200, text=SAMPLE.read_text(encoding="utf-8"),
                                                    headers={"Content-Type": "text/html"}))
    before = datetime.now(UTC)
    out = await provider().fetch(ctx())
    after = datetime.now(UTC)
    assert {k: (r.value, r.unit) for k, r in out.items()} == SAMPLE_EXPECTED
    r = out["0x02"]
    assert r.source == "ecowitt" and before <= r.observed_at <= after and r.observed_at.tzinfo is not None


@respx.mock
@pytest.mark.parametrize("response", [
    httpx.Response(500, text="oops"),
    httpx.Response(404),
    httpx.Response(200, text="<html>not json</html>"),
    httpx.Response(200, text="[1, 2]"),
])
async def test_fetch_bad_response_raises(response):
    respx.get(URL).mock(return_value=response)
    with pytest.raises((httpx.HTTPStatusError, ValueError)):
        await provider().fetch(ctx())


@respx.mock
@pytest.mark.parametrize("exc", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")])
async def test_fetch_network_failure_raises(exc):
    respx.get(URL).mock(side_effect=exc)
    with pytest.raises(httpx.HTTPError):
        await provider().fetch(ctx())


async def test_missing_host_raises():
    p = EcowittLocalProvider("ecowitt", ProviderCfg(type="ecowitt_local"))
    with pytest.raises(ValueError, match="host"):
        await p.fetch(ctx())


def test_options():
    p = provider(timeout="2s")
    assert p.timeout == 2.0 and p.url == URL
    assert provider().timeout == 4.0
    p = EcowittLocalProvider("e", ProviderCfg(type="ecowitt_local", host="http://gw.local:8080/"))
    assert p.url == "http://gw.local:8080/get_livedata_info"


@respx.mock
async def test_example_config_resolves_every_ecowitt_field():
    cfg: AppConfig = load(REPO_DIR / "config" / "config.example.yaml")
    eco_fields = {f: [s for s in srcs if s.from_ == "ecowitt"] for f, srcs in cfg.fields.items()}
    eco_fields = {f: srcs for f, srcs in eco_fields.items() if srcs}
    assert {"outdoor.temperature", "outdoor.feels_like", "wind.direction", "uv.index", "solar.radiation",
            "indoor.temperature", "garage.humidity", "pressure.sea_level", "rain.rate", "rain.year"} <= set(eco_fields)

    respx.get(URL).mock(return_value=httpx.Response(200, text=SAMPLE.read_text(encoding="utf-8")))
    p = base.build("ecowitt", cfg.providers["ecowitt"], cfg)
    assert isinstance(p, EcowittLocalProvider) and p.ttl == timedelta(seconds=30)
    readings = await p.fetch(ctx())
    resolved = resolve(eco_fields, {"ecowitt": readings}, datetime.now(UTC))

    bad = {f: r.tried for f, r in resolved.items() if r.reading.status is not Status.ok}
    assert not bad
    assert {f: r.reading.unit for f, r in resolved.items()} == {f: fields.FIELDS[f].unit for f in eco_fields}
    v = {f: r.reading.value for f, r in resolved.items()}
    assert v["outdoor.temperature"] == 59.5 and v["outdoor.feels_like"] == 59.5
    assert v["garage.temperature"] == 63.7 and v["pressure.sea_level"] == 29.62
    assert v["wind.direction"] == 47 and v["rain.month"] == 1.22 and v["uv.index"] == 0
