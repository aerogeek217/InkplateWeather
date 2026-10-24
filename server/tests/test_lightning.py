"""GLM lightning: file parsing, the S3 poller (respx), bolt merging and drawing on the radar panel (offline).

The GLM files here are synthetic but use the real LCFA layout and attributes (flash_lat/lon float32, first-event
offsets as unsigned int16 with scale 0.00038148 and offset −5 s, quality flag 0 = good).
"""
import io
import json
from datetime import UTC, datetime, timedelta

import h5py
import httpx
import numpy as np
import pytest
import respx
from conftest import TEST_CONFIG

from app import collector, config, lightning, radarmap, viewmodel
from app.config import AppConfig, LocationCfg, ProviderCfg, ServerCfg
from app.providers import base, lightning_glm  # noqa: F401  (registers the type)
from app.providers.base import RequestContext
from app.providers.fixture import fixture_device, fixture_now
from app.readings import Reading
from app.resolver import Resolved

EXT = radarmap.extent()
BBOX = EXT.bbox(10)
T0 = datetime(2026, 10, 24, 19, 0, tzinfo=UTC)


def glm_file(start: datetime, flashes: list[tuple[float, float, float, int]]) -> bytes:
    """(lat, lon, seconds after start, quality) → LCFA-like NetCDF4 bytes."""
    buf = io.BytesIO()
    with h5py.File(buf, "w") as f:
        f.attrs["time_coverage_start"] = np.bytes_(start.strftime("%Y-%m-%dT%H:%M:%S.0Z"))
        f["flash_lat"] = np.array([x[0] for x in flashes], dtype="float32")
        f["flash_lon"] = np.array([x[1] for x in flashes], dtype="float32")
        raw = np.array([round((x[2] + 5) / 0.00038148) for x in flashes], dtype="uint16").view("int16")
        off = f.create_dataset("flash_time_offset_of_first_event", data=raw)
        off.attrs["scale_factor"] = np.array([0.00038148], dtype="float32")
        off.attrs["add_offset"] = np.array([-5.0], dtype="float32")
        off.attrs["_Unsigned"] = np.bytes_("true")
        f["flash_quality_flag"] = np.array([x[3] for x in flashes], dtype="int16")
    return buf.getvalue()


def key(t: datetime) -> str:
    s = f"{t.year}{t.timetuple().tm_yday:03d}{t:%H%M%S}0"
    return f"{lightning.hour_prefix(t)}OR_GLM-L2-LCFA_G19_s{s}_e{s}_c{s}.nc"


def listing(keys: list[str]) -> httpx.Response:
    items = "".join(f"<Contents><Key>{k}</Key><Size>1000</Size></Contents>" for k in keys)
    return httpx.Response(200, text=f'<?xml version="1.0" encoding="UTF-8"?><ListBucketResult '
                                    f'xmlns="http://s3.amazonaws.com/doc/2006-03-01/">{items}</ListBucketResult>')


def test_key_times_and_prefixes():
    k = "GLM-L2-LCFA/2026/297/18/OR_GLM-L2-LCFA_G19_s20262971800200_e20262971800400_c20262971800419.nc"
    assert lightning.key_start(k) == datetime(2026, 10, 24, 18, 0, 20, tzinfo=UTC)
    assert lightning.key_start("nope.nc") is None
    assert lightning.hour_prefix(T0) == "GLM-L2-LCFA/2026/297/19/"
    assert lightning.key_start(key(T0)) == T0


def test_parse_keeps_good_flashes_in_the_box():
    data = glm_file(T0, [(EXT.lat, EXT.lon, 12.5, 0), (EXT.lat + 0.1, EXT.lon, 3.0, 1), (35.0, -95.0, 4.0, 0)])
    out = lightning.parse(data, BBOX)
    assert out == [{"t": "2026-10-24T19:00:12+00:00", "lat": EXT.lat, "lon": EXT.lon}]


async def test_poller_backfills_then_keeps_up(tmp_path):
    store = lightning.Store(tmp_path / "lightning.json")
    poller = lightning.Poller(store, BBOX, "(test)")
    now = T0 + timedelta(minutes=5)
    old, a, b = T0 - timedelta(minutes=40), T0 - timedelta(minutes=10), T0 + timedelta(minutes=4)
    with respx.mock(assert_all_called=False) as router:
        lst = router.get(f"{lightning.BUCKET}/").mock(side_effect=lambda req: listing(
            [key(old), key(a)] if "/18/" in req.url.params["prefix"] else [key(b)]))
        router.get(f"{lightning.BUCKET}/{key(a)}").mock(
            return_value=httpx.Response(200, content=glm_file(a, [(EXT.lat, EXT.lon, 1.0, 0)])))
        router.get(f"{lightning.BUCKET}/{key(b)}").mock(
            return_value=httpx.Response(200, content=glm_file(b, [(EXT.lat + 0.2, EXT.lon, 1.0, 0)])))
        old_route = router.get(f"{lightning.BUCKET}/{key(old)}")
        assert await poller.poll(now) == 2  # the 40-minute-old file is outside the window
        assert old_route.call_count == 0 and lst.call_count == 2  # hours 18 and 19
        assert store.last_key == key(b) and len(store.recent(now)) == 2
        await poller.poll(now + timedelta(minutes=1))
        assert lst.calls[-1].request.url.params["start-after"] == key(b)  # only newer keys are listed
    saved = json.loads((tmp_path / "lightning.json").read_text())
    assert saved["last_key"] == key(b) and len(saved["flashes"]) == 2
    again = lightning.Store(tmp_path / "lightning.json")  # a restart keeps the last half hour
    assert len(again.recent(now)) == 2 and len(again.recent(now + timedelta(minutes=25))) == 1


