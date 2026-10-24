"""Minimal GRIB2 reader for NOAA MRMS 2D products.

MRMS files are single-message GRIB2 on a regular lat/lon grid (template 3.0, 0.01°, scanning west→east,
north→south) with PNG packing (template 5.41), so Pillow decodes the payload and no eccodes is needed.
Anything else raises `GribError`.

    grid = decode(gzip.decompress(data))
    sub = grid.crop(west, south, east, north)       # small window, values in physical units
    sub.value(i, j)                                 # float, or None outside coverage
"""
from __future__ import annotations

import io
import math
import struct
from dataclasses import dataclass, field
from datetime import UTC, datetime

from PIL import Image


class GribError(ValueError):
    pass


@dataclass
class Grid:
    time: datetime  # reference time (UTC)
    category: int  # product category / parameter (section 4), e.g. 3/57 reflectivity, 6/0 precip flag
    parameter: int
    ni: int  # columns (west → east)
    nj: int  # rows (north → south)
    lat1: float  # center of the first (north-west) cell
    lon1: float  # −180…180
    di: float  # degrees per column
    dj: float  # degrees per row
    ref: float  # packing: value = (ref + X · 2^E) / 10^D
    e: int
    d: int
    raw: list[int] = field(repr=False, default_factory=list)  # X, row-major (only after crop)
    image: Image.Image | None = field(repr=False, default=None)  # full packed image (before crop)

    def scale(self, x: int) -> float:
        return (self.ref + x * 2.0 ** self.e) / 10.0 ** self.d

    def crop(self, west: float, south: float, east: float, north: float, pad: int = 1) -> Grid:
        """The cells covering the box (plus `pad` cells), decoded to raw values."""
        if self.image is None:
            raise GribError("grid already cropped")
        c0 = max(0, math.floor((west - self.lon1) / self.di) - pad)
        c1 = min(self.ni, math.ceil((east - self.lon1) / self.di) + 1 + pad)
        r0 = max(0, math.floor((self.lat1 - north) / self.dj) - pad)
        r1 = min(self.nj, math.ceil((self.lat1 - south) / self.dj) + 1 + pad)
        if c0 >= c1 or r0 >= r1:
            raise GribError("box outside the grid")
        sub = self.image.crop((c0, r0, c1, r1))
        if sub.mode == "RGB":  # 24-bit values come as 3 × 8-bit channels
            raw = [r << 16 | g << 8 | b for r, g, b in _pixels(sub)]
        else:
            raw = list(_pixels(sub))
        return Grid(self.time, self.category, self.parameter, c1 - c0, r1 - r0, self.lat1 - r0 * self.dj,
                    self.lon1 + c0 * self.di, self.di, self.dj, self.ref, self.e, self.d, raw=raw)

    def value(self, i: int, j: int) -> float:
        return self.scale(self.raw[j * self.ni + i])

    def lonlat(self, i: float, j: float) -> tuple[float, float]:
        return self.lon1 + i * self.di, self.lat1 - j * self.dj


def _pixels(img: Image.Image):
    get = getattr(img, "get_flattened_data", None)  # Pillow ≥ 12.1; getdata is deprecated
    return get() if get else img.getdata()


def _signed(hi: int, lo: int) -> int:
    """GRIB2 sign-magnitude 16-bit integer."""
    v = (hi & 0x7F) << 8 | lo
    return -v if hi & 0x80 else v


def _int4(b: bytes) -> int:
    v = struct.unpack(">I", b)[0]
    return -(v & 0x7FFFFFFF) if v & 0x80000000 else v


