"""Radar frame: reflectivity (+ precipitation type) on a lon/lat grid → classified masks for the radar panel.

Shared by `radar_mrms` and `radar_iem`. The input rasters are reprojected onto the 536 × 570 px panel
(`app.mapgeo`; equirectangular, so a lon/lat grid is one affine transform): reflectivity bilinearly (smooth echo
edges from the 1 km grid), precipitation type by nearest cell. Each echo class becomes a 1-bit mask PNG that the
radar partial fills with the design's grays and patterns (SPEC *Radar*, icon sheet `fills/`):

- intensity legend (no frozen precipitation in view): light / moderate / heavy / extreme by dBZ (MRMS's "rain
  mixed with hail" flag covers far more than the hail cores, so extreme comes from dBZ alone)
- types legend (snow or ice in view): rain one gray, ice/sleet hatched, snow dotted, heavy snow dots on gray

The frame is a plain dict (JSON, so the cache can persist it as last good):

    {"site": "MRMS", "time": iso, "legend": "intensity"|"types", "types": ["rain", ...],
     "layers": [{"fill": "rain-light", "png": "<base64 1-bit PNG>"}, ...], "echo_px": int, "aria_label": str}
"""
from __future__ import annotations

import base64
import io
from dataclasses import dataclass
from datetime import datetime

from PIL import Image, ImageChops

from .mapgeo import HEIGHT_PX, WIDTH_PX, Extent

# dBZ thresholds: light ≥ 15, moderate ≥ 30, heavy ≥ 40, extreme ≥ 50 (NWS-style rain intensity); snow shows from
# 10 dBZ (light snow is weak on radar) and is "heavy" from 25.
LIGHT, MODERATE, HEAVY, EXTREME = 15.0, 30.0, 40.0, 50.0
SNOW_MIN, SNOW_HEAVY = 10.0, 25.0
MIN_TYPED_PX = 100  # snow/ice pixels needed (≈ 2 mi²) before the legend switches to Rain / Ice / Snow

# Precipitation type codes in the type raster.
NONE, RAIN, SNOW, ICE, HAIL = 0, 1, 2, 3, 4
TYPE_NAMES = {RAIN: "rain", SNOW: "snow", ICE: "ice", HAIL: "rain"}


def enc(dbz: float | None) -> int:
    """dBZ → 8-bit code (0 = no echo / no data; 1…255 = −32…95 dBZ in ½ dB steps)."""
    if dbz is None or dbz < -31.5:
        return 0
    return max(1, min(255, round((dbz + 32) * 2)))


@dataclass
class Raster:
    """An 8-bit raster on a regular lon/lat grid; (lon1, lat1) is the center of the north-west cell."""

    ni: int
    nj: int
    lon1: float
    lat1: float
    di: float
    dj: float
    data: bytes  # ni × nj, row-major, north row first

    def to_panel(self, ext: Extent, resample: int) -> Image.Image:
        img = Image.frombytes("L", (self.ni, self.nj), self.data)
        lon0, dlon, lat0, dlat = ext.px_to_lonlat_affine()
        # panel pixel coordinate x (continuous, centers at i + ½) → raster column coordinate u (cell i covers [i, i+1))
        a = dlon / self.di
        c = (lon0 - (self.lon1 - self.di / 2)) / self.di
        e = -dlat / self.dj
        f = ((self.lat1 + self.dj / 2) - lat0) / self.dj
        return img.transform((WIDTH_PX, HEIGHT_PX), Image.AFFINE, (a, 0, c, 0, e, f), resample=resample,
                             fillcolor=0)


def _band(img: Image.Image, lo: float, hi: float | None = None) -> Image.Image:
    """1-bit mask of enc(lo) ≤ v < enc(hi)."""
    a, b = enc(lo), enc(hi) if hi is not None else 256
    return img.point([255 if a <= v < b else 0 for v in range(256)], "1")


def _eq(img: Image.Image, *codes: int) -> Image.Image:
    return img.point([255 if v in codes else 0 for v in range(256)], "1")


def _count(mask: Image.Image) -> int:
    return mask.histogram()[255]


def _png(mask: Image.Image) -> str:
    buf = io.BytesIO()
    mask.save(buf, "PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def classify(ext: Extent, refl: Raster, ptype: Raster | None = None) -> tuple[str, list[tuple[str, Image.Image]]]:
    """→ (legend, [(fill class, 1-bit mask), ...]) in drawing order; empty masks are left out."""
    z = refl.to_panel(ext, Image.BILINEAR)
    t = ptype.to_panel(ext, Image.NEAREST) if ptype is not None else None
    snow = ice = None
    if t is not None:
        snow = ImageChops.logical_and(_eq(t, SNOW), _band(z, SNOW_MIN))
        ice = ImageChops.logical_and(_eq(t, ICE), _band(z, LIGHT))
    typed = t is not None and _count(snow) + _count(ice) >= MIN_TYPED_PX
    out: list[tuple[str, Image.Image]] = []
    if typed:
        liquid = ImageChops.logical_and(_band(z, LIGHT), _eq(t, NONE, RAIN, HAIL))
        out = [("rain", liquid), ("ice", ice),
               ("snow", ImageChops.logical_and(snow, _band(z, SNOW_MIN, SNOW_HEAVY))),
               ("snow-heavy", ImageChops.logical_and(snow, _band(z, SNOW_HEAVY)))]
    else:
        out = [("rain-light", _band(z, LIGHT, MODERATE)), ("rain-moderate", _band(z, MODERATE, HEAVY)),
               ("rain-heavy", _band(z, HEAVY, EXTREME)), ("rain-extreme", _band(z, EXTREME))]
    return ("types" if typed else "intensity"), [(name, m) for name, m in out if _count(m)]


def frame(ext: Extent, refl: Raster, ptype: Raster | None, *, site: str, time: datetime,
          home: str = "home") -> dict:
    legend, masks = classify(ext, refl, ptype)
    types: set[str] = set()
    for name, _ in masks:
        types.add(name.split("-")[0])
    if legend == "intensity":
        types = {"rain"} if masks else set()
    echo_px = sum(_count(m) for _, m in masks)
    return {"site": site, "time": time.isoformat(), "legend": legend, "types": sorted(types),
            "layers": [{"fill": name, "png": _png(m)} for name, m in masks], "echo_px": echo_px,
            "aria_label": aria_label(types, echo_px, home)}


def aria_label(types: set[str], echo_px: int, home: str) -> str:
    if not echo_px:
        return f"Radar map around {home}: no precipitation in view"
    names = [n for n in ("rain", "ice", "snow") if n in types]
    words = {"rain": "rain", "ice": "ice or sleet", "snow": "snow"}
    listed = " and ".join(words[n] for n in names) if len(names) < 3 else "rain, ice or sleet, and snow"
    return f"Radar map around {home}: {listed} in view"
