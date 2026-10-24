"""Error states (ARCHITECTURE: Error handling; BUILD_PLAN 2.6): stale and missing values, fallback notes, the header
summary, battery notes, the alert-tag abbreviations and the error strip burned into the last good image."""
import io
import os
import re
import time
from datetime import datetime, timedelta

import pytest
from conftest import REPO_DIR, TEST_CONFIG
from markupsafe import escape
from PIL import Image

from app import collector, config, messages, summary, viewmodel
from app.config import SourceCfg
from app.main import build_context, burn_error_strip, render_failure_reason
from app.providers.base import DeviceInfo, RequestContext
from app.providers.fixture import fixture_device, fixture_now
from app.readings import Reading, Status
from app.render import render_html
from app.render.quantize import LEVELS
from app.resolver import Attempt, Resolved, resolve_field
from app.textwidth import measure

CFG = config.load(TEST_CONFIG)  # fixture-only field config
LIVE_CFG = config.load(REPO_DIR / "config" / "config.example.yaml")  # real sources: fallback notes name them
TZ = "America/Chicago"
NOW = datetime.fromisoformat("2026-10-24T14:30:00-05:00")


async def ctx_for(name, cfg=LIVE_CFG, device=None):
    return await build_context(cfg, name, device)


async def build(name, cfg=CFG, device=None, **override):
    """Fixture context with fields replaced by Resolved objects (or values) given as keyword overrides."""
    now = fixture_now(name)
    resolved = dict(await collector.collect_fixture(name, RequestContext(now=now)))
    for key, value in override.items():
        field = key.replace("__", ".")
        resolved[field] = value if isinstance(value, Resolved) else Resolved(
            field=field, reading=Reading(value=value, observed_at=now, source="test"), tried=[])
    return viewmodel.build(resolved, now, device or fixture_device(name), cfg)


def src(name, max_age=None):
    return SourceCfg.model_validate({"from": name, **({"max_age": max_age} if max_age else {})})


def failed(field, *attempts):
    """Resolve `field` from sources that all fail: attempts are (source, result) with result an exception or Reading."""
    results = {s: (r if isinstance(r, BaseException) else {field: r}) for s, r in attempts}
    return resolve_field(field, [src(s) for s, _ in attempts], results, NOW)


# ------------------------------------------------------------------ wording


@pytest.mark.parametrize("note,source,expected", [
    ("nws_timeout", "nws_fc", "NWS didn't respond"),
    ("nws_http_404", "nws_fc", "NWS didn't respond"),
    ("nws_rate_limited", "nws_obs", "NWS is busy"),
    ("nws_empty_forecast", "nws_fc", "NWS sent an empty forecast"),
    ("no_observations", "nws_obs", "the airport station hasn't reported"),
    ("no_value", "ecowitt", "the weather station didn't report it"),
    ("bad_value", "ecowitt", "the weather station sent a bad value"),
    ("unavailable", "ha", "Home Assistant is unavailable"),
    ("unknown", "ha", "Home Assistant has no reading yet"),
    ("not_in_request", "device", "the display didn't send it"),
    ("http_503", "om", "Open-Meteo didn't respond"),
    ("timeout", "om", "Open-Meteo didn't respond"),
    ("unreachable", "om", "Open-Meteo couldn't be reached"),
    ("provider_error:ConnectError", "ecowitt", "the weather station didn't respond"),
    ("insufficient_history", "history", "the station history hasn't recorded enough yet"),
    ("something_new", "acis", "NOAA climate data had a problem"),
])
def test_note_codes_become_friendly_causes(note, source, expected):
    assert messages.cause(note, source) == expected


def test_instance_names_resolve_through_the_config_types():
    types = {"station": "ecowitt_local", "fixture": "fixture"}
    assert messages.source_name("station", types) == "the weather station"
    assert messages.source_name("om", types) == "Open-Meteo"  # example-config name, no config needed
    assert messages.source_name("fixture", types) == "the test data"
    assert messages.short_source_name("ecowitt") == "weather station"


