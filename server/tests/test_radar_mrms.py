"""radar_mrms provider against recorded MRMS files (offline, respx).

- storm: SeamlessHSR + PrecipFlag, 2023-04-01 01:00 UTC (a severe line through Champaign County)
- winter: 2022-02-02 09:00 UTC (snow north of Savoy, rain south); the surface temperature grid is the live one
  from 2026-10-24 (warm) or, for the ice class, the same grid rewritten to ≤ 0 °C north of a line
"""
import gzip
from datetime import UTC, datetime

import httpx
import pytest
import respx
from conftest import TESTS_DIR

from app import grib2, radarmap
from app.config import AppConfig, LocationCfg, ProviderCfg
from app.providers import base, radar_mrms
from app.providers.base import RequestContext
from app.readings import Status

MRMS = TESTS_DIR / "data" / "recorded" / "mrms"
NOW = datetime(2026, 10, 24, 19, 0, tzinfo=UTC)
REFL = radar_mrms.url(radar_mrms.BASE_URL, "ReflectivityAtLowestAltitude")
FLAG = radar_mrms.url(radar_mrms.BASE_URL, "PrecipFlag")
TEMP = radar_mrms.url(radar_mrms.BASE_URL, "Model_SurfaceTemp")


def gz(name: str) -> httpx.Response:
    return httpx.Response(200, content=gzip.compress((MRMS / name).read_bytes()))


STORM = ("SeamlessHSR_00.00_20230401-010000.grib2", "PrecipFlag_00.00_20230401-010000.grib2")
WINTER = ("SeamlessHSR_00.00_20220202-090000.grib2", "PrecipFlag_00.00_20220202-090000.grib2")
QUIET = ("MRMS_ReflectivityAtLowestAltitude_00.50_20261024-184236.grib2",
         "MRMS_PrecipFlag_00.50_20261024-184200.grib2")
WARM = "MRMS_Model_SurfaceTemp_00.50_20261024-170000.grib2"


def cold_temp() -> httpx.Response:
    """The recorded temperature grid, set to −2 °C north of 39.86° N and 3 °C south of it."""
    g = grib2.decode((MRMS / WARM).read_bytes()).crop(-180, -90, 180, 90, pad=0)
    g.raw = [round(((-2.0 if g.lonlat(i, j)[1] > 39.86 else 3.0) * 10 ** g.d - g.ref) / 2 ** g.e)
             for j in range(g.nj) for i in range(g.ni)]
    return httpx.Response(200, content=gzip.compress(grib2.encode(g)))


@pytest.fixture
def provider():
    app = AppConfig(location=LocationCfg(name="Savoy, IL", lat=40.0617, lon=-88.2517, tz="America/Chicago"))
    return base.build("mrms", ProviderCfg(type="radar_mrms", ttl="2m"), app)


def mock(router, files, temp=None):
    routes = [router.get(REFL).mock(return_value=files[0] if isinstance(files[0], httpx.Response) else gz(files[0])),
              router.get(FLAG).mock(return_value=files[1] if isinstance(files[1], httpx.Response) else gz(files[1]))]
    routes.append(router.get(TEMP).mock(return_value=temp if temp is not None else gz(WARM)))
    return routes


async def test_storm_frame(provider):
    with respx.mock(assert_all_called=False) as router:
        refl, flag, temp = mock(router, STORM)
        out = await provider.fetch(RequestContext(now=NOW))
        assert refl.call_count == flag.call_count == temp.call_count == 1  # liquid in view → temperature checked
        await provider.fetch(RequestContext(now=NOW))
        assert temp.call_count == 1  # the hourly grid is kept
    r = out["radar.frame"]
    assert r.status is Status.ok and r.source == "mrms"
    assert r.observed_at == datetime(2023, 4, 1, 1, 0, tzinfo=UTC)
    f = r.value
    assert f["site"] == "MRMS" and f["legend"] == "intensity" and f["types"] == ["rain"]
    assert [x["fill"] for x in f["layers"]] == ["rain-light", "rain-moderate", "rain-heavy", "rain-extreme"]
    assert f["echo_px"] > 100_000
    assert f["aria_label"] == "Radar map around Savoy: rain in view"
    assert refl.calls[0].request.headers["User-Agent"].startswith("(InkplateWeather")


async def test_winter_frame_snow_and_rain(provider):
    with respx.mock(assert_all_called=False) as router:
        mock(router, WINTER)  # warm temperature grid: no ice
        f = (await provider.fetch(RequestContext(now=NOW)))["radar.frame"].value
    assert f["legend"] == "types" and f["types"] == ["rain", "snow"]
    assert [x["fill"] for x in f["layers"]] == ["rain", "snow", "snow-heavy"]


async def test_freezing_surface_turns_rain_into_ice(provider):
    with respx.mock(assert_all_called=False) as router:
        mock(router, WINTER, temp=cold_temp())
        f = (await provider.fetch(RequestContext(now=NOW)))["radar.frame"].value
    assert f["types"] == ["ice", "rain", "snow"]
    assert [x["fill"] for x in f["layers"]] == ["rain", "ice", "snow", "snow-heavy"]
    assert f["aria_label"] == "Radar map around Savoy: rain, ice or sleet, and snow in view"


async def test_quiet_day_skips_the_temperature_grid(provider):
    with respx.mock(assert_all_called=False) as router:
        _, _, temp = mock(router, QUIET)
        f = (await provider.fetch(RequestContext(now=NOW)))["radar.frame"].value
    assert temp.call_count == 0
    assert f["layers"] == [] and f["echo_px"] == 0 and f["legend"] == "intensity"
    assert f["time"] == "2026-10-24T18:42:36+00:00"


async def test_missing_flag_and_temperature_still_draw_intensity(provider):
    with respx.mock(assert_all_called=False) as router:
        _, _, temp = mock(router, (WINTER[0], httpx.Response(404)), temp=httpx.Response(503))
        f = (await provider.fetch(RequestContext(now=NOW)))["radar.frame"].value
    assert temp.call_count == 0  # without types there is nothing to turn into ice
    assert f["legend"] == "intensity" and f["layers"]


async def test_temperature_failure_is_not_fatal(provider):
    with respx.mock(assert_all_called=False) as router:
        _, _, temp = mock(router, WINTER, temp=httpx.Response(503))
        f = (await provider.fetch(RequestContext(now=NOW)))["radar.frame"].value
        await provider.fetch(RequestContext(now=NOW))
    assert temp.call_count == 1  # retried after an hour, not on every render
    assert f["types"] == ["rain", "snow"]


async def test_reflectivity_failure_raises(provider):
    with respx.mock(assert_all_called=False) as router:
        mock(router, (httpx.Response(503), STORM[1]))
        with pytest.raises(httpx.HTTPStatusError):
            await provider.fetch(RequestContext(now=NOW))
    with respx.mock(assert_all_called=False) as router:
        mock(router, (httpx.Response(200, content=b"<html>maintenance</html>"), STORM[1]))
        with pytest.raises(grib2.GribError):
            await provider.fetch(RequestContext(now=NOW))


def test_extent_is_the_basemap_center():
    assert radarmap.extent().project(-88.2517, 40.0617) == (0, 0)
