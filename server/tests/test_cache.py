"""Provider TTL cache: reuse within TTL, single flight, last-good fallback, persistence, config changes."""
import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from app import collector
from app.cache import ProviderCache, config_signature
from app.config import AppConfig
from app.providers.base import RequestContext
from app.readings import Reading, Status, error, missing

T0 = datetime(2026, 10, 24, 19, 30, tzinfo=UTC)


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


class FakeProvider:
    """Returns {"temp": value} observed at the clock's time; behaviour switchable per call."""

    def __init__(self, name="station", ttl=timedelta(minutes=10), clock=None):
        self.name, self.ttl, self.clock = name, ttl, clock or Clock()
        self.calls = 0
        self.value = 58.4
        self.mode = "ok"  # ok | raise | slow | errors | partial
        self.delay = 0.0

    async def fetch(self, ctx):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.mode == "raise":
            raise ConnectionError("gateway unreachable")
        if self.mode == "errors":
            return {"temp": error(self.name, "http_503"), "rh": error(self.name, "http_503")}
        if self.mode == "partial":
            return {"temp": error(self.name, "parse"), "rh": Reading(value=40, observed_at=self.clock(),
                                                                      source=self.name)}
        if self.mode == "missing":
            return {"temp": missing(self.name, "sensor_offline"), "rh": Reading(value=41, observed_at=self.clock(),
                                                                                 source=self.name)}
        return {"temp": Reading(value=self.value, unit="°F", observed_at=self.clock(), source=self.name),
                "rh": Reading(value=42, unit="%", observed_at=self.clock(), source=self.name)}


CTX = RequestContext(now=T0)


def make(tmp_path=None, ttl=timedelta(minutes=10)):
    clock = Clock()
    cache = ProviderCache(tmp_path, clock=clock)
    return cache, FakeProvider(ttl=ttl, clock=clock), clock


async def test_reuses_result_within_ttl():
    cache, p, clock = make()
    a = await cache.get(p, CTX, 8)
    clock.advance(minutes=9)
    b = await cache.get(p, CTX, 8)
    assert p.calls == 1 and a["temp"].value == b["temp"].value
    assert cache.health()["station"].served == "cache"
    clock.advance(minutes=2)
    p.value = 60.0
    c = await cache.get(p, CTX, 8)
    assert p.calls == 2 and c["temp"].value == 60.0
    assert cache.health()["station"].served == "live"


async def test_zero_ttl_fetches_every_time():
    cache, p, _ = make(ttl=timedelta(0))
    await cache.get(p, CTX, 8)
    await cache.get(p, CTX, 8)
    assert p.calls == 2


async def test_failure_serves_last_good_with_original_observed_at():
    cache, p, clock = make()
    first = await cache.get(p, CTX, 8)
    clock.advance(minutes=30)
    p.mode = "raise"
    out = await cache.get(p, CTX, 8)
    assert out["temp"].value == 58.4 and out["temp"].observed_at == first["temp"].observed_at
    h = cache.health()["station"]
    assert h.served == "fallback" and "gateway unreachable" in h.last_error
    assert h.last_success == T0


async def test_failure_without_last_good_returns_the_exception():
    cache, p, _ = make()
    p.mode = "raise"
    out = await cache.get(p, CTX, 8)
    assert isinstance(out, ConnectionError)
    assert cache.health()["station"].served == "error"


async def test_last_good_is_capped():
    cache, p, clock = make()
    await cache.get(p, CTX, 8)
    clock.advance(hours=25)
    p.mode = "raise"
    assert isinstance(await cache.get(p, CTX, 8), ConnectionError)


async def test_all_error_readings_count_as_failure():
    cache, p, clock = make()
    await cache.get(p, CTX, 8)
    clock.advance(minutes=11)
    p.mode = "errors"
    out = await cache.get(p, CTX, 8)
    assert out["temp"].status is Status.ok and out["temp"].value == 58.4
    h = cache.health()["station"]
    assert h.served == "fallback" and "every reading failed" in h.last_error
    assert cache.entry("station").fetched_at == T0  # the failed fetch didn't refresh the entry


async def test_error_key_filled_from_last_good_but_missing_key_is_not():
    cache, p, clock = make()
    await cache.get(p, CTX, 8)
    clock.advance(minutes=11)
    p.mode = "partial"
    out = await cache.get(p, CTX, 8)
    assert out["temp"].value == 58.4 and out["temp"].observed_at == T0  # substituted, stays old
    assert out["rh"].value == 40
    assert cache.health()["station"].served == "fallback"
    clock.advance(minutes=11)
    p.mode = "missing"  # the source says the sensor has no value: believe it
    out = await cache.get(p, CTX, 8)
    assert out["temp"].status is Status.missing


async def test_concurrent_requests_fetch_once():
    cache, p, clock = make(ttl=timedelta(0))
    p.delay = 0.05
    a, b = await asyncio.gather(cache.get(p, CTX, 8), cache.get(p, CTX, 8))
    assert p.calls == 1 and a["temp"].value == b["temp"].value


