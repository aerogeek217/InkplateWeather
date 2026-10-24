"""Station history store and the `history` provider (coverage rules, pressure trend, persistence)."""
import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import collector, shapes
from app.config import AppConfig, ProviderCfg, SourceCfg
from app.history import HistoryStore, get_store
from app.providers.base import RequestContext
from app.providers.history import HistoryProvider
from app.readings import Reading, Status
from app.resolver import Resolved

TZ = ZoneInfo("America/Chicago")
NOW = datetime(2026, 10, 24, 14, 30, tzinfo=TZ)


def fill(store, field, hours, step_min=10, end=NOW, value=lambda i: 50 + i * 0.01, skip=()):
    n = int(hours * 60 / step_min)
    for i in range(n + 1):
        t = end - timedelta(minutes=step_min * (n - i))
        if not any(a <= t <= b for a, b in skip):
            store.add(field, t, value(i))


def provider(store):
    return HistoryProvider("history", ProviderCfg(type="history"), None, store=store)


def resolved(field, value, t, status=Status.ok):
    return {field: Resolved(field=field, reading=Reading(value=value, observed_at=t, source="ecowitt", status=status),
                            tried=[])}


async def test_full_day_gives_a_chart_oldest_first_at_most_every_10_min():
    store = HistoryStore()
    fill(store, "outdoor.temperature", 30, step_min=5)
    out = await provider(store).fetch(RequestContext(now=NOW))
    r = out["outdoor.temperature_history"]
    assert r.status is Status.ok and r.source == "history" and r.unit == "°F"
    pts = shapes.validate("outdoor.temperature_history", r.value)
    times = [p.t for p in pts]
    assert times == sorted(times)
    assert times[0] >= NOW - timedelta(hours=24) and times[-1] == NOW
    assert all(b - a >= timedelta(minutes=10) for a, b in zip(times, times[1:], strict=False))
    assert r.observed_at == NOW


@pytest.mark.parametrize("hours,skip", [
    (19, ()),  # not enough span
    (24, [(NOW - timedelta(hours=10), NOW - timedelta(hours=7, minutes=55))]),  # a gap > 2 h
])
async def test_thin_coverage_is_missing(hours, skip):
    store = HistoryStore()
    fill(store, "outdoor.temperature", hours, skip=skip)
    r = (await provider(store).fetch(RequestContext(now=NOW)))["outdoor.temperature_history"]
    assert r.status is Status.missing and r.note == "insufficient_history" and r.observed_at is None


async def test_gap_of_just_under_2h_is_fine():
    store = HistoryStore()
    fill(store, "outdoor.temperature", 24, skip=[(NOW - timedelta(hours=10), NOW - timedelta(hours=8, minutes=15))])
    r = (await provider(store).fetch(RequestContext(now=NOW)))["outdoor.temperature_history"]
    assert r.status is Status.ok


async def test_empty_history_falls_back_to_nws_in_the_resolver():
    store = HistoryStore()
    fields = {"outdoor.temperature_history": [SourceCfg.model_validate({"from": "history", "max_age": "40m"}),
                                              SourceCfg.model_validate({"from": "nws", "max_age": "3h"})]}

    class Nws:
        name, ttl = "nws", timedelta(0)

        async def fetch(self, ctx):
            return {"outdoor.temperature_history": Reading(value=[{"t": NOW.isoformat(), "v": 50.0}],
                                                           observed_at=NOW, source="nws")}

    res = await collector.collect({"history": provider(store), "nws": Nws()}, fields, RequestContext(now=NOW))
    r = res["outdoor.temperature_history"]
    assert r.reading.source == "nws" and r.tried[0].note == "insufficient_history"


async def test_pressure_trend_vs_3h_ago():
    store = HistoryStore()
    fill(store, "pressure.sea_level", 4, step_min=10, value=lambda i: 30.10 - i * 0.002)
    r = (await provider(store).fetch(RequestContext(now=NOW)))["pressure.trend_3h"]
    assert r.status is Status.ok and r.unit == "inHg"
    assert r.value == pytest.approx(-0.036, abs=1e-6)  # 18 steps of 10 min × -0.002
    assert r.observed_at == NOW


