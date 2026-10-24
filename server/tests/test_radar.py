"""Radar building blocks: GRIB2 reader, classification onto the panel, base map + SVG layers (offline)."""
import base64
import io
import json
import math
from datetime import UTC, datetime

import pytest
from conftest import TESTS_DIR
from PIL import Image

from app import grib2, mapgeo, radar, radarmap

MRMS = TESTS_DIR / "data" / "recorded" / "mrms"
EXT = radarmap.extent()


def grid(name: str) -> grib2.Grid:
    return grib2.decode((MRMS / name).read_bytes())


# --- map geometry -----------------------------------------------------------------------------------------------

def test_extent_matches_the_approved_board():
    assert (EXT.lat, EXT.lon) == (40.0617, -88.2517)
    # 25 mi ring r = 140 units; the panel is 536 × 570 px at 1.25 px per unit
    assert mapgeo.RING_MILES * mapgeo.UNITS_PER_MILE == 140
    assert (2 * mapgeo.HALF_W * mapgeo.PX_PER_UNIT, 2 * mapgeo.HALF_H * mapgeo.PX_PER_UNIT) == (536, 570)
    w, s, e, n = EXT.bbox()
    assert round((e - w) * 69.093 * 0.765, 0) == 77  # ≈ 77 × 81 mi
    assert round((n - s) * 69.093, 0) == 81
    assert EXT.project(EXT.lon, EXT.lat) == (0, 0)
    x, y = EXT.project(-87.63, 40.124)  # Danville: east and a little north
    assert 180 < x < 195 and -40 < y < -20
    lon, lat = EXT.unproject(*EXT.project(-88.9, 39.6))
    assert lon == pytest.approx(-88.9) and lat == pytest.approx(39.6)


def test_basemap_towns_sit_where_the_board_drew_them():
    bm = radarmap.load()
    assert bm["properties"]["center"] == [-88.2517, 40.0617]
    layers = {f["properties"]["layer"] for f in bm["features"]}
    assert layers == {"water", "county", "state", "interstate", "place"}
    board = {"Rantoul": (2, -100), "Paxton": (11, -146), "Danville": (185, -10), "Monticello": (-100, -22),
             "Clinton": (-204, -34), "Bloomington": (-207, -185), "Tuscola": (0, 123), "Mattoon": (-34, 235)}
    for f in bm["features"]:
        p = f["properties"]
        if p["layer"] == "place" and p["name"] in board:
            x, y = EXT.project(*f["geometry"]["coordinates"])
            bx, by = board[p["name"]]
            assert abs(x - bx) < 35 and abs(y - by) < 35, p["name"]  # the board was drawn by hand (≤ 6 mi off)
    refs = {f["properties"]["ref"] for f in bm["features"] if f["properties"]["layer"] == "interstate"}
    assert {"I-57", "I-74", "I-72"} <= refs


# --- GRIB2 ------------------------------------------------------------------------------------------------------

def test_live_reflectivity_header_and_values():
    g = grid("MRMS_ReflectivityAtLowestAltitude_00.50_20261024-184236.grib2")
    assert g.time == datetime(2026, 10, 24, 18, 42, 36, tzinfo=UTC)
    assert (g.di, g.dj) == (0.01, 0.01)
    assert (g.ref, g.e, g.d) == (-9990.0, 0, 1)
    sub = g.crop(*EXT.bbox())
    assert {sub.value(i, j) for j in range(sub.nj) for i in range(sub.ni)} == {-99.0}  # no echo: a quiet day
    w, s, e, n = EXT.bbox()
    assert sub.lon1 <= w and sub.lat1 >= n
    assert sub.lonlat(sub.ni - 1, sub.nj - 1)[0] >= e and sub.lonlat(sub.ni - 1, sub.nj - 1)[1] <= s


def test_24_bit_surface_temperature():
    g = grid("MRMS_Model_SurfaceTemp_00.50_20261024-170000.grib2")
    assert g.image.mode == "RGB" and (g.ref, g.d) == (-99900.0, 2)
    sub = g.crop(*EXT.bbox())
    home = sub.value(round((EXT.lon - sub.lon1) / sub.di), round((sub.lat1 - EXT.lat) / sub.dj))
    assert home == pytest.approx(20.5, abs=0.1)  # °C at noon, 2026-10-24 (69 °F)


def test_encode_round_trip():
    g = grid("SeamlessHSR_00.00_20230401-010000.grib2")
    sub = g.crop(-88.6, 39.8, -88.0, 40.3)
    again = grib2.decode(grib2.encode(sub)).crop(-180, -90, 180, 90, pad=0)
    assert (again.ni, again.nj, again.raw) == (sub.ni, sub.nj, sub.raw)
    assert again.lat1 == pytest.approx(sub.lat1) and again.lon1 == pytest.approx(sub.lon1)
    assert again.time == sub.time and (again.category, again.parameter) == (sub.category, sub.parameter)


def test_bad_files_raise():
    with pytest.raises(grib2.GribError):
        grib2.decode(b"not grib at all")
    data = bytearray((MRMS / "PrecipFlag_00.00_20230401-010000.grib2").read_bytes())
    data[16 + 21 + 12:16 + 21 + 14] = b"\x00\x1e"  # grid template 3.30 (Lambert) instead of 3.0
    with pytest.raises(grib2.GribError, match="3.30"):
        grib2.decode(bytes(data))


# --- classification ---------------------------------------------------------------------------------------------

