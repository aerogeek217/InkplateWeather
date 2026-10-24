"""`radar_iem` provider: the Iowa Environmental Mesonet NEXRAD base reflectivity mosaic → `radar.frame` (fallback).

Fetches (concurrently) from `https://mesonet.agron.iastate.edu/data/gis/images/4326/USCOMP/`:

- `n0q_0.png`: CONUS mosaic, palette PNG on a 0.005° lon/lat grid (~1.3 MB, every 5 min). The palette index *is*
  the value: dBZ = index / 2 − 32.5, 0 = no echo, so no color decoding is needed.
- `n0q_0.wld`: the world file (pixel size and the upper-left pixel center)
- `n0q_0.json`: `meta.valid`, the mosaic time

No precipitation type, so the frame always uses the intensity legend. Any failure raises (the resolver falls back
or shows the radar as missing).

Config: `iem: {type: radar_iem, ttl: 5m}`; optional `timeout` (seconds, default 10), `base_url`.
"""
from __future__ import annotations

import asyncio
import io
import math
from datetime import datetime
from typing import Any

import httpx
from PIL import Image

from .. import radar, radarmap
from ..config import AppConfig, ProviderCfg
from ..readings import Reading
from .base import RequestContext, register

BASE_URL = "https://mesonet.agron.iastate.edu/data/gis/images/4326/USCOMP"
MARGIN = 8.0  # map units


class IemError(ValueError):
    pass


def world(text: str) -> tuple[float, float, float, float]:
    """World file → (dx, dy, x0, y0): pixel size and the upper-left pixel's center (no rotation)."""
    v = [float(x) for x in text.split()]
    if len(v) != 6 or v[1] or v[2] or v[0] <= 0 or v[3] >= 0:
        raise IemError(f"unexpected world file: {text!r}")
    return v[0], v[3], v[4], v[5]


def raster(png: bytes, wld: str, bbox: tuple[float, float, float, float]) -> radar.Raster:
    dx, dy, x0, y0 = world(wld)
    img = Image.open(io.BytesIO(png))
    if img.mode != "P":
        raise IemError(f"expected a palette PNG, got {img.mode}")
    w, s, e, n = bbox
    c0 = max(0, math.floor((w - x0) / dx) - 1)
    c1 = min(img.width, math.ceil((e - x0) / dx) + 2)
    r0 = max(0, math.floor((n - y0) / dy) - 1)
    r1 = min(img.height, math.ceil((s - y0) / dy) + 2)
    if c0 >= c1 or r0 >= r1:
        raise IemError("extent outside the mosaic")
    sub = img.crop((c0, r0, c1, r1))
    get = getattr(sub, "get_flattened_data", None)
    idx = get() if get else sub.getdata()
    data = bytes(radar.enc(i / 2 - 32.5) if i else 0 for i in idx)
    return radar.Raster(c1 - c0, r1 - r0, x0 + c0 * dx, y0 + r0 * dy, dx, -dy, data)


def valid_time(meta: Any) -> datetime:
    try:
        return datetime.fromisoformat(meta["meta"]["valid"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise IemError(f"no mosaic time: {e}") from e


@register("radar_iem")
class IemProvider:
    def __init__(self, name: str, cfg: ProviderCfg, app: AppConfig):
        self.name = name
        self.ttl = cfg.ttl
        opts = cfg.options
        self.timeout = float(opts.get("timeout", 10))
        self.base = str(opts.get("base_url", BASE_URL)).rstrip("/")
        self.user_agent = app.nws.user_agent
        self.home = app.location.name.split(",")[0]

    async def fetch(self, ctx: RequestContext) -> dict[str, Reading]:
        ext = radarmap.extent()
        async with httpx.AsyncClient(timeout=self.timeout, headers={"User-Agent": self.user_agent},
                                     follow_redirects=True) as client:
            png, wld, meta = await asyncio.gather(*(client.get(f"{self.base}/n0q_0.{x}")
                                                    for x in ("png", "wld", "json")))
        for r in (png, wld, meta):
            r.raise_for_status()
        t = valid_time(meta.json())
        zr = await asyncio.to_thread(raster, png.content, wld.text, ext.bbox(MARGIN))
        frame = await asyncio.to_thread(radar.frame, ext, zr, None, site="NEXRAD", time=t, home=self.home)
        return {"radar.frame": Reading(value=frame, observed_at=t, source=self.name)}
