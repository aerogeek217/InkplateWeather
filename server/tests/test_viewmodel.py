"""View model + template: geometry and text reproduce the reference boards; conditional states via data."""
import re
from datetime import datetime
from html.parser import HTMLParser

import pytest
from conftest import REPO_DIR, TEST_CONFIG

from app import collector, config, icons, viewmodel
from app.context import Icon
from app.providers.base import DeviceInfo, RequestContext
from app.providers.fixture import fixture_device, fixture_now
from app.readings import Reading, Status
from app.render import render_html
from app.resolver import Resolved

CFG = config.load(TEST_CONFIG)


async def build(name):
    now = fixture_now(name)
    resolved = await collector.collect_fixture(name, RequestContext(now=now))
    return viewmodel.build(resolved, now, fixture_device(name), CFG), resolved


def close(a, b, tol=0.3):
    return all(abs(x - y) <= tol for x, y in zip(a, b, strict=True))


async def test_fall_header_and_current():
    ctx, _ = await build("fall")
    h = ctx.header
    assert (h.city, h.date_text, h.time_text, h.next_update) == ("Savoy, IL", "Saturday 24 October", "2:30 PM", "3:00")
    assert h.day_line == "Storms around 4 PM, then colder and windy." and h.alert is None
    assert h.battery_pct == 78 and h.battery_fill == 15.6
    assert icons.resolve(h.icon) == "modifiers/cloudy-wind"
    c = ctx.current
    assert (c.temp.text, c.high, c.low, c.feels, c.normals) == (
        "58°", "H 60°", "L 38°", "Feels 58° · Humidity 64%", "Average 63° / 42°")
    assert (c.wind.dir_text, c.wind.speed, c.wind.gust) == ("S", "14", "26")
    assert [(r.label, r.slot.text, r.slot.sub) for r in ctx.readings] == [
        ("Inside", "70.2°", "41%"), ("Garage", "52.8°", "58%"), ("Pressure ↓", "29.94", None),
        ("Air quality", "34", "Good"), ("UV index", "2", "Low")]


async def test_fall_trend_geometry_matches_board():
    t = (await build("fall"))[0].trend
    assert [g for g in t.gridlines] == [(10.5, "60°"), (28.18, "50°"), (45.86, "40°")]
    assert close(t.midnights, [91.5, 265.1, 438.7])
    assert t.day_labels == [(34.0, "FRI"), (96.0, "SAT"), (270.0, "SUN"), (443.0, "MON")]
    assert close(t.now, (196.4, 13.33))
    assert len(t.past) == 24 and close(t.past[0], (30, 5.19)) and close(t.past[12], (116.8, 42.33))
    board_future = [(196.4, 13.33), (200, 10.5), (207.3, 21.1), (221.7, 28.18), (243.4, 37.02), (265.1, 44.09),
                    (286.8, 47.62), (308.5, 49.39), (330.2, 42.33), (351.9, 29.95), (373.6, 24.64), (395.3, 33.48),
                    (417, 44.09), (438.7, 51.16), (460.4, 54.69), (482.1, 56.47), (503.8, 44.09), (525.5, 26.41),
                    (540, 21.1)]
    assert len(t.future) == len(board_future)
    assert all(close(a, b) for a, b in zip(t.future, board_future, strict=True))
    board_points = [(120.4, 42.33, 120.4, 37.5, "42°"), (308.5, 49.39, 308.5, 44.57, "38°"),
                    (373.6, 24.64, 373.6, 20.14, "52°"), (482.1, 56.47, 482.1, 51, "34°"),
                    (540, 21.1, 532, 16.29, "54°")]
    assert [p[4] for p in t.points] == [p[4] for p in board_points]
    for p, b in zip(t.points, board_points, strict=True):
        assert close(p[:4], b[:4], tol=0.9)
    board_nights = [(49, 95.2), (222.7, 95.2), (396.1, 95.5)]
    assert all(close(a, b, tol=0.5) for a, b in zip(t.nights, board_nights, strict=True))


