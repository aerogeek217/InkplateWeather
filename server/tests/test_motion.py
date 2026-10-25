"""Storm motion (app/motion.py), its provider plumbing (radar_mrms), drawing (radarmap) and the radar callout.

Recorded MRMS frames in tests/data/recorded/mrms/motion/ (recorded with --margin 80, the provider's motion window):
- 2023-03-31 23:36, 23:50 and 2023-04-01 00:06 UTC: a squall line growing as it raced east; the frame pairs give
  different motions, so the gate must refuse it
- 2022-02-02 08:30, 08:46, 09:00 UTC: snow north-west of Savoy, ice/rain south-east; the snow line edges south-east
"""
import gzip
import json
import math
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import numpy as np
import pytest
import respx
from conftest import TESTS_DIR

from app import motion, radarmap, summary
from app.config import AppConfig, LocationCfg, ProviderCfg
from app.mapgeo import UNITS_PER_MILE, X_MAX, X_MIN, Y_MAX, Y_MIN
from app.paths import FIXTURES_DIR
from app.providers import base, radar_mrms
from app.providers.base import RequestContext

MOTION = TESTS_DIR / "data" / "recorded" / "mrms" / "motion"
TZ = ZoneInfo("America/Chicago")
EXT = radarmap.extent()
HOME = EXT.project(-88.2517, 40.0617)


def grid(name: str, margin: float = radar_mrms.MOTION_MARGIN):
    return radar_mrms.decode_crop((MOTION / name).read_bytes(), EXT.bbox(margin))


def frame(refl: str, flag: str | None = None) -> motion.Frame:
    return radar_mrms.motion_frame(grid(refl), grid(flag) if flag else None)


def lattice():
    return radar_mrms.lattice(EXT, grid("SeamlessHSR_00.00_20230401-000600.grib2"))


def storm_set():
    """The real 00:06 UTC line and two older frames: the same field moved back along 15 mph toward ENE."""
    from build_radar_fixtures import shifted

    lat = lattice()
    latest = frame("SeamlessHSR_00.00_20230401-000600.grib2")
    return latest, [shifted(latest, lat, 15.0, 67.5, m) for m in (30, 15)], lat


def winter_set(real: bool = False):
    """The real 09:00 UTC snow line and two older frames: by default the same field moved back along 4 mph toward
    SE (the real 08:30 and 08:46 frames, with `real`, show the line nearly stationary)."""
    from build_radar_fixtures import shifted

    names = [(f"SeamlessHSR_00.00_20220202-0{t}.grib2", f"PrecipFlag_00.00_20220202-0{t}.grib2")
             for t in ("83000", "84600", "90000")]
    lat = radar_mrms.lattice(EXT, grid(names[-1][0]))
    latest = frame(*names[-1])
    if real:
        return latest, [frame(*n) for n in names[:-1]], lat
    return latest, [shifted(latest, lat, 4.0, 135.0, m) for m in (30, 14)], lat


# ------------------------------------------------------------------ motion estimate


def test_shift_finds_a_known_translation():
    y, x = np.mgrid[0:120, 0:140]
    blob = lambda cy, cx: np.exp(-((y - cy) ** 2 + (x - cx) ** 2) / 60.0)  # noqa: E731
    a = blob(60, 50) + 0.6 * blob(40, 90)
    b = blob(63.5, 58.0) + 0.6 * blob(43.5, 98.0)
    dy, dx, peak = motion.shift(a - a.mean(), b - b.mean(), (20, 20))
    assert dy == pytest.approx(3.5, abs=0.3) and dx == pytest.approx(8.0, abs=0.3)
    assert peak > 0.9


def test_motion_of_a_translated_real_field():
    latest, older, lat = storm_set()
    m = motion.estimate(older, latest, lat)
    assert m is not None
    assert m.mph == pytest.approx(15, abs=1.0) and m.heading == "ENE"


def test_disagreeing_frames_give_no_motion():
    # The real line: 30 → 0 min and 16 → 0 min disagree by ~19 mph (it grew eastward as it moved), so no overlays.
    names = ["SeamlessHSR_00.00_20230331-233600.grib2", "SeamlessHSR_00.00_20230331-235000.grib2"]
    latest, lat = frame("SeamlessHSR_00.00_20230401-000600.grib2"), lattice()
    older = [frame(n) for n in names]
    pairs = [motion._pair_motion(o, latest, lat)[0].mph for o in older]
    assert abs(pairs[0] - pairs[1]) > motion.AGREE_MPH
    assert motion.estimate(older, latest, lat) is None
    assert motion.analyze(latest, older, lat, HOME, "intensity", TZ) is None
    assert motion.analyze(latest, [], lat, HOME, "intensity", TZ) is None  # nothing to compare with


