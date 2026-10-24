"""Conditional behaviour (design/SPEC.md, Conditional behavior table) driven by fixtures and small variations."""
import copy
import json
from datetime import datetime

import pytest
from conftest import TEST_CONFIG

from app import collector, config, icons, messages, shapes, viewmodel
from app.context import Icon
from app.paths import FIXTURES_DIR
from app.providers.base import DeviceInfo, RequestContext
from app.providers.fixture import fixture_device, fixture_now
from app.readings import Reading, Status
from app.render import render_html
from app.resolver import Resolved
from app.textwidth import measure

CFG = config.load(TEST_CONFIG)
NEW_FIXTURES = ["alert", "frost", "live-2026-10-24", "live-winter-2027-01-19"]


async def resolved_for(name):
    now = fixture_now(name)
    return now, await collector.collect_fixture(name, RequestContext(now=now))


async def build(name, **override):
    """Context for a fixture; keyword overrides replace field values (None = field missing)."""
    now, resolved = await resolved_for(name)
    resolved = dict(resolved)
    for key, value in override.items():
        field = key.replace("__", ".")
        if value is None:
            resolved[field] = Resolved(field=field, reading=Reading(status=Status.missing, note="no_value"), tried=[])
        else:
            resolved[field] = Resolved(field=field, reading=Reading(value=value, observed_at=now, source="test"),
                                       tried=[])
    return viewmodel.build(resolved, now, fixture_device(name), CFG)


def raw(name, field):
    v = json.loads((FIXTURES_DIR / f"{name}.json").read_text(encoding="utf-8"))["readings"][field]
    return copy.deepcopy(v["value"] if isinstance(v, dict) and "value" in v and "observed_at" in v else v)


@pytest.mark.parametrize("name", NEW_FIXTURES)
def test_new_fixtures_match_shapes(name):
    for field in shapes.FIELD_SHAPES:
        try:
            value = raw(name, field)
        except KeyError:
            continue
        shapes.validate(field, value)


# ------------------------------------------------------------------ icons and modifiers


async def test_header_icon_modifiers():
    assert icons.resolve((await build("fall")).header.icon) == "modifiers/cloudy-wind"  # gusts 26
    assert icons.resolve((await build("winter")).header.icon) == "modifiers/sleet-wind"
    frost = (await build("frost")).header.icon
    assert (frost.modifiers, icons.resolve(frost)) == (["frost"], "modifiers/clear-night-frost")  # clear, 33.8°
    assert icons.resolve((await build("frost", outdoor__temperature=36)).header.icon) == "conditions/clear-night"
    storm = (await build("alert")).header.icon
    assert storm.modifiers == ["wind", "alert"] and icons.resolve(storm) == "modifiers/storm-nws-alert"
    calm = await build("fall", wind__gust=12)
    assert calm.header.icon.modifiers == [] and icons.resolve(calm.header.icon) == "conditions/cloudy"
    # the source's own "windy" flag counts like gusts ≥ 20
    flagged = await build("fall", wind__gust=12, conditions__current={"name": "sunny", "windy": True})
    assert icons.resolve(flagged.header.icon) == "modifiers/sunny-wind"


def test_forecast_wind_composite_only_where_drawn():
    assert icons.forecast_modifiers("conditions/sunny", 22) == ["wind"]
    assert icons.forecast_modifiers("conditions/sunny", 19) == []
    assert icons.forecast_modifiers("conditions/rain", 30) == []  # no rain-wind artwork: plain rain
    assert icons.resolve(Icon(name="conditions/sunny", modifiers=["wind"])) == "modifiers/sunny-wind"


async def test_forecast_rows_rules():
    daily = raw("fall", "forecast.daily")
    daily[1]["icon"] = "sunny"  # the fixture names the composite directly; the rule derives it from the wind
    daily[2].update(icon="rain", wind_speed=25)
    daily[3].update(precip_type="snow", snow_in=2.4, pop=80)
    daily[4].update(precip_type="ice", pop=40)
    daily[5].update(wind_speed=0.4)
    rows = (await build("fall", forecast__daily=daily)).forecast
    assert icons.resolve(rows[1].icon) == "modifiers/sunny-wind" and rows[1].icon.label.endswith("windy")
    assert icons.resolve(rows[2].icon) == "conditions/rain" and rows[2].wind.bold
    assert [r.wind.bold for r in rows] == [False, True, True, False, False, False, False]
    assert (rows[3].precip_kind, rows[3].precip_text) == ("snow", "2.4 in")
    assert (rows[4].precip_kind, rows[4].precip_text) == ("ice", "40%")
    assert rows[5].wind.calm and rows[5].wind.dir_text == ""
    html = render_html(await build("fall", forecast__daily=daily))
    assert "Calm" in html


