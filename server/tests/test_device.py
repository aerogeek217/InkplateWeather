from datetime import UTC, datetime, timedelta

from conftest import REPO_DIR

from app import collector
from app.config import ProviderCfg, load
from app.providers import base
from app.providers.base import DeviceInfo, RequestContext
from app.providers.device import DeviceProvider
from app.readings import Status

NOW = datetime(2026, 10, 24, 19, 30, tzinfo=UTC)


async def test_reports_request_values():
    ctx = RequestContext(now=NOW, device=DeviceInfo(battery_pct=78, voltage_mv=3912, wake_reason="timer"))
    out = await DeviceProvider("device", ProviderCfg(type="device")).fetch(ctx)
    assert {k: (r.value, r.unit, r.status) for k, r in out.items()} == {
        "device.battery_pct": (78, "%", Status.ok),
        "device.voltage_mv": (3912, "mV", Status.ok),
        "device.wake_reason": ("timer", None, Status.ok),
    }
    assert all(r.observed_at == NOW and r.source == "device" for r in out.values())


async def test_missing_when_request_lacks_values():
    ctx = RequestContext(now=NOW, device=DeviceInfo(battery_pct=0))
    out = await DeviceProvider("device").fetch(ctx)
    assert out["device.battery_pct"].value == 0 and out["device.battery_pct"].usable  # 0 % is a value, not missing
    for key in ("device.voltage_mv", "device.wake_reason"):
        r = out[key]
        assert r.status is Status.missing and r.note == "not_in_request" and r.observed_at is None


async def test_never_cached():
    p = DeviceProvider("device", ProviderCfg(type="device", ttl="10m"))
    assert p.ttl == timedelta(0)


async def test_example_config_device_fields_resolve():
    cfg = load(REPO_DIR / "config" / "config.example.yaml")
    p = base.build("device", cfg.providers["device"], cfg)
    assert isinstance(p, DeviceProvider)
    dev_fields = {f: s for f, s in cfg.fields.items() if f.startswith("device.")}
    ctx = RequestContext(now=NOW, device=DeviceInfo(battery_pct=12, voltage_mv=3550, wake_reason="button"))
    resolved = await collector.collect({"device": p}, dev_fields, ctx)
    assert {f: r.reading.value for f, r in resolved.items()} == {
        "device.battery_pct": 12, "device.voltage_mv": 3550, "device.wake_reason": "button"}
    assert all(r.reading.status is Status.ok for r in resolved.values())