def decode(data: bytes) -> Grid:
    """Decode the first message of a GRIB2 file (already gunzipped)."""
    if data[:4] != b"GRIB" or data[7] != 2:
        raise GribError("not a GRIB2 file")
    off = 16
    time = cat = param = None
    geom = pack = None
    png = None
    while off < len(data) - 4:
        if data[off:off + 4] == b"7777":
            break
        length, num = struct.unpack(">IB", data[off:off + 5])
        sec = data[off:off + length]
        if num == 1:
            y, = struct.unpack(">H", sec[12:14])
            time = datetime(y, sec[14], sec[15], sec[16], sec[17], sec[18], tzinfo=UTC)
        elif num == 3:
            tmpl, = struct.unpack(">H", sec[12:14])
            if tmpl != 0:
                raise GribError(f"grid template 3.{tmpl} not supported")
            ni, nj = struct.unpack(">II", sec[30:38])
            la1, lo1 = _int4(sec[46:50]) / 1e6, _int4(sec[50:54]) / 1e6
            di, dj = struct.unpack(">II", sec[63:71])
            if sec[71] & 0xE0:
                raise GribError(f"scanning mode {sec[71]:08b} not supported")
            geom = (ni, nj, la1, (lo1 + 180) % 360 - 180, di / 1e6, dj / 1e6)
        elif num == 4:
            cat, param = sec[9], sec[10]
        elif num == 5:
            tmpl, = struct.unpack(">H", sec[9:11])
            if tmpl != 41:
                raise GribError(f"data template 5.{tmpl} not supported (PNG packing only)")
            ref, = struct.unpack(">f", sec[11:15])
            pack = (ref, _signed(sec[15], sec[16]), _signed(sec[17], sec[18]))
        elif num == 6:
            if sec[5] != 255:
                raise GribError("bitmaps not supported")
        elif num == 7:
            png = sec[5:]
        off += length
    if time is None or geom is None or pack is None or png is None:
        raise GribError("incomplete GRIB2 message")
    img = Image.open(io.BytesIO(png))
    img.load()
    ni, nj, la1, lo1, di, dj = geom
    if img.size != (ni, nj):
        raise GribError(f"PNG is {img.size}, grid is {ni}×{nj}")
    return Grid(time, cat if cat is not None else -1, param if param is not None else -1, ni, nj, la1, lo1, di, dj,
                *pack, image=img)


def encode(grid: Grid) -> bytes:
    """Write a cropped grid back as a GRIB2 message (5.41 PNG packing): used to record small test files."""
    if grid.image is not None or not grid.raw:
        raise GribError("encode needs a cropped grid")
    peak = max(grid.raw)
    if peak < 256:
        img = Image.frombytes("L", (grid.ni, grid.nj), bytes(grid.raw))
        bits = 8
    elif peak < 65536:
        img = Image.new("I;16", (grid.ni, grid.nj))
        img.frombytes(struct.pack(f"<{len(grid.raw)}H", *grid.raw), "raw", "I;16")
        bits = 16
    else:
        img = Image.frombytes("RGB", (grid.ni, grid.nj),
                              b"".join(struct.pack(">I", v)[1:] for v in grid.raw))
        bits = 24
    buf = io.BytesIO()
    img.save(buf, "PNG")
    png = buf.getvalue()

    def s4(v: int) -> bytes:
        return struct.pack(">I", (abs(v) | 0x80000000) if v < 0 else v)

    def s2(v: int) -> bytes:
        return struct.pack(">H", (abs(v) | 0x8000) if v < 0 else v)

    t = grid.time
    sec1 = struct.pack(">IBHHBBBHBBBBBBBB", 21, 1, 161, 0, 2, 1, 1, t.year, t.month, t.day, t.hour, t.minute,
                       t.second, 0, 0, 0)[:21]
    la2 = grid.lat1 - (grid.nj - 1) * grid.dj
    lo2 = grid.lon1 + (grid.ni - 1) * grid.di
    sec3 = (struct.pack(">IBBIBBH", 72, 3, 0, grid.ni * grid.nj, 0, 0, 0)
            + bytes([6, 0]) + b"\0" * 4 + bytes([0]) + b"\0" * 4 + bytes([0]) + b"\0" * 4
            + struct.pack(">II", grid.ni, grid.nj) + b"\0" * 8
            + s4(round(grid.lat1 * 1e6)) + s4(round((grid.lon1 % 360) * 1e6)) + bytes([48])
            + s4(round(la2 * 1e6)) + s4(round((lo2 % 360) * 1e6))
            + struct.pack(">II", round(grid.di * 1e6), round(grid.dj * 1e6)) + bytes([0]))
    sec4 = struct.pack(">IBHH", 34, 4, 0, 0) + bytes([grid.category, grid.parameter]) + b"\0" * 23
    sec5 = (struct.pack(">IBIH", 21, 5, grid.ni * grid.nj, 41) + struct.pack(">f", grid.ref)
            + s2(grid.e) + s2(grid.d) + bytes([bits, 0]))
    sec6 = struct.pack(">IBB", 6, 6, 255)
    sec7 = struct.pack(">IB", 5 + len(png), 7) + png
    body = sec1 + sec3 + sec4 + sec5 + sec6 + sec7 + b"7777"
    return b"GRIB" + b"\0\0" + bytes([209, 2]) + struct.pack(">Q", 16 + len(body)) + body