def test_compass():
    assert [motion.compass16(b) for b in (0, 67.5, 90, 200, 359)] == ["N", "ENE", "E", "SSW", "N"]
    assert motion.compass8_word(270) == "west" and motion.compass8_word(315) == "northwest"
    assert motion.Motion(77.6, -32.1).heading == "ENE"  # x east, y south


# ------------------------------------------------------------------ storm overlays


def test_storm_overlays_follow_the_motion():
    latest, older, lat = storm_set()
    latest.t = datetime(2026, 10, 24, 14, 25, tzinfo=TZ)
    for o, mins in zip(older, (30, 15), strict=True):
        o.t = latest.t - timedelta(minutes=mins)
    res = motion.analyze(latest, older, lat, HOME, "intensity", TZ)
    m = res["motion"]
    ux, uy = m["vx"] / math.hypot(m["vx"], m["vy"]), m["vy"] / math.hypot(m["vx"], m["vy"])
    lines = res["arrivals"]
    assert [datetime.fromisoformat(a["t"]).strftime("%H:%M") for a in lines] == \
        ["15:00", "16:00", "17:00"]
    for a in lines:  # perpendicular to the motion, inside the panel, tags on the top row
        assert abs((a["x2"] - a["x1"]) * ux + (a["y2"] - a["y1"]) * uy) < 1.5
        assert all(X_MIN - 0.1 <= v <= X_MAX + 0.1 for v in (a["x1"], a["x2"]))
        assert a["tag"][1] == motion.TAG_Y
    gaps = [lines[i + 1]["x1"] - lines[i]["x1"] for i in range(2)]
    assert gaps[0] == pytest.approx(gaps[1], abs=1)  # one hour apart = the same distance
    arr = res["arrival"]
    assert arr["kind"] == "storm" and arr["from"] == "west" and arr["heading"] == "ENE" and arr["mph"] == 15
    t = datetime.fromisoformat(arr["t"])
    assert latest.t < t < latest.t + timedelta(hours=1)
    assert arr["dist_mi"] == pytest.approx((t - latest.t).total_seconds() / 3600 * 14.7, abs=1.5)
    ar = res["arrow"]
    assert ar["label"] == "15 mph"
    assert not motion._blocked(ar["x1"], ar["y1"]) and not motion._blocked(ar["x2"], ar["y2"])
    for a in lines:
        assert abs(ar["x2"] - a["tag"][0]) >= motion.TAG_CLEAR_X or abs(ar["y2"] - a["tag"][1]) >= motion.TAG_CLEAR_Y


def test_times_follow_the_local_clock():
    t0 = datetime(2026, 10, 24, 20, 25, tzinfo=UTC).astimezone(TZ)  # 3:25 PM CDT
    hours = [t.strftime("%H:%M") for t in motion._times_after(t0, 180, 6, 0.5)]
    assert hours == ["18:00", "21:00"]  # local multiples of 3 h, not UTC ones
    assert [t.strftime("%H:%M") for t in motion._times_after(t0, 30, 1.2, 10 / 60)] == ["16:00", "16:30"]


def test_clip_segment():
    assert motion.clip_segment(-1000, 0, 1000, 0) == pytest.approx((X_MIN, 0, X_MAX, 0))
    assert motion.clip_segment(-1000, 500, 1000, 500) is None


# ------------------------------------------------------------------ snow line


def test_snow_line_and_its_motion():
    latest, older, lat = winter_set()
    res = motion.analyze(latest, older, lat, HOME, "types", TZ)
    assert res["motion"] is None and res["arrivals"] == [] and res["arrow"] is None
    line = np.array(res["snow_line"])
    assert len(line) >= 10
    assert line[:, 0].min() < -150 and line[:, 0].max() > 120  # runs across the panel, south-west to north-east
    assert line[0, 1] > line[-1, 1]
    lines = res["snow_lines"]
    assert [datetime.fromisoformat(s["t"]).strftime("%H:%M") for s in lines] == ["06:00", "09:00"]  # CST
    # each later line lies further south-east (the snow advancing over the ice)
    mid = [np.array(s["points"]).mean(axis=0) for s in lines]
    assert mid[0][1] > line[:, 1].mean() - 5 and mid[1][0] > mid[0][0] - 5 and mid[1][1] > mid[0][1]
    arr = res["arrival"]
    assert arr["kind"] == "snow" and arr["from"] == "northwest" and 1 <= arr["mph"] <= 6


