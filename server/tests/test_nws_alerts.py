"""nws_alerts: recorded responses (none for Savoy on 2026-10-24; New Mexico's real alerts), a crafted winter storm."""
import json
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from app import shapes
from app.config import AppConfig
from app.providers import base, nws_alerts, nws_common
from app.providers.base import RequestContext
from app.providers.nws_common import NwsError
from app.readings import Status

RECORDED = Path(__file__).resolve().parent / "data" / "recorded"
TZ = ZoneInfo("America/Chicago")
NOW = datetime(2026, 10, 24, 16, 39, tzinfo=UTC)
WINTER_NOW = datetime(2027, 1, 19, 15, 30, tzinfo=TZ)
ALERTS = "https://api.weather.gov/alerts/active"


def recorded(name: str) -> dict:
    return json.loads((RECORDED / name).read_text(encoding="utf-8"))


@pytest.fixture
def app(tmp_path, monkeypatch) -> AppConfig:
    monkeypatch.setenv("INKPLATE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(nws_common, "_points_mem", {})
    monkeypatch.setattr(nws_common, "RETRY_DELAY_S", 0)
    return AppConfig.model_validate({
        "location": {"name": "Savoy, IL", "lat": 40.0617, "lon": -88.2517, "tz": "America/Chicago"},
        "providers": {"nws_alerts": {"type": "nws_alerts", "ttl": "5m"}},
    })


async def fetch(app, now):
    p = base.build("nws_alerts", app.providers["nws_alerts"], app)
    return await p.fetch(RequestContext(now=now))


@respx.mock
async def test_no_alerts_is_an_ok_empty_list(app):
    route = respx.get(ALERTS).respond(json=recorded("nws_alerts_point.json"))
    r = await fetch(app, NOW)
    assert route.calls.last.request.url.params["point"] == "40.0617,-88.2517"
    a = r["alerts.active"]
    assert (a.status, a.value, a.observed_at, a.source) == (Status.ok, [], NOW, "nws_alerts")


@respx.mock
async def test_winter_storm(app):
    respx.get(ALERTS).respond(json=recorded("nws_alerts_winter.json"))
    r = await fetch(app, WINTER_NOW)
    alerts = shapes.validate("alerts.active", r["alerts.active"].value)
    # The advisory the warning replaced, the cancellation and the test message are gone.
    assert [(a.event, a.severity, a.impact) for a in alerts] == [
        ("Winter Storm Warning", "Severe", "3–5 in snow"),
        ("Cold Weather Advisory", "Moderate", "wind chill −15°"),
    ]
    w = alerts[0]
    assert w.ends == datetime(2027, 1, 20, 9, tzinfo=TZ)
    assert w.onset == datetime(2027, 1, 19, 12, tzinfo=TZ)
    assert w.headline.startswith("Winter Storm Warning issued January 19")
    assert w.url.endswith("winter-storm-warning")


def test_real_alerts_new_mexico():
    """Real active alerts recorded 2026-10-24 (area=NM): one per event, most severe first, ended ones dropped."""
    doc = recorded("nws_alerts_nm.json")
    alerts = nws_alerts.parse(doc, NOW)
    events = [a.event for a in alerts]
    assert events == ["Flood Warning", "Flood Watch", "Special Weather Statement", "Flood Advisory"]
    assert [a.severity for a in alerts] == ["Severe", "Severe", "Moderate", "Minor"]
    sws = alerts[2]
    assert sws.impact == "gusts to 50 mph"
    assert sws.ends == datetime(2026, 10, 24, 11, tzinfo=sws.ends.tzinfo)  # no `ends`: `expires`
    flood = alerts[0]  # several Flood Warnings: the latest-ending one is kept
    raw_ends = [nws_alerts._ends(f["properties"]) for f in doc["features"]
                if f["properties"]["event"] == "Flood Warning"]
    assert flood.ends == max(raw_ends)
    later = nws_alerts.parse(doc, datetime(2026, 10, 24, 18, 0, tzinfo=UTC))  # SWS ended at 17:00 UTC
    assert "Special Weather Statement" not in [a.event for a in later]


@respx.mock
async def test_point_query_failure_falls_back_to_zone_and_county(app):
    respx.get("https://api.weather.gov/points/40.0617,-88.2517").respond(json=recorded("nws_points.json"))
    async with nws_common.client(app) as http:
        await nws_common.get_point(http, app, NOW)  # what nws_forecast leaves in the cache
    route = respx.get(ALERTS).mock(side_effect=[httpx.Response(500), httpx.Response(500),
                                                httpx.Response(200, json=recorded("nws_alerts_zone.json"))])
    r = await fetch(app, NOW)
    assert route.calls.last.request.url.params["zone"] == "ILZ045,ILC019"
    assert r["alerts.active"].value == []


@pytest.mark.parametrize("responses,note", [
    ([httpx.Response(429)], "nws_rate_limited"),
    ([httpx.Response(502), httpx.Response(503)], "nws_server_error"),
])
@respx.mock
async def test_failure_without_cached_point_raises(app, responses, note):
    respx.get(ALERTS).mock(side_effect=responses)
    with pytest.raises(NwsError) as e:
        await fetch(app, NOW)
    assert e.value.note == note


IMPACTS = [
    ("Winter Storm Warning", "Total snow accumulations of 3 to 5 inches and ice accumulations of a light glaze.",
     "3–5 in snow"),
    ("Winter Weather Advisory", "* WHAT...Snow expected. Total snow accumulations up to 2 inches.", "up to 2 in snow"),
    ("Winter Weather Advisory", "Additional snow accumulations of one to two inches.", "1–2 in snow"),
    ("Winter Weather Advisory", "Snow accumulations of a dusting to one inch.", "up to 1 in snow"),
    ("Winter Storm Watch", "Heavy snow possible. Total snow accumulations of 6 inches or more.", "6 in snow"),
    ("Blizzard Warning", "Blizzard conditions with 8 to 12 inches of snow and gusts as high as 55 mph.",
     "8–12 in snow"),
    ("Ice Storm Warning", "Significant icing. Total snow accumulations of up to one inch and ice accumulations of "
     "one quarter to one half inch.", "ice 0.25–0.5 in"),
    ("Ice Storm Warning", "Ice accumulations of 0.25 to 0.5 inch.", "ice 0.25–0.5 in"),
    ("Freezing Rain Advisory", "Ice accumulations of around a tenth of an inch.", "ice 0.1 in"),
    ("Freezing Rain Advisory", "Ice accumulations between one and two tenths of an inch.", "ice 0.1–0.2 in"),
    ("Winter Weather Advisory", "Mixed precipitation. Ice accumulations of a light glaze.", None),
    ("Wind Advisory", "* WHAT...West winds 25 to 35 mph with gusts up to 50 mph.", "gusts to 50 mph"),
    ("High Wind Warning", "Southwest winds 35 to 45 mph with gusts between 60 and 70 mph expected.",
     "gusts to 70 mph"),
    ("Severe Thunderstorm Warning", "At 405 PM CDT, a severe thunderstorm was located near Tolono. HAZARD...60 mph "
     "wind gusts and quarter size hail.", "hail 1 in"),
    ("Severe Thunderstorm Warning", "HAZARD...70 mph wind gusts.", "gusts to 70 mph"),
    ("Extreme Cold Warning", "Dangerously cold wind chills as low as 35 below zero expected.", "wind chill −35°"),
    ("Freeze Warning", "* WHAT...Sub-freezing temperatures as low as 26 expected.", "low 26°"),
    ("Frost Advisory", "Temperatures as low as 33 will result in frost formation.", "low 33°"),
    ("Heat Advisory", "Heat index values up to 108 expected.", "heat index 108°"),
    ("Excessive Heat Warning", "Dangerously hot conditions with heat index values 110 to 115.", "heat index 115°"),
    ("Flood Warning", "Flooding caused by excessive rainfall is expected.", None),
    ("Tornado Warning", "A severe thunderstorm capable of producing a tornado was located near Savoy.", None),
]


@pytest.mark.parametrize("event,description,expected", IMPACTS)
def test_impact(event, description, expected):
    assert nws_alerts.impact(event, description) == expected


def test_sorting_and_dedup():
    def feat(i, event, severity, onset, ends, refs=()):
        return {"properties": {"id": f"a{i}", "event": event, "severity": severity, "onset": onset, "ends": ends,
                               "status": "Actual", "messageType": "Alert", "description": "",
                               "references": [{"identifier": r} for r in refs]}}

    doc = {"features": [
        feat(1, "Flood Advisory", "Minor", "2027-01-19T10:00:00-06:00", "2027-01-20T10:00:00-06:00"),
        feat(2, "Winter Storm Watch", "Severe", "2027-01-20T10:00:00-06:00", "2027-01-21T10:00:00-06:00"),
        feat(3, "Winter Storm Warning", "Severe", "2027-01-19T18:00:00-06:00", "2027-01-20T09:00:00-06:00"),
        feat(4, "Wind Advisory", "Moderate", "2027-01-19T12:00:00-06:00", "2027-01-19T18:00:00-06:00"),
        feat(5, "Wind Advisory", "Moderate", "2027-01-19T12:00:00-06:00", "2027-01-19T21:00:00-06:00"),
        feat(6, "Tornado Warning", "Extreme", "2027-01-19T15:00:00-06:00", "2027-01-19T15:20:00-06:00"),  # over
        feat(7, "Special Weather Statement", "Moderate", "2027-01-19T11:00:00-06:00", None),
    ]}
    alerts = nws_alerts.parse(doc, WINTER_NOW)
    assert [a.event for a in alerts] == ["Winter Storm Warning", "Winter Storm Watch", "Wind Advisory",
                                         "Special Weather Statement", "Flood Advisory"]
    assert alerts[2].ends == datetime(2027, 1, 19, 21, tzinfo=TZ)  # the later-ending duplicate
    assert alerts[3].ends is None
