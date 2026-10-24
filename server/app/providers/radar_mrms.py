"""`radar_mrms` provider: the latest NOAA MRMS reflectivity + precipitation type → `radar.frame`.

Fetches (concurrently) from `https://mrms.ncep.noaa.gov/2D/`:

- `<product>/MRMS_<product>.latest.grib2.gz`: reflectivity in dBZ, default `ReflectivityAtLowestAltitude`
  (alternatives: `MergedBaseReflectivityQC`, `SeamlessHSR`), ~0.7 MB, every 2 min
- `PrecipFlag/…`: rain / snow / hail per cell (no ice class)
- `Model_SurfaceTemp/…` (hourly, ~14 MB) only when there is liquid precipitation in view, at most hourly, and only
  every 6 h while the last grid was warm (> 4 °C everywhere in view). Liquid precipitation at ≤ 0 °C at the surface
  is drawn as ice/sleet. If this grid is unavailable the frame still renders, without the ice class.

Each GRIB2 file is decoded with `app.grib2` (PNG packing; no eccodes), cropped to the radar extent and classified by
`app.radar.frame`. A missing PrecipFlag gives an intensity-only frame; a missing reflectivity file raises.

Config: `mrms: {type: radar_mrms, ttl: 2m}`; optional `product`, `ice` (default true), `timeout` (seconds, default
10), `base_url`.
"""
from __future__ import annotations

import asyncio
import gzip
import logging
import time
from typing import Any

import httpx

from .. import grib2, radar, radarmap
from ..config import AppConfig, ProviderCfg
from ..readings import Reading
from .base import RequestContext, register

log = logging.getLogger(__name__)

BASE_URL = "https://mrms.ncep.noaa.gov/2D"
MARGIN = 8.0  # map units of grid kept around the panel, so bilinear edges have neighbours
TEMP_REFRESH_S = 3600  # the surface temperature grid is hourly
TEMP_WARM_REFRESH_S = 6 * 3600
WARM_C = 4.0

FLAG_TYPES = {1: radar.RAIN, 2: radar.RAIN, 6: radar.RAIN, 10: radar.RAIN, 91: radar.RAIN, 96: radar.RAIN,
              3: radar.SNOW, 4: radar.SNOW, 7: radar.HAIL}


def url(base: str, product: str) -> str:
    return f"{base}/{product}/MRMS_{product}.latest.grib2.gz"


def decode_crop(data: bytes, bbox: tuple[float, float, float, float]) -> grib2.Grid:
    raw = gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data
    return grib2.decode(raw).crop(*bbox)


def _sample(g: grib2.Grid, lon: float, lat: float) -> float | None:
    i = round((lon - g.lon1) / g.di)
    j = round((g.lat1 - lat) / g.dj)
    if 0 <= i < g.ni and 0 <= j < g.nj:
        return g.value(i, j)
    return None


def rasters(refl: grib2.Grid, flag: grib2.Grid | None,
            temp: grib2.Grid | None) -> tuple[radar.Raster, radar.Raster | None]:
    """Reflectivity and type rasters on the reflectivity grid's cells."""
    z = bytes(radar.enc(refl.scale(x)) for x in refl.raw)
    zr = radar.Raster(refl.ni, refl.nj, refl.lon1, refl.lat1, refl.di, refl.dj, z)
    if flag is None:
        return zr, None
    codes = bytearray(refl.ni * refl.nj)
    for j in range(refl.nj):
        for i in range(refl.ni):
            if not z[j * refl.ni + i]:
                continue
            lon, lat = refl.lonlat(i, j)
            f = _sample(flag, lon, lat)
            t = FLAG_TYPES.get(round(f)) if f is not None else None
            if t == radar.RAIN and temp is not None:
                c = _sample(temp, lon, lat)
                if c is not None and -60 < c <= 0.0:
                    t = radar.ICE
            codes[j * refl.ni + i] = t or radar.NONE
    return zr, radar.Raster(refl.ni, refl.nj, refl.lon1, refl.lat1, refl.di, refl.dj, bytes(codes))