async def test_winter_trend_labels_below_lows_when_room():
    t = (await build("winter"))[0].trend
    assert [g[1] for g in t.gridlines] == ["30°", "20°", "10°"]
    board = [(109.6, 27.93, 36.21, "25°"), (330.2, 39.24, 47.79, "19°"), (373.6, 35.5, 31.07, "21°"),
             (482.1, 54.34, 49.71, "11°"), (540, 35.5, 30.43, "21°")]
    for p, b in zip(t.points, board, strict=True):
        assert p[4] == b[3]
        assert close((p[0], p[1], p[3]), b[:3], tol=0.9)


async def test_fall_precip_rain_bars():
    p = (await build("fall"))[0].precip
    assert (p.title, p.right_text, p.right_bold, p.patterned, p.key, p.height) == (
        "Chance of rain", "next 12 h", False, False, [], 66)
    assert p.axis_y == 48 and p.hour_y == 62
    assert [(b.y, b.height, b.fill) for b in p.bars[:6]] == [
        (30.86, 17.14, "#494949"), (13.71, 34.29, "#000000"), (13.71, 34.29, "#000000"), (22.29, 25.71, "#494949"),
        (30.86, 17.14, "#494949"), (39.43, 8.57, "#929292")]
    assert [b.label for b in p.bars] == ["40", "80", "80", "60", "40", "20"] + [None] * 6
    assert p.hour_labels == [(3, "3 PM", "start"), (87.5, "6 PM", "middle"), (162.5, "9 PM", "middle"),
                             (237.5, "12 AM", "middle")]


async def test_winter_precip_patterned_with_key_and_amounts():
    p = (await build("winter"))[0].precip
    assert (p.title, p.right_text, p.right_bold, p.patterned, p.key, p.height) == (
        "Chance of snow", "3–5 in · ice 0.05 in", True, True, ["ice", "snow"], 56)
    assert p.axis_y == 42 and p.hour_y == 54
    assert [b.fill for b in p.bars[:3]] == ["url(#wBarMix)", "url(#wBarMix)", "url(#wBarSnow)"]
    assert (p.bars[0].y, p.bars[0].height, p.bars[3].y, p.bars[0].label_y) == (14.78, 27.22, 11.76, 12.26)


async def test_gauge_switches_in_winter():
    fall = (await build("fall"))[0].gauge
    winter = (await build("winter"))[0].gauge
    assert (fall.title, fall.note) == ("Rain gauge", "inches")
    assert [(c.value.text, c.label) for c in fall.cells] == [
        ("0.00", "Today"), ("0.42", "This week"), ("1.86", "October"), ("31.07", "2026")]
    assert (winter.title, winter.note, winter.note_size) == ("Gauge", "liquid in · snow via NWS", 11)
    assert [(c.value.text, c.label) for c in winter.cells] == [
        ("0.21", "Today"), ("1.12", "January"), ("4 in", "Snow depth · NWS"), ("11.8 in", "Season snow · NWS")]


async def test_winter_alert_replaces_day_line():
    ctx, _ = await build("winter")
    h = ctx.header
    assert h.alert.event == "Winter Storm Warning"
    assert h.alert.detail == "until 9 AM Wed · 3–5 in snow"
    assert h.next_update is None
    assert icons.resolve(h.icon) == "modifiers/sleet-wind"
    assert ctx.radar.legend == "types" and ctx.radar.callout.kind == "snow"
    html = render_html(ctx)
    assert "Winter Storm Warning" in html and "Sleet changing to snow" not in html and "Next update" not in html


async def test_sun_and_moon():
    fall = (await build("fall"))[0]
    winter = (await build("winter"))[0]
    assert close(fall.sun.arc_end, (165.1, 14.8), 0.1) and close(winter.sun.arc_end, (203.5, 28.6), 0.1)
    assert (fall.sun.rise, fall.sun.set, fall.sun.day_length, fall.sun.delta) == (
        "7:17 AM", "6:08 PM", "10 h 51 m", "−2 m/day")
    assert winter.sun.delta == "+2 m/day"
    assert fall.moon.shadow_path == "M0,-25 A25,25 0 0 0 0,25 A23.5,25 0 0 1 0,-25 Z"
    assert winter.moon.shadow_path == "M0,-25 A25,25 0 0 0 0,25 A20,25 0 0 1 0,-25 Z"
    assert fall.moon.rise_set == "Rises 5:21 PM · sets 6:04 AM"
    assert [(p.label, p.date) for p in fall.moon.next] == [("Full", "10/26"), ("Last", "11/2"), ("New", "11/9"),
                                                          ("First", "11/17")]