def test_explain_sentences():
    r = failed("forecast.daily", ("nws_fc", Reading(status=Status.error, note="nws_timeout")),
               ("om", Reading(status=Status.error, note="timeout")))
    assert messages.explain(r, TZ) == "Forecast unavailable: NWS and Open-Meteo didn't respond. Retrying next update."
    r = failed("air.aqi", ("ha", Reading(status=Status.missing, note="unknown")))
    assert messages.explain(r, TZ) == "Air quality unavailable: Home Assistant has no reading yet."
    r = failed("indoor.temperature", ("ecowitt", Reading(status=Status.missing, note="no_value",
                                                         observed_at=NOW - timedelta(minutes=78))))
    assert messages.explain(r, TZ) == "Inside sensor hasn't reported since 1:12 PM."
    assert messages.short(r, TZ) == "since 1:12 PM"
    r = failed("alerts.active", ("nws_alerts", RuntimeError("boom")))
    assert messages.explain(r, TZ) == "Weather alerts unavailable: NWS didn't respond. Retrying next update."
    assert messages.short(r, TZ) == "no response"


def test_grouped_notes():
    since = NOW - timedelta(hours=2, minutes=50)
    rs = [failed(f, ("ecowitt", Reading(status=Status.missing, note="no_value", observed_at=since)))
          for f in ("indoor.temperature", "garage.temperature", "rain.today", "rain.week")]
    assert messages.missing_notes(rs, TZ) == [
        "Inside sensor, garage sensor and rain gauge haven't reported since 11:40 AM."]
    assert messages.missing_notes(rs[:1], TZ) == ["Inside sensor hasn't reported since 11:40 AM."]
    stale = [Resolved(field=f, reading=Reading(value=1, status=Status.stale, observed_at=since), tried=[])
             for f in ("pressure.sea_level", "uv.index")]
    assert messages.stale_notes(stale, TZ) == ["Pressure and UV index as of 11:40 AM."]


def test_sections_missing_is_shortened_to_fit():
    secs = ["alerts", "radar", "forecast", "hourly", "current", "readings"]
    assert messages.sections_missing(secs[:2]) == "Some data is unavailable: alerts, radar."
    want = "Some data is unavailable: alerts, radar and 4 more."
    budget = measure(want, summary.DAY_LINE_SIZE, summary.DAY_LINE_WEIGHT) + 1
    assert messages.sections_missing(secs, lambda t: summary.fits(t, budget)) == want
    assert messages.sections_missing(secs, lambda t: False) == messages.SECTIONS_MISSING_SHORT


def no_jargon(text: str) -> None:
    """No note codes, exception names or provider instance names on screen."""
    assert not re.search(r"\w_\w|Error|Exception|Timeout|None\b|nws_fc|\bom\b", text), text


# ------------------------------------------------------------------ outage fixture


async def test_outage_board():
    ctx = await ctx_for("outage")
    h = ctx.header
    assert h.notice == "Some data is unavailable: alerts, readings, rain gauge."
    assert ctx.notices == [
        "Weather alerts unavailable: NWS didn't respond. Retrying next update.",
        "Inside sensor, garage sensor and rain gauge haven't reported since 11:40 AM.",
        "Air quality hasn't reported since 9:10 AM.",
        "Forecast from Open-Meteo (NWS unavailable).",
        "Outdoor readings from NWS (weather station unavailable).",
        "UV index from Open-Meteo (weather station unavailable).",
    ]
    cells = {c.label: c.slot for c in ctx.readings}
    assert (cells["Inside"].message, cells["Inside"].sub) == ("No data", "since 11:40 AM")
    assert (cells["Air quality"].message, cells["Air quality"].sub) == ("No data", "since 9:10 AM")
    assert cells["Pressure ↓"].text == "29.95" and cells["UV index"].text == "2"
    assert [c.value.message for c in ctx.gauge.cells] == ["No data"] * 4
    assert ctx.radar.message == messages.RADAR_LATER  # phase 2: no radar source, not counted as missing
    assert ctx.current.temp.text == "57°" and len(ctx.forecast) == 7
    html = render_html(ctx)
    assert h.notice in html and h.day_line not in html
    for n in ctx.notices:
        assert str(escape(n)) in html
        no_jargon(n)


async def test_fixture_fallback_without_a_real_config_names_only_the_winner():
    ctx = await ctx_for("outage", cfg=CFG)
    assert "Forecast from Open-Meteo." in ctx.notices


