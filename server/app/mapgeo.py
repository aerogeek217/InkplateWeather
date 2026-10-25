"""Radar map geometry: the display extent and the lon/lat ↔ map-unit projection.

Shared by the base map build script (`tools/build_basemap.py`), the radar providers (crop + reprojection) and the
radar partial, so the base map, the echoes and the overlays always line up.

The extent (layout v3, 2026-10-25): home at the origin but west of center, so storms moving east show earlier.
4.48 map units per mile (the 25 mi range ring has r = 112), viewBox −270.4…170.4 × −234…234, drawn at 1.25 px per
unit into the 551 × 585 px panel. That is about 60 mi west and 38 mi east of home, 52 mi north and south (98 × 104 mi;
v2 was 77 × 81 mi centered on home, 5.6 units per mile).

The projection is equirectangular around the center (x east, y south, in map units). Over ±0.8° the error against a
conformal projection is far below a pixel, and it keeps lon/lat grids (MRMS, IEM) affine in map space.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

UNITS_PER_MILE = 4.48
X_MIN, X_MAX = -270.4, 170.4  # map units; home is (0, 0)
Y_MIN, Y_MAX = -234.0, 234.0
PX_PER_UNIT = 1.25
WIDTH_PX = 551  # radar panel
HEIGHT_PX = 585
VIEWBOX = f"{X_MIN:g} {Y_MIN:g} {X_MAX - X_MIN:g} {Y_MAX - Y_MIN:g}"
MI_PER_DEG = 69.093  # mean earth radius 3958.8 mi × π/180
RING_MILES = 25


@dataclass(frozen=True)
class Extent:
    lat: float  # center (home)
    lon: float

    @property
    def _kx(self) -> float:  # map units per degree of longitude
        return MI_PER_DEG * math.cos(math.radians(self.lat)) * UNITS_PER_MILE

    @property
    def _ky(self) -> float:  # map units per degree of latitude
        return MI_PER_DEG * UNITS_PER_MILE

    def project(self, lon: float, lat: float) -> tuple[float, float]:
        """lon/lat → map units (x east, y south; home at 0, 0)."""
        return (lon - self.lon) * self._kx, (self.lat - lat) * self._ky

    def unproject(self, x: float, y: float) -> tuple[float, float]:
        """Map units → (lon, lat)."""
        return self.lon + x / self._kx, self.lat - y / self._ky

    def bbox(self, margin: float = 0.0) -> tuple[float, float, float, float]:
        """(west, south, east, north) in degrees covering the viewBox plus `margin` map units on every side."""
        w, n = self.unproject(X_MIN - margin, Y_MIN - margin)
        e, s = self.unproject(X_MAX + margin, Y_MAX + margin)
        return w, s, e, n

    def to_px(self, x: float, y: float) -> tuple[float, float]:
        """Map units → panel pixels (0, 0 at the top-left corner)."""
        return (x - X_MIN) * PX_PER_UNIT, (y - Y_MIN) * PX_PER_UNIT

    def px_to_lonlat_affine(self) -> tuple[float, float, float, float]:
        """(lon0, dlon/dpx, lat0, dlat/dpy): panel pixel centers → lon/lat, both axes independent.

        Pixel (i, j) has its center at lon0 + (i + 0.5)·dlon, lat0 + (j + 0.5)·dlat.
        """
        lon0, lat0 = self.unproject(X_MIN, Y_MIN)
        return lon0, 1 / (PX_PER_UNIT * self._kx), lat0, -1 / (PX_PER_UNIT * self._ky)


def inside(x: float, y: float, inset: float = 0.0) -> bool:
    """(x, y) lies in the viewBox, at least `inset` map units from every edge."""
    return X_MIN + inset <= x <= X_MAX - inset and Y_MIN + inset <= y <= Y_MAX - inset


def miles(units: float) -> float:
    return units / UNITS_PER_MILE
