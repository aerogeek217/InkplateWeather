"""Record an IEM n0q mosaic for tests: crop the palette PNG to the radar extent and write PNG + world file + JSON.

    .venv/Scripts/python.exe tests/record_iem.py <png-url> <valid-time-iso> [--out tests/data/recorded/iem]

Archive: https://mesonet.agron.iastate.edu/archive/data/YYYY/MM/DD/GIS/uscomp/n0q_YYYYMMDDHHMM.png (+ .wld).
Live: https://mesonet.agron.iastate.edu/data/gis/images/4326/USCOMP/n0q_0.png (+ .wld, .json).
"""
from __future__ import annotations

import argparse
import io
import json
import math
import sys
import urllib.request
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import radarmap  # noqa: E402
from app.providers.radar_iem import world  # noqa: E402

OUT = Path(__file__).resolve().parent / "data" / "recorded" / "iem"
MARGIN = 20.0


def get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "InkplateWeather test recorder"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("png_url")
    ap.add_argument("valid")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()
    dx, dy, x0, y0 = world(get(args.png_url.removesuffix(".png") + ".wld").decode())
    img = Image.open(io.BytesIO(get(args.png_url)))
    w, s, e, n = radarmap.extent().bbox(MARGIN)
    c0, c1 = math.floor((w - x0) / dx), math.ceil((e - x0) / dx) + 1
    r0, r1 = math.floor((n - y0) / dy), math.ceil((s - y0) / dy) + 1
    stem = args.out / Path(args.png_url).stem
    args.out.mkdir(parents=True, exist_ok=True)
    img.crop((c0, r0, c1, r1)).save(stem.with_suffix(".png"))  # no optimize: it would renumber the palette
    stem.with_suffix(".wld").write_text(f"{dx}\n0.0\n0.0\n{dy}\n{x0 + c0 * dx:.4f}\n{y0 + r0 * dy:.4f}\n",
                                        newline="\n")
    stem.with_suffix(".json").write_text(json.dumps({"meta": {"product": "N0Q", "site": "USCOMP",
                                                              "valid": args.valid}}) + "\n", newline="\n")
    print(stem, "png/wld/json")


if __name__ == "__main__":
    main()