async def test_fallback_note_only_when_the_primary_failed():
    ok = Reading(value=[{"date": "2026-10-24", "high": 60, "low": 40}], observed_at=NOW, source="om")
    down = Resolved(field="forecast.daily", reading=ok, tried=[
        Attempt(source="nws_fc", key="forecast.daily", outcome=Status.error, note="nws_timeout"),
        Attempt(source="om", key="forecast.daily", outcome=Status.ok)])
    ctx = await build("fall", cfg=LIVE_CFG, forecast__daily=down)
    assert "Forecast from Open-Meteo (NWS unavailable)." in ctx.notices
    no_key = Resolved(field="uv.index", reading=Reading(value=3, observed_at=NOW, source="om"), tried=[
        Attempt(source="ecowitt", key="0x17", outcome=Status.missing, note="no_such_key"),
        Attempt(source="om", key="uv.index", outcome=Status.ok)])
    ctx = await build("fall", cfg=LIVE_CFG, uv__index=no_key)
    assert ctx.notices == []  # the station simply has no UV sensor: a config matter, not an outage


# ------------------------------------------------------------------ stale fixture


async def test_stale_board():
    ctx = await ctx_for("stale")
    c = ctx.current
    assert c.temp.status is Status.stale and c.as_of == "as of 1:50 PM"
    assert c.feels_stale and c.wind_stale and not c.high_low_stale and not c.normals_stale
    assert [cell.slot.status for cell in ctx.readings] == [Status.stale, Status.stale, Status.stale, Status.ok,
                                                           Status.stale]
    assert all(cell.value.status is Status.stale for cell in ctx.gauge.cells)
    assert ctx.notices == ["Inside sensor, garage sensor, pressure, UV index and rain gauge as of 1:50 PM."]
    assert ctx.header.notice is None and ctx.header.day_line == "Storms around 4 PM, then colder and windy."
    html = render_html(ctx)
    assert "color: #6D6D6D\">58°" in html and ">as of 1:50 PM<" in html


async def test_ok_boards_have_no_error_states():
    for name in ("fall", "winter"):
        ctx = await ctx_for(name)
        assert ctx.notices == [] and ctx.header.notice is None and ctx.current.as_of is None
        assert ctx.header.battery_text is None and ctx.forecast_message is None


# ------------------------------------------------------------------ battery


@pytest.mark.parametrize("pct,text,strip", [
    (16, None, None), (15, messages.BATTERY_LOW, None), (12, messages.BATTERY_LOW, None),
    (5, messages.BATTERY_LOW, "Display battery 5%: charge it now or the screen will stop updating."),
])
async def test_battery_notes(pct, text, strip):
    ctx = await ctx_for("battery-low", device=DeviceInfo(battery_pct=pct))
    assert (ctx.header.battery_text, ctx.header.battery_strip) == (text, strip)


async def test_battery_low_day_line_yields():
    ctx = await ctx_for("battery-low")
    h = ctx.header
    assert h.battery_pct == 12 and h.battery_text == messages.BATTERY_LOW
    # The fixture's day line no longer fits beside the battery note: a shorter generated one is used.
    budget = summary.header_budget(h.city, h.date_text, h.battery_pct, h.time_text) - (
        viewmodel.BATTERY_TEXT_GAP + measure(messages.BATTERY_LOW, 16, 600))
    assert h.day_line == "Storms around 4 PM." and summary.fits(h.day_line, budget)
    html = render_html(await ctx_for("battery-low", device=DeviceInfo(battery_pct=4)))
    assert messages.BATTERY_LOW in html and 'role="alert"' in html
    assert measure(messages.BATTERY_STRIP.format(pct=100), 15, 600) <= 530 - 24  # one line in the strip


# ------------------------------------------------------------------ sections


async def test_missing_sections_say_so():
    def gone(field):
        return Resolved(field=field, reading=Reading(status=Status.missing, note="nws_timeout"),
                        tried=[Attempt(source="nws_fc", key=field, outcome=Status.error, note="nws_timeout")])

    ctx = await build("fall", forecast__daily=gone("forecast.daily"), forecast__hourly=gone("forecast.hourly"),
                      outdoor__temperature_history=None, astro__sun=gone("astro.sun").model_copy(
                          update={"tried": []}))
    assert ctx.forecast == [] and ctx.forecast_message == (
        "Forecast unavailable: NWS didn't respond. Retrying next update.")
    assert ctx.precip.empty_text == messages.NO_HOURLY
    assert ctx.sun.message == "Sun times unavailable." and ctx.moon.message is None
    assert ctx.header.notice == "Some data is unavailable: forecast, sun and moon."
    assert ctx.notices == []  # the forecast list itself carries the cause
    html = render_html(ctx)
    for text in (ctx.forecast_message, messages.NO_HOURLY, ctx.sun.message):
        assert str(escape(text)) in html
    ctx = await build("fall", forecast__hourly=gone("forecast.hourly"))
    assert ctx.notices == ["Hourly forecast unavailable: NWS didn't respond. Retrying next update."]
    assert ctx.header.notice is None  # one section only: the day line stays


