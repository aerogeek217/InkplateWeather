"""acis provider against RCC-ACIS responses recorded for CHAMPAIGN 3S (USC00118740) on 2026-10-24 (offline)."""
import copy
import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from conftest import TESTS_DIR

from app.config import AppConfig, LocationCfg, ProviderCfg
from app.providers import acis, base
from app.providers.base import RequestContext
from app.readings import Status

TZ = ZoneInfo("America/Chicago")
RECORDED = TESTS_DIR / "data" / "recorded"
FALL = json.loads((RECORDED / "acis_2026-10-24.json").read_text(encoding="utf-8"))
WINTER = json.loads((RECORDED / "acis_2026-01-27.json").read_text(encoding="utf-8"))
APP = AppConfig(location=LocationCfg(name="Savoy, IL", lat=40.0617, lon=-88.2517, tz="America/Chicago"))


def provider(**opts):
    return base.build("acis", ProviderCfg(type="acis", ttl="12h", **({"station": "USC00118740"} | opts)), APP)


async def fetch(data, now):
    with respx.mock(assert_all_called=True) as router:
        route = router.post(acis.URL).mock(return_value=httpx.Response(200, json=data))
        out = await provider().fetch(RequestContext(now=now))
    return out, json.loads(route.calls.last.request.content)


async def test_fall_recorded():
    now = datetime(2026, 10, 24, 11, 43, tzinfo=TZ)
    out, body = await fetch(FALL, now)
    assert body["sid"] == "USC00118740"
    assert (body["sdate"], body["edate"]) == ("2026-07-01", "2026-10-24")
    assert {e["name"] for e in body["elems"]} == {"maxt", "mint", "snow", "snwd"}
    assert [e.get("normal") for e in body["elems"][:2]] == ["91", "91"]  # 1991–2020 normals
    assert (out["normals.high"].value, out["normals.low"].value) == (62, 41)
    assert out["normals.high"].unit == "°F" and out["normals.high"].observed_at == now
    assert out["snow.season"].value == 0.0 and out["snow.depth"].value == 0.0
    # Today's 8 AM report isn't in yet; yesterday's is (obs hour 7 LST = 8 AM CDT).
    assert out["snow.depth"].observed_at == datetime(2026, 10, 23, 8, 0, tzinfo=TZ)
    assert out["snow.season"].observed_at == datetime(2026, 10, 23, 8, 0, tzinfo=TZ)
    assert all(r.status is Status.ok and r.source == "acis" for r in out.values())


async def test_winter_recorded():
    now = datetime(2026, 1, 27, 12, 0, tzinfo=TZ)
    out, body = await fetch(WINTER, now)
    assert (body["sdate"], body["edate"]) == ("2025-07-01", "2026-01-27")
    assert (out["normals.high"].value, out["normals.low"].value) == (34, 17)
    assert out["snow.depth"].value == 5.0 and out["snow.depth"].unit == "in"
    assert out["snow.depth"].observed_at == datetime(2026, 1, 27, 8, 0, tzinfo=TZ)
    assert out["snow.season"].value == 24.7


def test_season_sum_counts_trace_as_zero_and_skips_missing():
    d = copy.deepcopy(WINTER)
    rows = d["data"]
    rows[-1][3] = ["T", 8]  # today: trace
    rows[-2][3] = ["M", -1]  # yesterday: missing (was 0.9)
    out = acis.parse(d, "acis", date(2026, 1, 27), TZ, datetime(2026, 1, 27, 12, tzinfo=TZ))
    assert out["snow.season"].value == round(24.7 - 0.9, 1)


def test_depth_missing_without_a_recent_report():
    d = copy.deepcopy(WINTER)
    for row in d["data"][-3:]:
        row[4] = ["M", -1]
    out = acis.parse(d, "acis", date(2026, 1, 27), TZ, datetime(2026, 1, 27, 12, tzinfo=TZ))
    assert out["snow.depth"].status is Status.missing and out["snow.depth"].note == "no_recent_report"
    d["data"][-3][4] = ["T", 8]  # a trace two days ago still counts
    out = acis.parse(d, "acis", date(2026, 1, 27), TZ, datetime(2026, 1, 27, 12, tzinfo=TZ))
    assert out["snow.depth"].value == 0.0
    assert out["snow.depth"].observed_at == datetime(2026, 1, 25, 8, 0, tzinfo=TZ)


def test_missing_normal_is_missing():
    d = copy.deepcopy(FALL)
    d["data"][-1][1] = "M"
    out = acis.parse(d, "acis", date(2026, 10, 24), TZ, datetime(2026, 10, 24, 12, tzinfo=TZ))
    assert out["normals.high"].status is Status.missing and out["normals.low"].value == 41


@pytest.mark.parametrize("raw,expected", [("T", 0.0), ("M", None), ("S", None), ("0.40A", 0.4), ("3", 3.0),
                                          (" 1.5 ", 1.5), (None, None), ("", None)])
def test_parse_value(raw, expected):
    assert acis.parse_value(raw) == expected


@pytest.mark.parametrize("today,start", [(date(2026, 10, 24), date(2026, 7, 1)), (date(2027, 1, 19), date(2026, 7, 1)),
                                         (date(2026, 6, 30), date(2025, 7, 1)), (date(2026, 7, 1), date(2026, 7, 1))])
def test_season_start(today, start):
    assert acis.season_start(today) == start


async def test_unknown_station_raises():
    with respx.mock as router:
        router.post(acis.URL).mock(return_value=httpx.Response(200, json={"error": "no data available"}))
        with pytest.raises(acis.AcisError):
            await provider(station="nope").fetch(RequestContext(now=datetime(2026, 10, 24, 12, tzinfo=TZ)))


async def test_server_error_raises():
    with respx.mock as router:
        router.post(acis.URL).mock(return_value=httpx.Response(503))
        with pytest.raises(httpx.HTTPStatusError):
            await provider().fetch(RequestContext(now=datetime(2026, 10, 24, 12, tzinfo=TZ)))


def test_station_is_required():
    with pytest.raises(ValueError):
        base.build("acis", ProviderCfg(type="acis"), APP)
    assert provider().ttl == timedelta(hours=12)