async def test_timeout_serves_cached_value_and_fetch_completes_in_background():
    cache, p, clock = make()
    await cache.get(p, CTX, 8)
    clock.advance(minutes=11)
    p.delay, p.value = 0.2, 61.0
    out = await cache.get(p, CTX, 0.02)
    assert out["temp"].value == 58.4
    assert "TimeoutError" in cache.health()["station"].last_error
    await asyncio.sleep(0.3)  # the slow fetch finished and refreshed the cache
    out = await cache.get(p, CTX, 8)
    assert out["temp"].value == 61.0 and p.calls == 2


async def test_timeout_without_cache_is_an_error():
    cache, p, _ = make()
    p.delay = 0.2
    out = await cache.get(p, CTX, 0.02)
    assert isinstance(out, TimeoutError)
    await asyncio.sleep(0.25)


async def test_persists_and_survives_restart(tmp_path):
    cache, p, clock = make(tmp_path)
    await cache.get(p, CTX, 8)
    files = list((tmp_path / "providers").glob("*.json"))
    assert [f.name for f in files] == ["station.json"]
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert data["readings"]["temp"]["value"] == 58.4

    cache2 = ProviderCache(tmp_path, clock=clock)  # "restart"
    p2 = FakeProvider(clock=clock)
    clock.advance(minutes=5)
    out = await cache2.get(p2, CTX, 8)  # still within TTL: no fetch
    assert p2.calls == 0 and out["temp"].value == 58.4 and out["temp"].observed_at == T0
    clock.advance(minutes=10)
    p2.mode = "raise"
    out = await cache2.get(p2, CTX, 8)  # expired and failing: last good from disk
    assert out["temp"].value == 58.4 and cache2.health()["station"].served == "fallback"


@pytest.mark.parametrize("content", ["", "{not json", '{"version": 1, "name": "station"}', '{"version": 99}'])
async def test_corrupt_cache_file_is_ignored(tmp_path, content):
    (tmp_path / "providers").mkdir()
    (tmp_path / "providers" / "station.json").write_text(content, encoding="utf-8")
    cache, p, _ = make(tmp_path)
    out = await cache.get(p, CTX, 8)
    assert p.calls == 1 and out["temp"].value == 58.4


def _cfg(host="192.168.1.135", ttl="30s", lat=40.0617):
    return AppConfig.model_validate({
        "location": {"name": "Savoy", "lat": lat, "lon": -88.25},
        "providers": {"station": {"type": "fake", "host": host, "ttl": ttl}, "om": {"type": "fake2"}},
    })


def test_signature_ignores_ttl_but_not_options_or_location():
    assert config_signature("station", _cfg()) == config_signature("station", _cfg(ttl="5m"))
    assert config_signature("station", _cfg()) != config_signature("station", _cfg(host="10.0.0.2"))
    assert config_signature("station", _cfg()) != config_signature("station", _cfg(lat=41.0))


async def test_configure_drops_changed_providers(tmp_path):
    cache, p, clock = make(tmp_path)
    cache.configure(_cfg())
    await cache.get(p, CTX, 8)
    assert cache.configure(_cfg(ttl="1m")) == {"kept": ["om", "station"], "reset": []}
    assert cache.entry("station") is not None
    assert cache.configure(_cfg(host="10.0.0.2")) == {"kept": ["om"], "reset": ["station"]}
    assert cache.entry("station") is None
    assert not (tmp_path / "providers" / "station.json").exists()


async def test_file_from_other_config_is_ignored(tmp_path):
    cache, p, clock = make(tmp_path)
    cache.configure(_cfg())
    await cache.get(p, CTX, 8)
    cache2 = ProviderCache(tmp_path, clock=clock)
    cache2.configure(_cfg(host="10.0.0.2"))  # e.g. config edited while the container was down
    assert cache2.entry("station") is None


async def test_device_and_history_results_are_not_persisted(tmp_path):
    cache = ProviderCache(tmp_path, clock=Clock())
    cfg = AppConfig.model_validate({"location": {"name": "x", "lat": 1, "lon": 2},
                                    "providers": {"station": {"type": "device"}}})
    cache.configure(cfg)
    await cache.get(FakeProvider(), CTX, 8)
    assert not (tmp_path / "providers").exists()


async def test_collect_uses_the_cache():
    from app.config import SourceCfg

    cache, p, clock = make()
    fields = {"outdoor.temperature": [SourceCfg.model_validate({"from": "station", "key": "temp",
                                                                 "max_age": "10m"})]}
    ctx = RequestContext(now=T0)
    r1 = await collector.collect({"station": p}, fields, ctx, cache=cache)
    assert r1["outdoor.temperature"].reading.status is Status.ok
    clock.advance(minutes=20)
    p.mode = "raise"
    r2 = await collector.collect({"station": p}, fields, RequestContext(now=clock()), cache=cache)
    assert r2["outdoor.temperature"].reading.status is Status.stale  # last good, marked stale by max_age
    assert r2["outdoor.temperature"].reading.value == 58.4
