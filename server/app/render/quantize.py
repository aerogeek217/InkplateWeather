"""Quantize a screenshot to the Inkplate 10's 8 gray levels (same LUT as design/tools/render_reference.py)."""
from __future__ import annotations

import io

from PIL import Image

LEVELS = [round(i * 255 / 7) for i in range(8)]  # 0, 36, 73, 109, 146, 182, 219, 255
LUT = [min(LEVELS, key=lambda v, i=i: abs(v - i)) for i in range(256)]


def quantize(img: Image.Image) -> Image.Image:
    """Mode-L image whose pixels use only LEVELS (nearest level; ties go to the darker one)."""
    return img.convert("L").point(LUT)


def quantize_png(png: bytes) -> bytes:
    with Image.open(io.BytesIO(png)) as img:
        out = quantize(img)
    buf = io.BytesIO()
    out.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