async def test_forecast_skips_days_already_past():
    rows = (await build("frost", forecast__daily=raw("fall", "forecast.daily"))).forecast  # fall starts Saturday
    assert [r.day for r in rows][:2] == ["Today", "Mon"] and len(rows) == 6


# ------------------------------------------------------------------ wind


@pytest.mark.parametrize("speed,gust,expected", [
    (14, 26, ("S", "14", "26", False)),
    (14, 18, ("S", "14", None, False)),  # gusts under speed + 5 aren't worth a mention
    (14, 19, ("S", "14", "19", False)),
    (1.34, 2.01, ("S", "1", None, False)),  # the first live render's "Wind S 1 · gusts 2"
    (0.4, 1.1, ("", "0", None, True)),
])
async def test_current_wind(speed, gust, expected):
    w = (await build("fall", wind__speed=speed, wind__gust=gust)).current.wind
    assert (w.dir_text, w.speed, w.gust, w.calm) == expected


async def test_calm_renders_as_calm():
    html = render_html(await build("frost"))
    assert ">Calm</span>" in html and "Wind  " not in html


# ------------------------------------------------------------------ alert takeover


async def test_alert_takeover_most_severe_and_fitted():
    h = (await build("alert")).header
    # The Severe Thunderstorm Warning outranks the Wind Advisory listed first. Its tag is long, so it is
    # abbreviated (2.6, approved 2026-10-24) and the impact ("60 mph gusts, 1 in hail") still fits.
    assert (h.alert.event, h.alert.detail) == ("Svr T-storm Warning", "until 3:15 PM · 60 mph gusts, 1 in hail")
    assert h.day_line  # generated but hidden by the alert
    html = render_html(await build("alert"))
    assert "Svr T-storm Warning" in html and h.day_line not in html


async def test_alert_detail_formats():
    adv = raw("alert", "alerts.active")[:1]  # Wind Advisory until 9 PM today
    assert (await build("alert", alerts__active=adv)).header.alert.detail == "until 9 PM · gusts to 50 mph"
    adv[0]["impact"] = None
    assert (await build("alert", alerts__active=adv)).header.alert.detail == "until 9 PM"
    assert (await build("winter")).header.alert.detail == "until 9 AM Wed · 3–5 in snow"
    adv[0]["ends"] = None
    assert (await build("alert", alerts__active=adv)).header.alert.detail == ""
    none = await build("alert", alerts__active=[])
    assert none.header.alert is None
    assert none.header.icon.modifiers == ["wind"]  # no alert: the storm keeps the wind modifier only
    assert icons.resolve(none.header.icon) == "conditions/thunderstorm"  # (no storm-wind artwork)


# ------------------------------------------------------------------ day line, callout, next chip


async def test_fixture_text_wins_over_generated():
    fall = await build("fall")
    assert fall.header.day_line == "Storms around 4 PM, then colder and windy."
    assert fall.radar.callout.detail == "22 mi west · ENE 15 mph · ~0.5 in"  # radar-based text from the fixture
    assert fall.radar.next_chip == "Next: frost possible Sunday night (34°)"
    gen = await build("fall", summary__day_line=None, radar__callout=None, summary__next_hazard=None)
    assert gen.header.day_line == "Storms around 4 PM, then colder and windy."  # same sentence, generated
    assert (gen.radar.callout.headline, gen.radar.callout.detail) == ("Storms ~4 PM", "3–8 PM · 80%")
    assert gen.radar.next_chip == "Next: frost possible Sunday night (34°)"