@pytest.mark.parametrize("illum,waxing,expected", [
    (0.25, True, "M0,-25 A25,25 0 0 0 0,25 A12.5,25 0 0 0 0,-25 Z"),
    (0.75, False, "M0,-25 A25,25 0 0 1 0,25 A12.5,25 0 0 0 0,-25 Z"),
    (0.25, False, "M0,-25 A25,25 0 0 1 0,25 A12.5,25 0 0 1 0,-25 Z"),
])
def test_moon_shadow_other_phases(illum, waxing, expected):
    assert viewmodel.moon_shadow(illum, waxing)[1] == expected


async def test_forecast_rows():
    fall = (await build("fall"))[0].forecast
    winter = (await build("winter"))[0].forecast
    assert [r.day for r in fall] == ["Today", "Sun", "Mon", "Tue", "Wed", "Thu", "Fri"]
    assert [(r.wind.dir_text, r.wind.speed, r.wind.bold) for r in fall[:2]] == [("S", "14", False), ("NW", "22", True)]
    assert [r.icon.name for r in fall] == ["conditions/thunderstorm", "modifiers/sunny-wind", "conditions/sunny",
                                           "conditions/partly-cloudy", "conditions/cloudy", "conditions/rain",
                                           "conditions/partly-cloudy"]
    assert [r.icon.name for r in winter] == ["conditions/wintry-mix", "conditions/snow", "conditions/sunny",
                                             "conditions/cloudy", "conditions/freezing-rain", "conditions/wintry-mix",
                                             "conditions/flurries"]
    assert [(r.precip_kind, r.precip_text) for r in winter] == [
        ("snow", "4 in"), ("snow", "1 in"), ("rain", "0%"), ("rain", "10%"), ("ice", "40%"), ("ice", "50%"),
        ("snow", "20%")]
    assert [(r.high, r.low) for r in winter[:2]] == [("31°", "19°"), ("22°", "11°")]


def test_icon_rules():
    assert icons.header_modifiers("conditions/clear-night", 5, 33, False) == ["frost"]
    assert icons.header_modifiers("conditions/thunderstorm", 25, 70, True) == ["wind", "alert"]
    assert icons.header_modifiers("conditions/sunny", 8, 70, False, windy=True) == ["wind"]
    assert icons.header_modifiers("conditions/clear-night", 5, 35, False) == []
    assert icons.resolve(Icon(name="conditions/thunderstorm", modifiers=["alert"])) == "modifiers/storm-nws-alert"
    assert icons.resolve(Icon(name="conditions/rain", modifiers=["wind"])) == "conditions/rain"
    assert icons.condition("no-such-thing") == "conditions/cloudy"
    assert [icons.wind_dir(d) for d in (0, 44, 180, 315, 359)] == ["N", "NE", "S", "NW", "N"]
    svg = icons.svg("wind/from-nw", 12, label="from the northwest")
    assert svg.startswith('<svg width="12" height="12" viewBox="0 0 14 14" role="img"') and "rotate(135 7 7)" in svg


def test_build_survives_missing_data():
    now = datetime.fromisoformat("2026-10-24T14:30:00-05:00")
    resolved = {f: Resolved(field=f, reading=Reading(status=Status.missing, note="no_such_key"), tried=[])
                for f in CFG.fields}
    ctx = viewmodel.build(resolved, now, DeviceInfo(), CFG)
    assert ctx.current.temp.status is Status.missing and ctx.current.temp.message
    assert ctx.radar.status is Status.missing
    html = render_html(ctx)
    assert "<html" in html


# ------------------------------------------------------------------ rendered HTML vs the design source


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.chunks: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "title", "script"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("style", "title", "script"):
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.chunks.append(re.sub(r"\s+", " ", data.strip()))


def visible_text(html: str) -> list[str]:
    p = _Text()
    p.feed(html)
    return p.chunks


@pytest.mark.parametrize("name,source", [("fall", "dashboard.html"), ("winter", "dashboard-winter.html")])
async def test_rendered_text_matches_design_source(name, source):
    src = REPO_DIR / "design" / "source" / source
    if not src.is_file():
        pytest.skip("design/ not present (container image)")
    ctx, _ = await build(name)
    assert visible_text(render_html(ctx)) == visible_text(src.read_text(encoding="utf-8"))
