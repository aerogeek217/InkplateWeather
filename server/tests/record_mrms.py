"""Record MRMS GRIB2 files for tests: download, crop to the radar extent (plus a margin), save as small GRIB2.

    .venv/Scripts/python.exe tests/record_mrms.py <url-or-path> [...] [--out tests/data/recorded/mrms]

Sources: the live feed (https://mrms.ncep.noaa.gov/2D/<product>/MRMS_<product>.latest.grib2.gz) or the Iowa State
archive (https://mtarchive.geol.iastate.edu/YYYY/MM/DD/mrms/ncep/<product>/<product>_00.00_YYYYMMDD-HHMMSS.grib2.gz;
it keeps SeamlessHSR and PrecipFlag but not ReflectivityAtLowestAltitude). The output keeps the real values and
header; only the grid window shrinks, so the provider decodes it exactly like a full CONUS file.
"""
from __future__ import annotations

import argparse
import gzip
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import grib2, radarmap  # noqa: E402

OUT = Path(__file__).resolve().parent / "data" / "recorded" / "mrms"
MARGIN = 20.0  # map units around the viewBox


def load(src: str) -> bytes:
    if src.startswith("http"):
        req = urllib.request.Request(src, headers={"User-Agent": "InkplateWeather test recorder"})
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read()
    else:
        data = Path(src).read_bytes()
    return gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data


def record(src: str, out: Path) -> Path:
    grid = grib2.decode(load(src))
    sub = grid.crop(*radarmap.extent().bbox(MARGIN))
    name = (src.rsplit("/", 1)[-1] if src.startswith("http") else Path(src).name).removesuffix(".gz")
    name = name.removesuffix(".grib2")
    path = out / f"{name}.grib2"
    out.mkdir(parents=True, exist_ok=True)
    path.write_bytes(grib2.encode(sub))
    return path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("src", nargs="+")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()
    for s in args.src:
        p = record(s, args.out)
        print(f"{p} ({p.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
