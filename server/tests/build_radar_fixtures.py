"""Build the radar test boards from the fall/winter fixtures and the recorded MRMS files.

    .venv/Scripts/python.exe tests/build_radar_fixtures.py

- `fixtures/radar-storm.json`: fall.json with the MRMS frame of 2023-04-01 01:00 UTC (a severe line through
  Champaign County), intensity legend
- `fixtures/radar-winter.json`: winter.json with the MRMS frame of 2022-02-02 09:00 UTC (snow north, rain south)
  and a surface temperature grid set to ≤ 0 °C north of 39.86° N, so the rain band nearest the snow shows as ice

Storm motion (phase 4.1), recorded with `--margin 80` in `mrms/motion/`:

- `fixtures/radar-motion.json`: fall.json with the MRMS frame of 2023-04-01 00:06 UTC (the same line, just west of
  Savoy). That line grew too fast for a clean motion estimate (the frames disagree, so the live provider would draw
  nothing), so the two older frames are the same field shifted back along 15 mph toward ENE (the fall board's
  motion): synthetic motion, real echoes.
- `fixtures/radar-snowline.json`: winter.json with the MRMS frame of 2022-02-02 09:00 UTC. Measured over the same
  window in each frame, that snow line was nearly stationary (≤ 1 mph), so the two older frames are the same field
  shifted back along 4 mph toward SE: synthetic motion, real echoes.

Both drop the fixture's callout text, so the radar-timed callout shows. The frame keeps the fixture's own radar time,
so the legend reads like the board ("MRMS 2:25 PM"); the older frames keep their offsets from it.

Fixture boards (layout v3): the radar art of the fall and winter boards (`fixtures/radar/fall.svg`, `winter.svg`,
also used by the alert, stale, battery and live-winter fixtures) is the generated radar of `radar-motion` and
`radar-snowline`, and those fixtures take its site, types, description and arrival; the ones with a fixed callout
get the radar-timed callout text, so text and map agree. The design boards (`design/source/dashboard*.html`) carry
the same art (`design/tools/layout_v3.py`).
"""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import grib2, motion, radarmap, summary  # noqa: E402
from app.providers import radar_mrms  # noqa: E402

SERVER = Path(__file__).resolve().parents[1]
MRMS = SERVER / "tests" / "data" / "recorded" / "mrms"
FIXTURES = SERVER / "fixtures"
TZ = ZoneInfo("America/Chicago")
HOME = (40.0617, -88.2517)


def crop(name: str, margin: float = radar_mrms.MARGIN) -> grib2.Grid:
    return radar_mrms.decode_crop((MRMS / name).read_bytes(), radarmap.extent().bbox(margin))


def shifted(f: motion.Frame, lat: motion.Lattice, mph: float, bearing: float, minutes: float) -> motion.Frame:
    """The field (and its types) as it was `minutes` earlier if it moved at `mph` toward `bearing` (whole cells; new
    edges empty)."""
    d = mph * radarmap.UNITS_PER_MILE * minutes / 60
    di = -round(d * math.sin(math.radians(bearing)) / lat.cell_w)
    dj = -round(-d * math.cos(math.radians(bearing)) / lat.cell_h)

    def move(a: np.ndarray, empty) -> np.ndarray:
        a = np.roll(np.roll(a, dj, 0), di, 1)
        if dj > 0:
            a[:dj, :] = empty
        elif dj < 0:
            a[dj:, :] = empty
        if di > 0:
            a[:, :di] = empty
        elif di < 0:
            a[:, di:] = empty
        return a

    types = None if f.types is None else move(f.types, 0)
    return motion.Frame(f.t - timedelta(minutes=minutes), move(f.dbz, np.nan), types)


def build_motion(base: str, out: str, refl: str, flag: str, older, temp: grib2.Grid | None, recorded: str) -> None:
    """`older`: [(file name, flag name)] of real older frames, or ("synthetic", mph, bearing, [minutes, ...])."""
    data = json.loads((FIXTURES / f"{base}.json").read_text(encoding="utf-8"))
    entry = data["readings"]["radar.frame"]
    t = datetime.fromisoformat(entry["value"]["time"]).astimezone(TZ)
    ext = radarmap.extent()
    frame = radar_mrms.build_frame(ext, crop(f"motion/{refl}"), crop(f"motion/{flag}"), temp, "Savoy")
    mrefl, mflag = crop(f"motion/{refl}", radar_mrms.MOTION_MARGIN), crop(f"motion/{flag}", radar_mrms.MOTION_MARGIN)
    typed = frame["legend"] == "types"
    latest = radar_mrms.motion_frame(mrefl, mflag if typed else None)
    lat = radar_mrms.lattice(ext, mrefl)
    if older[0] == "synthetic":
        _, mph, bearing, ages = older
        olds = [shifted(latest, lat, mph, bearing, a) for a in ages]
    else:
        olds = [radar_mrms.motion_frame(crop(f"motion/{r}", radar_mrms.MOTION_MARGIN),
                                        crop(f"motion/{f}", radar_mrms.MOTION_MARGIN) if typed else None)
                for r, f in older]
    # move the whole set onto the fixture's clock, keeping the real gaps between the frames
    offset = t - latest.t
    latest.t = t
    for o in olds:
        o.t = o.t + offset
    radar_mrms.add_motion(frame, latest, olds, lat, ext.project(HOME[1], HOME[0]), TZ)
    frame["time"] = t.isoformat()
    frame["recorded"] = recorded
    data["readings"]["radar.frame"] = {"value": frame, "observed_at": entry["observed_at"]}
    data["readings"].pop("radar.callout", None)
    path = FIXTURES / f"{out}.json"
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    ov = frame.get("overlays") or {}
    print(path, frame["legend"], frame.get("motion"), frame.get("arrival"),
          [a["t"] for a in ov.get("arrivals", [])], [s["t"] for s in ov.get("snow_lines", [])])


