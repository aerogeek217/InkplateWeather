"""radar_iem fallback against the IEM n0q mosaic recorded for 2023-04-01 01:00 UTC (offline, respx).

The same moment as the recorded MRMS storm, so the two sources should draw nearly the same echoes.
"""
import base64
import gzip
import io
from datetime import UTC, datetime

import httpx
import pytest
import respx
from conftest import TESTS_DIR
from PIL import Image, ImageChops

from app import radar
from app.config import AppConfig, LocationCfg, ProviderCfg
from app.providers import base, radar_iem, radar_mrms
from app.providers.base import RequestContext

IEM = TESTS_DIR / "data" / "recorded" / "iem"
MRMS = TESTS_DIR / "data" / "recorded" / "mrms"
NOW = datetime(2023, 4, 1, 1, 3, tzinfo=UTC)
STEM = "n0q_202304010100"


@pytest.fixture
def provider():
    app = AppConfig(location=LocationCfg(name="Savoy, IL", lat=40.0617, lon=-88.2517, tz="America/Chicago"))
    return base.build("iem", ProviderCfg(type="radar_iem", ttl="5m"), app)


def mock(router, png=None, wld=None, meta=None):
    def resp(x, ext):
        return x if isinstance(x, httpx.Response) else httpx.Response(200, content=(IEM / f"{STEM}.{ext}").read_bytes())
    return [router.get(f"{radar_iem.BASE_URL}/n0q_0.{ext}").mock(return_value=resp(x, ext))
            for x, ext in ((png, "png"), (wld, "wld"), (meta, "json"))]


def union(frame) -> Image.Image:
    out = Image.new("1", (551, 585))
    for layer in frame["layers"]:
        out = ImageChops.logical_or(out, Image.open(io.BytesIO(base64.b64decode(layer["png"]))).convert("1"))
    return out


async def test_recorded_mosaic(provider):
    with respx.mock() as router:
        mock(router)
        r = (await provider.fetch(RequestContext(now=NOW)))["radar.frame"]
    assert r.source == "iem" and r.observed_at == datetime(2023, 4, 1, 1, 0, tzinfo=UTC)
    f = r.value
    assert f["site"] == "NEXRAD" and f["legend"] == "intensity" and f["types"] == ["rain"]
    assert [x["fill"] for x in f["layers"]] == ["rain-light", "rain-moderate", "rain-heavy", "rain-extreme"]

    # Same storm as MRMS at that minute. IEM's mosaic takes the strongest return of overlapping radars, so it
    # shows ~30 % more echo than MRMS's lowest-altitude product; the shapes still overlap well.
    app = AppConfig(location=LocationCfg(name="Savoy, IL", lat=40.0617, lon=-88.2517, tz="America/Chicago"))
    mrms = base.build("mrms", ProviderCfg(type="radar_mrms", ttl="2m"), app)
    with respx.mock(assert_all_called=False) as router:
        for product, name in ((radar_mrms.PRODUCT, "SeamlessHSR_00.00_20230401-010000.grib2"),
                              ("PrecipFlag", "PrecipFlag_00.00_20230401-010000.grib2"),
                              ("Model_SurfaceTemp", "MRMS_Model_SurfaceTemp_00.50_20261024-170000.grib2")):
            router.get(radar_mrms.url(radar_mrms.BASE_URL, product)).mock(
                return_value=httpx.Response(200, content=gzip.compress((MRMS / name).read_bytes())))
        m = (await mrms.fetch(RequestContext(now=NOW)))["radar.frame"].value
    a, b = union(f), union(m)
    inter = ImageChops.logical_and(a, b).histogram()[255]
    uni = ImageChops.logical_or(a, b).histogram()[255]
    assert inter / uni > 0.6 and a.histogram()[255] > b.histogram()[255]


def test_palette_index_is_dbz():
    assert radar.enc(130 / 2 - 32.5) == radar.enc(32.5)  # index 130 is the green of 30–35 dBZ
    img = Image.new("P", (4, 1))
    img.putpalette([v for i in range(256) for v in (i, i, i)])  # a full palette, so the PNG keeps the indices
    img.putdata([0, 95, 130, 180])
    buf = io.BytesIO()
    img.save(buf, "PNG")
    r = radar_iem.raster(buf.getvalue(), "0.005\n0\n0\n-0.005\n-88.3\n40.1\n", (-90, 39, -87, 41))
    assert list(r.data) == [0, radar.enc(15), radar.enc(32.5), radar.enc(57.5)]
    assert (r.lon1, r.lat1, r.di, r.dj) == (-88.3, 40.1, 0.005, 0.005)


async def test_failures_raise(provider):
    with respx.mock(assert_all_called=False) as router:
        mock(router, png=httpx.Response(503))
        with pytest.raises(httpx.HTTPStatusError):
            await provider.fetch(RequestContext(now=NOW))
    with respx.mock(assert_all_called=False) as router:
        mock(router, meta=httpx.Response(200, json={"meta": {}}))
        with pytest.raises(radar_iem.IemError, match="no mosaic time"):
            await provider.fetch(RequestContext(now=NOW))
    rgb = io.BytesIO()
    Image.new("RGB", (10, 10)).save(rgb, "PNG")
    with respx.mock(assert_all_called=False) as router:
        mock(router, png=httpx.Response(200, content=rgb.getvalue()))
        with pytest.raises(radar_iem.IemError, match="palette"):
            await provider.fetch(RequestContext(now=NOW))
    with pytest.raises(radar_iem.IemError, match="world file"):
        radar_iem.world("0.005\n0.1\n0\n-0.005\n-126\n50\n")
