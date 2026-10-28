"""`radar_mrms` provider: the latest NOAA MRMS reflectivity + precipitation type → `radar.frame`.

Fetches (concurrently) from `https://mrms.ncep.noaa.gov/2D/`:

- `<product>/MRMS_<product>.latest.grib2.gz`: reflectivity in dBZ, default `MergedBaseReflectivityQC` (the
  base-scan mosaic, like most radar apps; alternatives: `ReflectivityAtLowestAltitude`, `SeamlessHSR`, which miss
  light rain aloft: on 2026-10-27 they showed ~200 cells where the base scan showed ~1,600), ~0.7 MB, every 2 min
- `PrecipFlag/…`: rain / snow / hail per cell (no ice class)
- `Model_SurfaceTemp/…` (hourly, ~14 MB) only when there is liquid precipitation in view, at most hourly, and only
  every 6 h while the last grid was warm (> 4 °C everywhere in view). Liquid precipitation at ≤ 0 °C at the surface
  is drawn as ice/sleet. If this grid is unavailable the frame still renders, without the ice class.

Each GRIB2 file is decoded with `app.grib2` (PNG packing; no eccodes), cropped to the radar extent and classified by
`app.radar.frame`. A missing PrecipFlag gives an intensity-only frame; a missing reflectivity file raises.

Storm motion (`app.motion`): with echoes in view, the files ~15 and ~30 min older are picked from the product's
directory listing (`/2D/<product>/`, ~190 kB; PrecipFlag's too for a snow line) and fetched (~0.7 MB each, kept in
memory, so the next refresh mostly reuses them). Motion uses a wider window (MOTION_MARGIN) so storms still outside
the panel count. The frame then carries `motion`, `arrival` and `overlays` (arrival lines, motion arrow, snow lines).
Any failure there only drops the overlays.

Config: `mrms: {type: radar_mrms, ttl: 2m}`; optional `product`, `ice` (default true), `motion` (default true),
`timeout` (seconds, default 10), `base_url`.
"""
from __future__ import annotations

import asyncio
import gzip
import logging
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import numpy as np

from .. import grib2, motion, radar, radarmap
from ..config import AppConfig, ProviderCfg
from ..readings import Reading
from .base import RequestContext, register

log = logging.getLogger(__name__)

BASE_URL = "https://mrms.ncep.noaa.gov/2D"
PRODUCT = "MergedBaseReflectivityQC"
MARGIN = 8.0  # map units of grid kept around the panel, so bilinear edges have neighbours
TEMP_REFRESH_S = 3600  # the surface temperature grid is hourly
TEMP_WARM_REFRESH_S = 6 * 3600
WARM_C = 4.0
MOTION_MARGIN = 80.0  # map units (~14 mi) of grid around the panel for the motion estimate
MOTION_MIN_PX = 400  # echo pixels in view (≈ 8 mi²) before motion is worth the extra downloads
MOTION_AGES_MIN = (30, 15)  # older frames, minutes before the latest
MOTION_SLACK_MIN = 4  # how far a listed file may be from the wanted time
LISTING_TTL_S = 90
OLD_CACHE = 8  # decoded older grids kept in memory
FILE_TIME = re.compile(r'href="(MRMS_[A-Za-z_]+_\d\d\.\d\d_(\d{8}-\d{6})\.grib2\.gz)"')

FLAG_TYPES = {1: radar.RAIN, 2: radar.RAIN, 6: radar.RAIN, 10: radar.RAIN, 91: radar.RAIN, 96: radar.RAIN,
              3: radar.SNOW, 4: radar.SNOW, 7: radar.HAIL}


def url(base: str, product: str) -> str:
    return f"{base}/{product}/MRMS_{product}.latest.grib2.gz"


def decode_crop(data: bytes, bbox: tuple[float, float, float, float]) -> grib2.Grid:
    return decode_crops(data, bbox)[0]


def decode_crops(data: bytes, *bboxes: tuple[float, float, float, float]) -> list[grib2.Grid]:
    raw = gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data
    g = grib2.decode(raw)
    return [g.crop(*b) for b in bboxes]


def listed(html: str) -> list[tuple[datetime, str]]:
    """(time, file name) of the timestamped files in an MRMS directory listing, oldest first."""
    out = {}
    for name, stamp in FILE_TIME.findall(html):
        out[name] = datetime.strptime(stamp, "%Y%m%d-%H%M%S").replace(tzinfo=UTC)
    return sorted((t, n) for n, t in out.items())


def nearest(files: list[tuple[datetime, str]], want: datetime, slack_min: float) -> str | None:
    best = min(files, key=lambda tn: abs(tn[0] - want), default=None)
    if best is None or abs(best[0] - want) > timedelta(minutes=slack_min):
        return None
    return best[1]


def motion_frame(refl: grib2.Grid, flag: grib2.Grid | None = None) -> motion.Frame:
    """A motion input on the reflectivity grid's cells (types only with a PrecipFlag, for snow lines)."""
    z = motion.dbz_array(refl.raw, refl.ni, refl.nj, refl.ref, refl.e, refl.d)
    types = None
    if flag is not None:
        _, tr = rasters(refl, flag, None)
        types = np.frombuffer(tr.data, dtype=np.uint8).reshape(refl.nj, refl.ni)
    return motion.Frame(refl.time, z, types)