async def test_pressure_trend_uses_nearest_point_within_tolerance():
    store = HistoryStore()
    store.add("pressure.sea_level", NOW - timedelta(hours=3, minutes=25), 30.00)
    store.add("pressure.sea_level", NOW, 30.05)
    r = (await provider(store).fetch(RequestContext(now=NOW)))["pressure.trend_3h"]
    assert r.value == pytest.approx(0.05)


@pytest.mark.parametrize("offset", [timedelta(hours=2, minutes=25), timedelta(hours=3, minutes=35)])
async def test_pressure_trend_missing_without_a_point_near_3h_ago(offset):
    store = HistoryStore()
    store.add("pressure.sea_level", NOW - offset, 30.00)
    store.add("pressure.sea_level", NOW, 30.05)
    r = (await provider(store).fetch(RequestContext(now=NOW)))["pressure.trend_3h"]
    assert r.status is Status.missing and r.note == "insufficient_history"


def test_record_only_ok_winners_with_spacing_and_keep(tmp_path):
    store = HistoryStore(tmp_path / "history.json")
    t = NOW
    assert store.record(resolved("outdoor.temperature", 58.4, t), t) == 1
    assert store.record(resolved("outdoor.temperature", 58.5, t + timedelta(minutes=2)), t) == 0  # too soon
    assert store.record(resolved("outdoor.temperature", 58.5, t - timedelta(hours=1)), t) == 0  # older
    assert store.record(resolved("outdoor.temperature", 57.0, t + timedelta(minutes=10),
                                 status=Status.stale), t) == 0
    assert store.record(resolved("outdoor.temperature", "n/a", t + timedelta(minutes=10)), t) == 0
    assert store.record(resolved("pressure.sea_level", 29.94, t), t) == 1
    assert [p.v for p in store.series("outdoor.temperature")] == [58.4]
    later = t + timedelta(hours=49)
    assert store.record(resolved("outdoor.temperature", 40.0, later), later) == 1
    assert [p.v for p in store.series("outdoor.temperature")] == [40.0]  # older than 48 h dropped
    assert store.series("pressure.sea_level") == []


def test_persists_compactly_and_reloads(tmp_path):
    path = tmp_path / "history.json"
    store = HistoryStore(path)
    store.record(resolved("outdoor.temperature", 58.4, NOW), NOW)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert data["series"]["outdoor.temperature"] == [[int(NOW.timestamp()), 58.4]]
    again = HistoryStore(path)
    assert again.series("outdoor.temperature")[0].t == NOW.astimezone(UTC)


@pytest.mark.parametrize("content", ["", "[]", "{bad", '{"version": 1, "series": {"x": [[1]]}}'])
def test_corrupt_file_starts_empty(tmp_path, content):
    path = tmp_path / "history.json"
    path.write_text(content, encoding="utf-8")
    store = HistoryStore(path)
    assert store.series("outdoor.temperature") == []
    store.record(resolved("outdoor.temperature", 58.4, NOW), NOW)  # and can be written again
    assert HistoryStore(path).series("outdoor.temperature")[0].v == 58.4


def test_provider_from_config_shares_the_store(tmp_path, monkeypatch):
    monkeypatch.setenv("INKPLATE_CACHE_DIR", str(tmp_path))
    app = AppConfig.model_validate({"location": {"name": "x", "lat": 1, "lon": 2},
                                    "providers": {"history": {"type": "history"}}})
    providers = collector.build_providers(app)
    assert providers["history"].store is get_store(tmp_path)


async def test_collect_records_history():
    store = HistoryStore()

    class Station:
        name, ttl = "ecowitt", timedelta(0)

        async def fetch(self, ctx):
            return {"0x02": Reading(value=58.4, observed_at=ctx.now), "wh25.rel": Reading(value=29.94,
                                                                                          observed_at=ctx.now)}

    fields = {"outdoor.temperature": [SourceCfg.model_validate({"from": "ecowitt", "key": "0x02"})],
              "pressure.sea_level": [SourceCfg.model_validate({"from": "ecowitt", "key": "wh25.rel"})]}
    await collector.collect({"ecowitt": Station()}, fields, RequestContext(now=NOW), history=store)
    assert store.series("outdoor.temperature")[0].v == 58.4
    assert store.series("pressure.sea_level")[0].v == 29.94