async def test_trend_messages():
    ctx = await build("fall", outdoor__temperature_history=None)
    assert ctx.trend.past_message == messages.NO_HISTORY and ctx.trend.future
    ctx = await build("fall", outdoor__temperature_history=None, forecast__hourly=None, outdoor__temperature=None)
    assert ctx.trend.message == messages.NO_TREND
    assert messages.NO_TREND in render_html(ctx)


async def test_radar_failure_counts_once_a_source_is_configured():
    down = Resolved(field="radar.frame", reading=Reading(status=Status.missing, note="provider_error:TimeoutError"),
                    tried=[{"source": "mrms", "key": "radar.frame", "outcome": "error",
                            "note": "provider_error:TimeoutError"}])
    ctx = await build("fall", radar__frame=down, alerts__active=Resolved(
        field="alerts.active", reading=Reading(status=Status.error, note="nws_server_error"),
        tried=[{"source": "nws_alerts", "key": "alerts.active", "outcome": "error", "note": "nws_server_error"}]))
    assert ctx.radar.message == "Radar unavailable: NOAA's radar feed didn't respond. Retrying next update."
    assert ctx.header.notice == "Some data is unavailable: alerts, radar."


# ------------------------------------------------------------------ alert tag abbreviations (2026-10-24)


def test_abbreviation_table():
    ab = messages.abbreviate_event
    assert ab("Severe Thunderstorm Warning") == "Svr T-storm Warning"
    assert ab("Severe Thunderstorm Watch") == "Svr T-storm Watch"
    assert ab("Thunderstorm Warning") == "T-storm Warning"
    assert ab("Winter Weather Advisory") == "Winter Wx Adv."
    assert ab("Special Weather Statement") == "Special Wx Stmt"
    assert ab("Excessive Heat Warning") == "Exc. Heat Warning"
    assert ab("Winter Storm Warning") == "Winter Storm Warning"


async def test_long_alert_name_is_abbreviated_before_the_impact_is_dropped():
    alert = [{"event": "Severe Thunderstorm Warning", "ends": "2026-09-23T15:15:00-05:00", "impact": "hail 1 in",
              "severity": "Severe"}]
    # A long date ("Wednesday 23 September") and a full battery leave ~400 px: the full tag + detail (420 px)
    # doesn't fit, the abbreviated tag + full detail does.
    now = datetime.fromisoformat("2026-09-23T12:45:00-05:00")
    resolved = dict(await collector.collect_fixture("fall", RequestContext(now=now)))
    resolved["alerts.active"] = Resolved(field="alerts.active", reading=Reading(value=alert, observed_at=now),
                                         tried=[])
    ctx = viewmodel.build(resolved, now, DeviceInfo(battery_pct=100), CFG)
    assert (ctx.header.alert.event, ctx.header.alert.detail) == ("Svr T-storm Warning", "until 3:15 PM · hail 1 in")
    html = render_html(ctx)
    assert "text-transform: uppercase" in html and "Svr T-storm Warning" in html  # shown as SVR T-STORM WARNING
    # With room to spare (the fall board's header) the full name stays: rule 1 of the fitting order.
    alert[0]["ends"] = "2026-10-24T15:15:00-05:00"
    fall = await build("fall", alerts__active=alert)
    assert (fall.header.alert.event, fall.header.alert.detail) == ("Severe Thunderstorm Warning",
                                                                   "until 3:15 PM · hail 1 in")
    winter = await ctx_for("winter")
    assert winter.header.alert.event == "Winter Storm Warning"
    assert winter.header.alert.detail == "until 9 AM Wed · 3–5 in snow"