def cold(g: grib2.Grid, lat_line: float = 39.86) -> grib2.Grid:
    g.raw = [round(((-2.0 if g.lonlat(i, j)[1] > lat_line else 3.0) * 10 ** g.d - g.ref) / 2 ** g.e)
             for j in range(g.nj) for i in range(g.ni)]
    return g


# The fixed callouts of the radar-storm / radar-winter boards (the v2 fall and winter boards' text, which fits them).
STORM_CALLOUT = {"kind": "storm", "headline": "Storms ~4 PM", "detail": "22 mi west · ENE 15 mph · ~0.5 in"}
WINTER_CALLOUT = {"kind": "snow", "headline": "Snow by 6 PM", "detail": "Sleet now · heaviest 7–11 PM · 3–5 in"}


def build(base: str, out: str, refl: str, flag: str, temp: grib2.Grid | None, recorded: str,
          callout: dict | None = None) -> None:
    data = json.loads((FIXTURES / f"{base}.json").read_text(encoding="utf-8"))
    entry = data["readings"]["radar.frame"]
    t = datetime.fromisoformat(entry["value"]["time"])
    frame = radar_mrms.build_frame(radarmap.extent(), crop(refl), crop(flag), temp, "Savoy")
    frame["time"] = t.isoformat()
    frame["recorded"] = recorded
    data["readings"]["radar.frame"] = {"value": frame, "observed_at": entry["observed_at"]}
    if callout:
        data["readings"]["radar.callout"] = callout
    path = FIXTURES / f"{out}.json"
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    print(path, frame["legend"], frame["types"], [x["fill"] for x in frame["layers"]])


def artwork(art: str, src: str, boards: list[str]) -> None:
    """`src`'s generated radar (at its own clock) → fixtures/radar/<art>.svg; the boards drawn with it take the frame's
    site, types, legend, description and arrival, and a fixed callout becomes the radar-timed one."""
    data = json.loads((FIXTURES / f"{src}.json").read_text(encoding="utf-8"))
    frame = data["readings"]["radar.frame"]["value"]
    ext = radarmap.extent()
    svg = radarmap.layers(frame, "SAVOY", ext.project(HOME[1], HOME[0]), [], now=datetime.fromisoformat(data["now"]))
    path = FIXTURES / "radar" / f"{art}.svg"
    path.write_text(f"<!-- Radar art of the {art} fixture boards: the generated radar of fixtures/{src}.json "
                    "(tests/build_radar_fixtures.py). -->\n" + svg + "\n", encoding="utf-8", newline="\n")
    for name in boards:
        p = FIXTURES / f"{name}.json"
        d = json.loads(p.read_text(encoding="utf-8"))
        v = d["readings"]["radar.frame"]["value"]
        v.update({k: frame[k] for k in ("site", "types", "legend", "aria_label")})
        v.pop("arrival", None)
        if frame.get("arrival"):
            v["arrival"] = frame["arrival"]
        r = d["readings"]
        if r.get("radar.callout"):
            now = datetime.fromisoformat(d["now"]).astimezone(TZ)
            val = {k: (x["value"] if isinstance(x, dict) and "observed_at" in x else x) for k, x in r.items()}
            inp = summary.Inputs.build(now, TZ, val.get("forecast.hourly"), val.get("forecast.daily"),
                                       val.get("conditions.current"), val.get("alerts.active"))
            r["radar.callout"] = summary.callout(inp, frame.get("arrival"))
        p.write_text(json.dumps(d, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    print(path, boards)


def main() -> None:
    build("fall", "radar-storm", "SeamlessHSR_00.00_20230401-010000.grib2", "PrecipFlag_00.00_20230401-010000.grib2",
          None, "MRMS SeamlessHSR + PrecipFlag 2023-04-01T01:00Z", STORM_CALLOUT)
    build("winter", "radar-winter", "SeamlessHSR_00.00_20220202-090000.grib2",
          "PrecipFlag_00.00_20220202-090000.grib2", cold(crop("MRMS_Model_SurfaceTemp_00.50_20261024-170000.grib2")),
          "MRMS SeamlessHSR + PrecipFlag 2022-02-02T09:00Z; surface temperature synthetic (<= 0 C north of 39.86 N)",
          WINTER_CALLOUT)
    build_motion("fall", "radar-motion", "SeamlessHSR_00.00_20230401-000600.grib2",
                 "PrecipFlag_00.00_20230401-000600.grib2", ("synthetic", 15.0, 67.5, [30, 15]), None,
                 "MRMS SeamlessHSR + PrecipFlag 2023-04-01T00:06Z; older frames synthetic (shifted back along "
                 "15 mph toward ENE)")
    build_motion("winter", "radar-snowline", "SeamlessHSR_00.00_20220202-090000.grib2",
                 "PrecipFlag_00.00_20220202-090000.grib2",
                 ("synthetic", 4.0, 135.0, [30, 14]), cold(crop("MRMS_Model_SurfaceTemp_00.50_20261024-170000.grib2")),
                 "MRMS SeamlessHSR + PrecipFlag 2022-02-02T09:00Z; older frames synthetic (shifted back along 4 mph "
                 "toward SE); surface temperature synthetic (<= 0 C north of 39.86 N)")
    artwork("fall", "radar-motion", ["fall", "alert", "battery-low", "stale"])
    artwork("winter", "radar-snowline", ["winter", "live-winter-2027-01-19"])


if __name__ == "__main__":
    main()