async def test_generated_text_on_live_data():
    live = await build("live-2026-10-24")
    assert live.header.day_line == "Partly sunny, high 72°; mostly sunny tomorrow, 75°."
    assert live.radar.callout is None and live.radar.next_chip is None
    frost = await build("frost")
    assert frost.header.day_line == "Clear tonight, low 31°; sunny tomorrow, 55°."
    assert frost.radar.next_chip == "Next: freeze likely tonight (31°)"
    winter = await build("live-winter-2027-01-19")
    assert winter.header.alert.detail == "until 9 AM Wed · 3–5 in snow"
    assert (winter.radar.callout.kind, winter.radar.callout.headline) == ("snow", "Snow by 6 PM")


async def test_day_line_fits_the_header():
    for name in ["fall", "winter", *NEW_FIXTURES]:
        h = (await build(name, summary__day_line=None)).header
        if h.day_line:
            left = 30 + 14 + measure(h.city, 22, 800, -0.01) + 14 + measure(h.date_text, 22, 300)
            right = 22 + 1 + 22 + 30 + 6 + measure(f"{h.battery_pct}%", 16, 700) + 22 + measure(h.time_text, 22, 700)
            assert left + measure(h.day_line, 17, 600) + right <= 1058, name


# ------------------------------------------------------------------ precip chart


async def test_precip_empty_state():
    live = (await build("live-2026-10-24")).precip  # every hour 0–1 %
    assert (live.title, live.right_text, live.empty_text) == ("Chance of rain", "next 12 h", messages.NO_PRECIP)
    assert live.bars == [] and live.height == 66  # same height: nothing below moves
    frost = (await build("frost")).precip  # 31° tonight
    assert frost.empty_text == messages.NO_PRECIP_COLD
    html = render_html(await build("frost"))
    assert messages.NO_PRECIP_COLD in html and 'font-size="15" font-weight="600" fill="#494949"' in html
    assert (await build("fall")).precip.empty_text is None
    assert (await build("winter")).precip.empty_text is None


async def test_precip_type_key_and_amounts():
    hourly = raw("fall", "forecast.hourly")
    for h in hourly[:12]:
        h.update(precip_type="snow", pop=60, snow_in=0.05)
    p = (await build("fall", forecast__hourly=hourly)).precip
    assert (p.title, p.patterned, p.key, p.right_text) == ("Chance of snow", True, [], "0.6 in")
    hourly[0].update(precip_type="ice", ice_in=0.03)
    hourly[1].update(precip_type="rain", pop=5)  # under 10 %: doesn't add rain to the key
    p = (await build("fall", forecast__hourly=hourly)).precip
    assert (p.title, p.key, p.right_text, p.height) == ("Chance of snow", ["ice", "snow"], "0.6 in · ice 0.03 in", 56)
    rain = raw("alert", "forecast.hourly")  # storm hours carry rain amounts
    p = (await build("alert", forecast__hourly=rain)).precip
    assert (p.title, p.right_text, p.right_bold, p.key) == ("Chance of rain", "~0.9 in", True, [])


# ------------------------------------------------------------------ gauge


@pytest.mark.parametrize("depth,season,month,winter", [
    (4, 11.8, 1, True), (0, 0, 10, False), (0, 2.5, 12, True), (1, 0, 4, True), (0.5, 0, 12, False),
    (0, 6.0, 4, False), (None, 3.0, 2, True), (None, None, 1, False),
])
def test_winter_gauge_rule(depth, season, month, winter):
    assert viewmodel.winter_gauge(depth, season, datetime(2027, month, 15)) is winter


async def test_gauge_switches_on_snow_in_season():
    g = (await build("fall", snow__depth=0, snow__season=None)).gauge
    assert g.title == "Rain gauge"
    g = (await build("winter", snow__depth=0)).gauge  # January, 11.8 in so far this season
    assert g.title == "Gauge" and g.cells[2].value.text == "0 in"


# ------------------------------------------------------------------ temperature trend


def boxes(t):
    out = []
    for _, _, lx, ly, text in t.points:
        w = measure(text, 12, 700)
        out.append((lx - w / 2, ly - 0.73 * 12, lx + w / 2, ly))
    return out