def lattice(ext, g: grib2.Grid) -> motion.Lattice:
    return motion.Lattice.build(ext, g.lon1, g.lat1, g.di, g.dj, g.ni, g.nj)


def add_motion(frame: dict, latest: motion.Frame, older: list[motion.Frame], lat: motion.Lattice,
               home_xy: tuple[float, float], tz) -> dict:
    """Merge the motion analysis into a frame (in place): `motion`, `arrival`, `overlays`."""
    res = motion.analyze(latest, older, lat, home_xy, frame.get("legend", "intensity"), tz)
    if res:
        frame["motion"] = res.pop("motion")
        frame["arrival"] = res.pop("arrival")
        frame["overlays"] = res
    return frame


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
        self.product = str(opts.get("product", PRODUCT))
        self.ice = bool(opts.get("ice", True))
        self.timeout = float(opts.get("timeout", 10))
        self.base = str(opts.get("base_url", BASE_URL)).rstrip("/")
        self.user_agent = app.nws.user_agent
        self.home = app.location.name.split(",")[0]
        self.motion = bool(opts.get("motion", True))
        self.tz = ZoneInfo(app.location.tz)
        self.home_xy = radarmap.extent().project(app.location.lon, app.location.lat)
        self._listing: dict[str, tuple[float, list]] = {}  # product → (time.monotonic(), files)
        self._old: dict[str, grib2.Grid] = {}  # file name → grid in the motion window
        self._temp: grib2.Grid | None = None
        self._temp_at = 0.0  # time.monotonic() of the last attempt

    async def _get(self, client: httpx.AsyncClient, product: str, name: str | None = None) -> bytes:
        r = await client.get(f"{self.base}/{product}/{name}" if name else url(self.base, product))
        r.raise_for_status()
        return r.content

    async def _files(self, client: httpx.AsyncClient, product: str) -> list[tuple[datetime, str]]:
        hit = self._listing.get(product)
        if hit and time.monotonic() - hit[0] < LISTING_TTL_S:
            return hit[1]
        r = await client.get(f"{self.base}/{product}/")
        r.raise_for_status()
        files = listed(r.text)
        self._listing[product] = (time.monotonic(), files)
        return files

    async def _old_grid(self, client: httpx.AsyncClient, product: str, name: str, bbox) -> grib2.Grid:
        if name not in self._old:
            data = await self._get(client, product, name)
            self._old[name] = await asyncio.to_thread(decode_crop, data, bbox)
            while len(self._old) > OLD_CACHE:
                self._old.pop(next(iter(self._old)))
        return self._old[name]

    async def _older(self, client: httpx.AsyncClient, t: datetime, bbox, with_flag: bool) -> list[motion.Frame]:
        """The frames ~30 and ~15 min before `t` (with their PrecipFlag for a snow line)."""
        products = [self.product] + (["PrecipFlag"] if with_flag else [])
        listings = await asyncio.gather(*(self._files(client, p) for p in products))
        frames = []
        for age in MOTION_AGES_MIN:
            name = nearest(listings[0], t - timedelta(minutes=age), MOTION_SLACK_MIN)
            if name is None:
                continue
            g = await self._old_grid(client, self.product, name, bbox)
            f = None
            if with_flag:
                fname = nearest(listings[1], g.time, 2)
                if fname is None:
                    continue
                f = await self._old_grid(client, "PrecipFlag", fname, bbox)
            frames.append(await asyncio.to_thread(motion_frame, g, f))
        return frames

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
            mbox = ext.bbox(MOTION_MARGIN)
            refl, mrefl = await asyncio.to_thread(decode_crops, refl_data, bbox, mbox)
            flag = mflag = None
            if isinstance(flag_data, BaseException):
                log.warning("%s: PrecipFlag unavailable (%s); intensity only", self.name, flag_data)
            else:
                try:
                    flag, mflag = await asyncio.to_thread(decode_crops, flag_data, bbox, mbox)
                except (grib2.GribError, OSError, EOFError) as e:
                    log.warning("%s: PrecipFlag unreadable (%s); intensity only", self.name, e)
            temp = await self._surface_temp(client, bbox) if _has_liquid(refl, flag) else None
            frame = await asyncio.to_thread(build_frame, ext, refl, flag, temp, self.home)
            if self.motion and frame["echo_px"] >= MOTION_MIN_PX:
                try:
                    typed = frame["legend"] == "types" and mflag is not None
                    older = await self._older(client, refl.time, mbox, typed)
                    latest = await asyncio.to_thread(motion_frame, mrefl, mflag if typed else None)
                    await asyncio.to_thread(add_motion, frame, latest, older, lattice(ext, mrefl), self.home_xy,
                                            self.tz)
                except Exception as e:  # noqa: BLE001 - overlays are optional; never lose the frame over them
                    log.warning("%s: storm motion unavailable (%s: %s)", self.name, type(e).__name__, e)
        return {"radar.frame": Reading(value=frame, observed_at=refl.time, source=self.name)}


def build_frame(ext, refl: grib2.Grid, flag: grib2.Grid | None, temp: grib2.Grid | None,
                home: str) -> dict[str, Any]:
    zr, tr = rasters(refl, flag, temp)
    return radar.frame(ext, zr, tr, site="MRMS", time=refl.time, home=home)