async def test_failed_file_is_retried_next_poll(tmp_path):
    store = lightning.Store(None)
    poller = lightning.Poller(store, BBOX, "(test)")
    a, b = T0, T0 + timedelta(seconds=20)
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{lightning.BUCKET}/").mock(return_value=listing([key(a), key(b)]))
        router.get(f"{lightning.BUCKET}/{key(a)}").mock(return_value=httpx.Response(503))
        router.get(f"{lightning.BUCKET}/{key(b)}").mock(
            return_value=httpx.Response(200, content=glm_file(b, [(EXT.lat, EXT.lon, 1.0, 0)])))
        assert await poller.poll(T0 + timedelta(minutes=1)) == 0
    assert store.last_key == "" and store.flashes == []


def test_poller_runs_only_after_echoes():
    p = lightning.Poller(lightning.Store(None), BBOX, "(test)")
    assert not p.active(T0)
    p.note_radar({"echo_px": 0}, T0)
    assert not p.active(T0)
    p.note_radar({"echo_px": 120}, T0)
    assert p.active(T0 + timedelta(minutes=59)) and not p.active(T0 + timedelta(minutes=61))
    assert lightning.Poller(lightning.Store(None), BBOX, "(test)", always=True).active(T0)


async def test_provider_reads_the_rolling_list(tmp_path):
    app = AppConfig(location=LocationCfg(name="Savoy, IL", lat=40.0617, lon=-88.2517, tz="America/Chicago"),
                    server=ServerCfg(cache_dir=str(tmp_path)))
    p = base.build("glm", ProviderCfg(type="lightning_glm"), app)
    p.poller.store.flashes = [{"t": (T0 - timedelta(minutes=45)).isoformat(), "lat": 40.0, "lon": -88.0},
                              {"t": (T0 - timedelta(minutes=5)).isoformat(), "lat": 40.1, "lon": -88.2}]
    r = (await p.fetch(RequestContext(now=T0)))["lightning.flashes"]
    assert r.value == p.poller.store.flashes[1:] and r.observed_at == T0 and r.source == "glm"
    assert p.poller.store.path == tmp_path / "lightning.json"


def test_bolts_merge_per_cell_newest_wins():
    def at(dx_units, dy_units, minutes_ago):
        lon, lat = EXT.unproject(dx_units, dy_units)
        return {"t": (T0 - timedelta(minutes=minutes_ago)).isoformat(), "lat": lat, "lon": lon}
    flashes = [at(1, 1, 20), at(2, 2, 3),  # same 8 px cell: one bolt, the recent one
               at(60, 0, 15),  # older bolt
               at(100, 100, 40),  # too old
               at(300, 0, 1)]  # outside the panel
    marks = radarmap.bolts(flashes, T0, EXT)
    assert [(round(x), round(y), recent) for x, y, recent in marks] == [(60, 0, False), (2, 2, True)]
    svg = radarmap.layers(None, "SAVOY", marks=marks)
    assert svg.count(radarmap.BOLT_PATH) == 2
    assert svg.index('fill="#FFFFFF" stroke="#000000" stroke-width="2"') < svg.index('fill="#000000" stroke="#FFFFFF"')
    assert svg.index("r=\"140\"") < svg.index(radarmap.BOLT_PATH) < svg.index(">SAVOY<")


async def test_radar_panel_shows_bolts_and_the_legend_entry():
    cfg = config.load(TEST_CONFIG)
    now = fixture_now("radar-storm")
    resolved = dict(await collector.collect_fixture("radar-storm", RequestContext(now=now)))
    lon, lat = EXT.unproject(-40, -10)
    resolved["lightning.flashes"] = Resolved(field="lightning.flashes", tried=[], reading=Reading(
        value=[{"t": (now - timedelta(minutes=4)).isoformat(), "lat": lat, "lon": lon}], observed_at=now,
        source="glm"))
    r = viewmodel.build(resolved, now, fixture_device("radar-storm"), cfg).radar
    assert r.lightning_legend and r.svg_layers.count(radarmap.BOLT_PATH) == 1
    del resolved["lightning.flashes"]
    r = viewmodel.build(resolved, now, fixture_device("radar-storm"), cfg).radar
    assert not r.lightning_legend and radarmap.BOLT_PATH not in r.svg_layers


@pytest.mark.parametrize("bad", [{"t": "nope", "lat": 40, "lon": -88}, {"lat": 40}, "x"])
def test_bad_flash_entries_are_skipped(bad):
    assert radarmap.bolts([bad] if isinstance(bad, dict) else [], T0, EXT) == []