def test_a_stationary_snow_line_gets_no_projection():
    """The real 08:30 / 08:46 / 09:00 UTC frames, compared over the same window: the line barely moved."""
    latest, older, lat = winter_set(real=True)
    res = motion.analyze(latest, older, lat, HOME, "types", TZ)
    assert len(res["snow_line"]) >= 10 and res["snow_lines"] == [] and res["arrival"] is None


def test_no_snow_line_without_types():
    latest, older, lat = storm_set()
    assert motion.snow_overlays(latest, older, lat, HOME)["snow_line"] is None


# ------------------------------------------------------------------ provider plumbing


def test_listing_and_nearest():
    html = ('<a href="MRMS_ReflectivityAtLowestAltitude.latest.grib2.gz">x</a>'
            '<a href="MRMS_ReflectivityAtLowestAltitude_00.50_20261025-133032.grib2.gz">x</a>'
            '<a href="MRMS_ReflectivityAtLowestAltitude_00.50_20261025-134640.grib2.gz">x</a>')
    files = radar_mrms.listed(html)
    assert [t.strftime("%H%M%S") for t, _ in files] == ["133032", "134640"]
    t = datetime(2026, 10, 25, 14, 0, 39, tzinfo=UTC)
    assert radar_mrms.nearest(files, t - timedelta(minutes=30), 4).endswith("133032.grib2.gz")
    assert radar_mrms.nearest(files, t - timedelta(minutes=15), 4).endswith("134640.grib2.gz")
    assert radar_mrms.nearest(files, t - timedelta(minutes=60), 4) is None


def _gz(name: str) -> httpx.Response:
    return httpx.Response(200, content=gzip.compress((MOTION / name).read_bytes()))


@pytest.fixture
def provider():
    app = AppConfig(location=LocationCfg(name="Savoy, IL", lat=40.0617, lon=-88.2517, tz="America/Chicago"))
    return base.build("mrms", ProviderCfg(type="radar_mrms", ttl="2m", ice=False), app)


def _mock_winter(router, listing_status=200):
    b = radar_mrms.BASE_URL
    refl = "ReflectivityAtLowestAltitude"
    router.get(radar_mrms.url(b, refl)).mock(return_value=_gz("SeamlessHSR_00.00_20220202-090000.grib2"))
    router.get(radar_mrms.url(b, "PrecipFlag")).mock(return_value=_gz("PrecipFlag_00.00_20220202-090000.grib2"))
    for product, prefix in ((refl, "SeamlessHSR"), ("PrecipFlag", "PrecipFlag")):
        names = {s: f"MRMS_{product}_00.50_20220202-{s}.grib2.gz" for s in ("083000", "084600")}
        html = "".join(f'<a href="{n}">{n}</a>' for n in names.values())
        router.get(f"{b}/{product}/").mock(return_value=httpx.Response(listing_status, text=html))
        for s, n in names.items():
            router.get(f"{b}/{product}/{n}").mock(return_value=_gz(f"{prefix}_00.00_20220202-{s}.grib2"))


async def test_provider_adds_the_snow_line(provider):
    with respx.mock(assert_all_called=False) as router:
        _mock_winter(router)
        out = await provider.fetch(RequestContext(now=datetime(2022, 2, 2, 9, 2, tzinfo=UTC)))
    f = out["radar.frame"].value
    assert f["legend"] == "types"
    assert f["overlays"]["snow_line"] and f["overlays"]["snow_lines"] == []  # the recorded line is stationary
    assert f["arrival"] is None
    json.dumps(f)  # still plain JSON for the cache


async def test_provider_keeps_the_frame_when_motion_fails(provider):
    with respx.mock(assert_all_called=False) as router:
        _mock_winter(router, listing_status=503)
        out = await provider.fetch(RequestContext(now=datetime(2022, 2, 2, 9, 2, tzinfo=UTC)))
    f = out["radar.frame"].value
    assert f["layers"] and "overlays" not in f


# ------------------------------------------------------------------ drawing


def _fixture_frame(name: str) -> dict:
    return json.loads((FIXTURES_DIR / f"{name}.json").read_text(encoding="utf-8"))["readings"]["radar.frame"]["value"]


def test_motion_layers_draw_future_lines_only():
    f = _fixture_frame("radar-motion")
    now = datetime(2026, 10, 24, 14, 30, tzinfo=TZ)
    mo = radarmap.motion_layers(f, now)
    assert [s for s in ("3 PM", "4 PM", "5 PM") if f">{s}</text>" in mo["lines"]] == ["3 PM", "4 PM", "5 PM"]
    assert 'stroke-dasharray="6 4"' in mo["lines"] and mo["arrow"].count("<path") == 2
    assert ">15 mph</text>" in mo["labels"]
    later = radarmap.motion_layers(f, now.replace(hour=15, minute=10))
    assert ">3 PM<" not in later["lines"] and ">4 PM<" in later["lines"]
    svg = radarmap.layers(f, "SAVOY", HOME, now=now)
    assert svg.index("stroke-dasharray=\"6 4\"") > svg.index('mask="url(#rdM0)"')  # lines above the echoes
    assert radarmap.motion_layers({"layers": []}, now) == {"snow_line": "", "lines": "", "arrow": "", "labels": ""}


