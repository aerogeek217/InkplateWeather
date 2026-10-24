"""Build the radar test boards from the fall/winter fixtures and the recorded MRMS files.

    .venv/Scripts/python.exe tests/build_radar_fixtures.py

- `fixtures/radar-storm.json`: fall.json with the MRMS frame of 2023-04-01 01:00 UTC (a severe line through
  Champaign County), intensity legend
- `fixtures/radar-winter.json`: winter.json with the MRMS frame of 2022-02-02 09:00 UTC (snow north, rain south)
  and a surface temperature grid set to ≤ 0 °C north of 39.86° N, so the rain band nearest the snow shows as ice

The frame keeps the fixture's own radar time, so the legend reads like the board ("MRMS 2:25 PM").
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import grib2, radarmap  # noqa: E402
from app.providers import radar_mrms  # noqa: E402

SERVER = Path(__file__).resolve().parents[1]
MRMS = SERVER / "tests" / "data" / "recorded" / "mrms"
FIXTURES = SERVER / "fixtures"


def crop(name: str) -> grib2.Grid:
    return radar_mrms.decode_crop((MRMS / name).read_bytes(), radarmap.extent().bbox(radar_mrms.MARGIN))


def cold(g: grib2.Grid, lat_line: float = 39.86) -> grib2.Grid:
    g.raw = [round(((-2.0 if g.lonlat(i, j)[1] > lat_line else 3.0) * 10 ** g.d - g.ref) / 2 ** g.e)
             for j in range(g.nj) for i in range(g.ni)]
    return g


def build(base: str, out: str, refl: str, flag: str, temp: grib2.Grid | None, recorded: str) -> None:
    data = json.loads((FIXTURES / f"{base}.json").read_text(encoding="utf-8"))
    entry = data["readings"]["radar.frame"]
    t = datetime.fromisoformat(entry["value"]["time"])
    frame = radar_mrms.build_frame(radarmap.extent(), crop(refl), crop(flag), temp, "Savoy")
    frame["time"] = t.isoformat()
    frame["recorded"] = recorded
    data["readings"]["radar.frame"] = {"value": frame, "observed_at": entry["observed_at"]}
    path = FIXTURES / f"{out}.json"
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    print(path, frame["legend"], frame["types"], [x["fill"] for x in frame["layers"]])


def main() -> None:
    build("fall", "radar-storm", "SeamlessHSR_00.00_20230401-010000.grib2", "PrecipFlag_00.00_20230401-010000.grib2",
          None, "MRMS SeamlessHSR + PrecipFlag 2023-04-01T01:00Z")
    build("winter", "radar-winter", "SeamlessHSR_00.00_20220202-090000.grib2",
          "PrecipFlag_00.00_20220202-090000.grib2", cold(crop("MRMS_Model_SurfaceTemp_00.50_20261024-170000.grib2")),
          "MRMS SeamlessHSR + PrecipFlag 2022-02-02T09:00Z; surface temperature synthetic (<= 0 C north of 39.86 N)")


if __name__ == "__main__":
    main()