@pytest.mark.parametrize("name", ["fall", "winter", *NEW_FIXTURES])
async def test_trend_labels_inside_and_apart(name):
    t = (await build(name)).trend
    bs = boxes(t)
    for b in bs:
        assert b[1] >= 0 and b[3] <= viewmodel.TREND_AXIS + 2 and b[0] >= 0 and b[2] <= 544, (name, b)
        assert not b[0] <= t.now[0] <= b[2], (name, b)  # never across the NOW rule
    for i, a in enumerate(bs):
        for b in bs[i + 1:]:
            assert a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1], (name, a, b)
    ys = [y for _, y in t.past + t.future]
    assert all(viewmodel.TREND_TOP - 0.01 <= y <= viewmodel.TREND_AXIS + 0.01 for y in ys), name


async def test_trend_scale_covers_the_forecast_highs():
    t = (await build("live-2026-10-24")).trend
    labels = [p[4] for p in t.points]
    assert labels == ["44°", "71°", "46°", "75°", "49°", "68°"]
    hi = next(p for p in t.points if p[4] == "75°")
    assert hi[3] - 0.73 * 12 >= 0.5  # the label clears the top (the first live render clipped it)
    assert [g[1] for g in t.gridlines] == ["80°", "70°", "60°", "50°"]


async def test_trend_short_history_and_late_evening():
    t = (await build("frost")).trend
    assert 10 <= len(t.past) <= 11 and t.past[0][0] > 100  # 10 h of station history, drawn from 10 h ago
    labels = dict((s, x) for x, s in t.day_labels)
    now_w = measure("NOW", 11.5, 800)
    assert labels["MON"] >= t.now[0] + now_w / 2 + 12  # midnight is 2.5 h away: MON moves clear of NOW
    assert len(t.nights) >= 2


async def test_trend_without_history():
    t = (await build("live-2026-10-24", outdoor__temperature_history=None)).trend
    assert t.past == [] and len(t.future) > 10 and t.points


# ------------------------------------------------------------------ robustness


async def test_evening_without_todays_high():
    c = (await build("frost")).current
    assert (c.high, c.low) == (None, "L 31°")
    assert (await build("frost")).forecast[0].high == ""


@pytest.mark.parametrize("field,value", [
    ("forecast.hourly", None), ("forecast.daily", None), ("alerts.active", None), ("astro.sun", None),
    ("astro.moon", None), ("forecast.hourly", "garbage"), ("forecast.daily", [1, "x"]),
    ("alerts.active", ["x", {"event": "Flood Watch"}]), ("conditions.current", None),
    ("outdoor.temperature_history", [{"t": "bad", "v": 1}, {"t": "2026-10-24T10:00:00-05:00", "v": "x"}]),
    ("outdoor.temperature", None), ("wind.speed", None),
])
async def test_missing_or_garbled_inputs_never_crash(field, value):
    for name in ("fall", "live-2026-10-24", "frost"):
        ctx = await build(name, **{field.replace(".", "__"): value})
        assert "<html" in render_html(ctx)


def test_everything_missing():
    now = datetime.fromisoformat("2026-10-24T14:30:00-05:00")
    resolved = {f: Resolved(field=f, reading=Reading(status=Status.missing, note="no_such_key"), tried=[])
                for f in CFG.fields}
    ctx = viewmodel.build(resolved, now, DeviceInfo(), CFG)
    assert ctx.header.day_line is None and ctx.radar.callout is None and ctx.radar.next_chip is None
    assert ctx.precip.empty_text == messages.NO_HOURLY and ctx.forecast == []
    assert ctx.forecast_message == "Forecast unavailable." and ctx.header.notice  # details: tests/test_errors.py


# ------------------------------------------------------------------ renders for review


@pytest.mark.render
@pytest.mark.parametrize("name", NEW_FIXTURES)
def test_render_new_fixtures(name, needs_chromium, out_dir, tmp_path, monkeypatch):
    import io

    from fastapi.testclient import TestClient
    from PIL import Image

    monkeypatch.setenv("INKPLATE_CACHE_DIR", str(tmp_path))
    from app.main import app

    with TestClient(app) as client:
        r = client.get(f"/fixtures/{name}.png")
    assert r.status_code == 200
    img = Image.open(io.BytesIO(r.content))
    assert img.size == (1200, 825)
    img.save(out_dir / f"{name}.png")