def raster(fn, codes: bool = False) -> radar.Raster:
    """A 0.01° raster over the extent; fn(lon, lat) → dBZ (or a type code)."""
    w, s, e, n = EXT.bbox(10)
    w, n = EXT.lon - math.ceil((EXT.lon - w) / 0.01) * 0.01, EXT.lat + math.ceil((n - EXT.lat) / 0.01) * 0.01
    ni, nj = int((e - w) / 0.01) + 1, int((n - s) / 0.01) + 1  # cell centers on home
    data = bytes((fn(w + i * 0.01, n - j * 0.01) or 0) if codes else radar.enc(fn(w + i * 0.01, n - j * 0.01))
                 for j in range(nj) for i in range(ni))
    return radar.Raster(ni, nj, w, n, 0.01, 0.01, data)


def mask(png: str) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(png)))


def test_dbz_encoding():
    assert radar.enc(None) == 0 and radar.enc(-99) == 0 and radar.enc(-999) == 0
    assert radar.enc(15) == 94 and radar.enc(30) == 124 and radar.enc(200) == 255


@pytest.mark.parametrize(("dbz", "cls"), [(20, "rain-light"), (35, "rain-moderate"), (45, "rain-heavy"),
                                          (55, "rain-extreme")])
def test_uniform_intensity(dbz, cls):
    legend, masks = radar.classify(EXT, raster(lambda lon, lat: dbz))
    assert legend == "intensity" and [n for n, _ in masks] == [cls]
    assert masks[0][1].histogram()[255] == 536 * 570


def test_echo_lands_on_home():
    """A 3 × 3 km block of 45 dBZ centered on home is drawn around the panel center (268, 285)."""
    def fn(lon, lat):
        return 45 if abs(lon - EXT.lon) < 0.015 and abs(lat - EXT.lat) < 0.015 else None
    _, masks = radar.classify(EXT, raster(fn))
    box = dict(masks)["rain-heavy"].getbbox()
    assert abs((box[0] + box[2]) / 2 - 268) <= 1.5 and abs((box[1] + box[3]) / 2 - 285) <= 1.5
    assert 6 <= box[2] - box[0] <= 16  # 3 cells of ~4.3 px, edges eased by the bilinear resampling


def test_types_legend_only_for_enough_frozen_precip():
    north = raster(lambda lon, lat: 25)
    snow_north = raster(lambda lon, lat: radar.SNOW if lat > EXT.lat else radar.RAIN, codes=True)
    legend, masks = radar.classify(EXT, north, snow_north)
    assert legend == "types" and [n for n, _ in masks] == ["rain", "snow-heavy"]
    tiny = raster(lambda lon, lat: radar.SNOW if abs(lon - EXT.lon) < 0.005 and abs(lat - EXT.lat) < 0.005
                  else radar.RAIN, codes=True)
    legend, masks = radar.classify(EXT, north, tiny)
    assert legend == "intensity" and [n for n, _ in masks] == ["rain-light"]


def test_frame_dict():
    f = radar.frame(EXT, raster(lambda lon, lat: 20 if lat < EXT.lat else None), None, site="MRMS",
                    time=datetime(2026, 10, 24, 19, 0, tzinfo=UTC), home="Savoy")
    assert f["site"] == "MRMS" and f["time"] == "2026-10-24T19:00:00+00:00" and f["legend"] == "intensity"
    assert f["types"] == ["rain"] and [x["fill"] for x in f["layers"]] == ["rain-light"]
    m = mask(f["layers"][0]["png"])
    assert m.size == (536, 570) and m.getbbox()[1] >= 280  # south half only
    assert f["aria_label"] == "Radar map around Savoy: rain in view"
    json.dumps(f)  # persisted by the cache
    empty = radar.frame(EXT, raster(lambda lon, lat: None), None, site="MRMS", time=datetime.now(UTC), home="Savoy")
    assert empty["layers"] == [] and empty["types"] == [] and "no precipitation" in empty["aria_label"]


# --- SVG ----------------------------------------------------------------------------------------------------------

def test_layers_without_echoes_draw_the_map():
    svg = radarmap.layers(None, "SAVOY")
    assert svg.startswith('<rect x="-214.4" y="-228" width="428.8" height="456" fill="#FFFFFF">')
    assert 'stroke="#DBDBDB"' in svg and 'stroke-dasharray="8 4"' in svg and 'stroke-width="3.5"' in svg
    assert '<circle cx="0" cy="0" r="140"' in svg and ">25 mi<" in svg and ">SAVOY<" in svg and ">Rantoul<" in svg
    assert "<mask" not in svg


def test_layers_order_follows_the_legend():
    rain = {"legend": "intensity", "layers": [{"fill": "rain-heavy", "png": "AAAA"}]}
    svg = radarmap.layers(rain, "SAVOY")
    assert svg.index('stroke-width="3.5"') < svg.index('<mask id="rdM0"') < svg.index('r="140"')
    assert 'fill="#494949" mask="url(#rdM0)"' in svg and "<defs>" not in svg
    snow = {"legend": "types", "layers": [{"fill": "snow", "png": "AAAA"}, {"fill": "ice", "png": "AAAA"}]}
    svg = radarmap.layers(snow, "SAVOY")
    assert 'id="rdSnow"' in svg and 'fill="url(#rdSnow)" mask="url(#rdM0)"' in svg and 'url(#rdIce)' in svg
    # interstates above the echoes, thin with a white halo; home square outlined in white
    assert svg.index('mask="url(#rdM1)"') < svg.index('stroke="#6D6D6D" stroke-width="2"')
    assert 'stroke-width="3.5"' not in svg
    assert 'fill="#000000" stroke="#FFFFFF" stroke-width="2"></rect>' in svg