def _min_temp(g: grib2.Grid) -> float:
    vals = [v for v in (g.scale(x) for x in g.raw) if -60 < v < 60]
    return min(vals) if vals else 99.0


def _has_liquid(refl: grib2.Grid, flag: grib2.Grid | None) -> bool:
    if flag is None:
        return False
    lo = radar.LIGHT
    for j in range(0, refl.nj, 2):  # every other cell is plenty to spot rain in view
        for i in range(0, refl.ni, 2):
            if refl.value(i, j) >= lo:
                f = _sample(flag, *refl.lonlat(i, j))
                if f is not None and FLAG_TYPES.get(round(f)) == radar.RAIN:
                    return True
    return False


@register("radar_mrms")
class MrmsProvider:
    def __init__(self, name: str, cfg: ProviderCfg, app: AppConfig):
        self.name = name
        self.ttl = cfg.ttl
        opts = cfg.options
        self.product = str(opts.get("product", "ReflectivityAtLowestAltitude"))
        self.ice = bool(opts.get("ice", True))
        self.timeout = float(opts.get("timeout", 10))
        self.base = str(opts.get("base_url", BASE_URL)).rstrip("/")
        self.user_agent = app.nws.user_agent
        self.home = app.location.name.split(",")[0]
        self._temp: grib2.Grid | None = None
        self._temp_at = 0.0  # time.monotonic() of the last attempt

    async def _get(self, client: httpx.AsyncClient, product: str) -> bytes:
        r = await client.get(url(self.base, product))
        r.raise_for_status()
        return r.content

    def _temp_due(self) -> bool:
        if not self.ice:
            return False
        age = time.monotonic() - self._temp_at
        if self._temp is None:
            return self._temp_at == 0.0 or age >= TEMP_REFRESH_S
        if age < TEMP_REFRESH_S:
            return False
        return _min_temp(self._temp) <= WARM_C or age >= TEMP_WARM_REFRESH_S

    async def _surface_temp(self, client: httpx.AsyncClient, bbox) -> grib2.Grid | None:
        if self._temp_due():
            self._temp_at = time.monotonic()
            try:
                data = await self._get(client, "Model_SurfaceTemp")
                self._temp = await asyncio.to_thread(decode_crop, data, bbox)
            except (httpx.HTTPError, grib2.GribError, OSError, EOFError) as e:
                log.warning("%s: surface temperature unavailable (%s); no ice class this hour", self.name, e)
        return self._temp

    async def fetch(self, ctx: RequestContext) -> dict[str, Reading]:
        ext = radarmap.extent()
        bbox = ext.bbox(MARGIN)
        async with httpx.AsyncClient(timeout=self.timeout, headers={"User-Agent": self.user_agent},
                                     follow_redirects=True) as client:
            refl_data, flag_data = await asyncio.gather(self._get(client, self.product),
                                                        self._get(client, "PrecipFlag"), return_exceptions=True)
            if isinstance(refl_data, BaseException):
                raise refl_data
            refl = await asyncio.to_thread(decode_crop, refl_data, bbox)
            flag = None
            if isinstance(flag_data, BaseException):
                log.warning("%s: PrecipFlag unavailable (%s); intensity only", self.name, flag_data)
            else:
                try:
                    flag = await asyncio.to_thread(decode_crop, flag_data, bbox)
                except (grib2.GribError, OSError, EOFError) as e:
                    log.warning("%s: PrecipFlag unreadable (%s); intensity only", self.name, e)
            temp = await self._surface_temp(client, bbox) if _has_liquid(refl, flag) else None
        frame = await asyncio.to_thread(build_frame, ext, refl, flag, temp, self.home)
        return {"radar.frame": Reading(value=frame, observed_at=refl.time, source=self.name)}


def build_frame(ext, refl: grib2.Grid, flag: grib2.Grid | None, temp: grib2.Grid | None,
                home: str) -> dict[str, Any]:
    zr, tr = rasters(refl, flag, temp)
    return radar.frame(ext, zr, tr, site="MRMS", time=refl.time, home=home)