async def test_alert_never_overflows():
    alert = [{"event": "Severe Thunderstorm Warning", "ends": "2026-10-24T15:15:00-05:00",
              "impact": "80 mph gusts, 2 in hail, tornado possible, dangerous lightning", "severity": "Severe"}]
    ctx = await build("fall", alerts__active=alert, device=DeviceInfo(battery_pct=3))
    a = ctx.header.alert
    h = ctx.header
    budget = summary.header_budget(h.city, h.date_text, h.battery_pct, h.time_text, gap=18) - (
        viewmodel.BATTERY_TEXT_GAP + measure(messages.BATTERY_LOW, 16, 600))
    assert summary.alert_width(a.event, a.detail) <= budget
    assert a.event == "Svr T-storm Warning" and a.detail == "until 3:15 PM"


# ------------------------------------------------------------------ error strip


def test_error_strip_reasons_and_text(tmp_path, monkeypatch):
    from app.main import error_strip_text

    assert render_failure_reason(TimeoutError()) == messages.RENDER_REASON_TIMEOUT
    assert render_failure_reason(ValueError("x")) == messages.RENDER_REASON_OTHER
    monkeypatch.setenv("INKPLATE_CACHE_DIR", str(tmp_path))
    at = datetime.fromisoformat("2026-10-24T15:00:20-05:00")
    assert error_strip_text(CFG, at, ValueError()) == (
        "Display couldn't update at 3:00 PM: the server hit an error. Showing the last good screen.")
    (tmp_path / "last-good.png").write_bytes(b"x")
    for when, shown in (("2026-10-24T14:30:15-05:00", "2:30 PM"), ("2026-10-23T22:00:15-05:00", "Fri 10:00 PM")):
        t = datetime.fromisoformat(when).timestamp()
        os.utime(tmp_path / "last-good.png", (t, t))
        text = error_strip_text(CFG, at, TimeoutError())
        assert text == ("Display couldn't update at 3:00 PM: drawing the screen took too long. "
                        f"Showing the screen from {shown}.")
        no_jargon(text)


def test_error_strip_keeps_8_levels(out_dir):
    img = Image.new("L", (1200, 825), 255)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    text = messages.render_failed("3:00 PM", messages.RENDER_REASON_BROWSER, "2:30 PM")
    out = Image.open(io.BytesIO(burn_error_strip(buf.getvalue(), text)))
    assert out.size == (1200, 825) and out.mode == "L"
    assert {v for v, n in enumerate(out.histogram()) if n} <= set(LEVELS)
    assert out.getpixel((5, 60)) == 0 and out.getpixel((5, 200)) == 255  # band over the header only


def test_display_png_falls_back_to_last_good_with_strip(tmp_path, monkeypatch, out_dir):
    from fastapi.testclient import TestClient

    import app.main as main

    monkeypatch.setenv("INKPLATE_CACHE_DIR", str(tmp_path))
    ref = REPO_DIR / "design" / "reference" / "dashboard-eink.png"
    base = Image.open(ref).convert("L") if ref.is_file() else Image.new("L", (1200, 825), 255)
    base.save(tmp_path / "last-good.png")
    earlier = time.time() - 30 * 60  # the last good screen is from the previous half hour
    os.utime(tmp_path / "last-good.png", (earlier, earlier))

    async def boom(*a, **k):
        raise TimeoutError("render took too long")

    monkeypatch.setattr(main, "build_context", boom)
    with TestClient(main.app) as client:
        r = client.get("/display.png?batt=50")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    img = Image.open(io.BytesIO(r.content))
    assert img.size == (1200, 825) and {v for v, n in enumerate(img.histogram()) if n} <= set(LEVELS)
    assert img.getpixel((5, 60)) == 0
    img.save(out_dir / "error-strip.png")


# ------------------------------------------------------------------ renders for review


@pytest.mark.render
@pytest.mark.parametrize("name,batt", [("outage", None), ("stale", None), ("battery-low", None),
                                       ("battery-low", 4)])
async def test_render_error_boards(name, batt, needs_chromium, out_dir):
    from app.main import render_png
    from app.render.browser import Renderer

    ctx = await ctx_for(name, device=DeviceInfo(battery_pct=batt) if batt else None)
    r = Renderer()
    try:
        png = await render_png(r, ctx)
    finally:
        await r.close()
    img = Image.open(io.BytesIO(png))
    assert img.size == (1200, 825) and {v for v, n in enumerate(img.histogram()) if n} <= set(LEVELS)
    img.save(out_dir / f"{name}{f'-{batt}' if batt else ''}.png")