def test_snow_line_layers():
    f = _fixture_frame("radar-snowline")
    mo = radarmap.motion_layers(f, datetime(2027, 1, 19, 15, 30, tzinfo=TZ))
    assert 'stroke-width="2"' in mo["snow_line"] and ">snow line</text>" in mo["labels"]
    assert ">SNOW 6 PM</text>" in mo["lines"] and ">SNOW 9 PM</text>" in mo["lines"]
    svg = radarmap.layers(f, "SAVOY", HOME, now=datetime(2027, 1, 19, 15, 30, tzinfo=TZ))
    assert svg.index(mo["snow_line"]) < svg.index('stroke="#6D6D6D" stroke-width="2"')  # under the winter roads


def test_tag_widths_fit_their_text():
    assert radarmap._tag_text(datetime(2026, 1, 1, 15, 30)) == "3:30 PM"
    assert radarmap._tag_text(datetime(2026, 1, 1, 12, 0)) == "12 PM"
    assert 'width="36"' in radarmap._tag(0, 0, "4 PM")  # the fall board's tag


# ------------------------------------------------------------------ callout


def _inputs(name: str, now: datetime | None = None) -> summary.Inputs:
    d = json.loads((FIXTURES_DIR / f"{name}.json").read_text(encoding="utf-8"))
    r = {k: (v["value"] if isinstance(v, dict) and "value" in v and "observed_at" in v else v)
         for k, v in d["readings"].items()}
    now = now or datetime.fromisoformat(d["now"]).astimezone(TZ)
    return summary.Inputs.build(now, TZ, r.get("forecast.hourly"), r.get("forecast.daily"),
                                r.get("conditions.current"), r.get("alerts.active"))


def test_radar_callout_for_storms():
    inp = _inputs("fall")
    arrival = {"t": "2026-10-24T15:53:00-05:00", "kind": "storm", "dist_mi": 22, "from": "west", "heading": "ENE",
               "mph": 15}
    co = summary.callout(inp, arrival)
    assert co["kind"] == "storm" and co["headline"] == "Storms ~4 PM"
    assert co["detail"].startswith("22 mi west · ENE 15 mph")
    arrival["t"] = "2026-10-24T15:10:00-05:00"
    assert summary.callout(inp, arrival)["headline"] == "Storms ~3 PM"
    arrival["t"] = "2026-10-24T14:40:00-05:00"  # nearest half hour, never in the past
    assert summary.callout(inp, arrival)["headline"] == "Storms ~3 PM"
    arrival["t"] = "2026-10-24T19:30:00-05:00"  # beyond what radar can time: back to forecast wording
    assert summary.callout(inp, arrival) == summary.callout(inp)
    arrival["t"] = "2026-10-24T14:00:00-05:00"  # already past (an old cached frame)
    assert summary.callout(inp, arrival) == summary.callout(inp)


def test_radar_callout_for_the_snow_line():
    inp = _inputs("winter")
    co = summary.callout(inp, {"t": "2027-01-19T16:26:00-06:00", "kind": "snow", "dist_mi": 3, "from": "northwest",
                               "heading": "SE", "mph": 3})
    assert co["headline"] == "Snow by 5 PM" and co["kind"] == "snow"
    assert co["detail"] == summary.callout(inp)["detail"]  # the forecast's detail ("Sleet now · heaviest …")
    storm = {"t": "2027-01-19T16:00:00-06:00", "kind": "rain", "dist_mi": 5, "from": "west", "heading": "E", "mph": 20}
    assert summary.callout(inp, storm) == summary.callout(inp)  # a winter storm: echo motion doesn't time it


def test_fixture_boards_get_the_radar_callout():
    from app import viewmodel  # noqa: F401  (the view model passes the frame's arrival through)

    f = _fixture_frame("radar-motion")
    co = summary.callout(_inputs("radar-motion"), f["arrival"])
    assert co["headline"] == "Storms ~3 PM" and co["detail"].startswith("7 mi west · ENE 15 mph")
    assert abs(f["arrival"]["dist_mi"] * UNITS_PER_MILE) <= Y_MAX - Y_MIN
