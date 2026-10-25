"""Generated text (app/summary.py): day line, radar callout, next-hazard chip, fitted with app/textwidth."""
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import summary, textwidth
from app.paths import FIXTURES_DIR

TZ = ZoneInfo("America/Chicago")
WIDE = 10_000


def readings(name: str) -> tuple[datetime, dict]:
    d = json.loads((FIXTURES_DIR / f"{name}.json").read_text(encoding="utf-8"))
    r = {k: (v["value"] if isinstance(v, dict) and "value" in v and "observed_at" in v else v)
         for k, v in d["readings"].items()}
    return datetime.fromisoformat(d["now"]).astimezone(TZ), r


def inputs(name: str, **override) -> summary.Inputs:
    now, r = readings(name)
    r.update(override)
    return summary.Inputs.build(now, TZ, r.get("forecast.hourly"), r.get("forecast.daily"),
                                r.get("conditions.current"), r.get("alerts.active"), r.get("outdoor.temperature"))


def budget(name: str) -> float:
    now, _ = readings(name)
    return summary.header_budget("Savoy, IL", f"{now:%A} {now.day} {now:%B}", 78, "2:30 PM")


# ------------------------------------------------------------------ day line


@pytest.mark.parametrize("name,expected", [
    # The reference boards' own sentences come out of their forecast data.
    ("fall", "Storms around 4 PM, then colder and windy."),
    ("winter", "Sleet changing to snow by 6 PM; 3–5 in by morning."),
    # Real NWS responses recorded for Savoy at 11:45 AM on 2026-10-24 (a dry, warm week).
    ("live-2026-10-24", "Partly sunny, high 72°; mostly sunny tomorrow, 75°."),
    # The crafted winter storm parsed by the real NWS provider.
    ("live-winter-2027-01-19", "Sleet changing to snow by 6 PM; 4–6 in by morning."),
    ("frost", "Clear tonight, low 31°; sunny tomorrow, 55°."),
    ("alert", "Storms until 8 PM; ~0.9 in, then colder."),
])
def test_day_line_from_forecast(name, expected):
    assert summary.day_line(inputs(name), budget(name)) == expected


def test_day_line_candidates_most_complete_first():
    assert summary.day_line_candidates(inputs("winter")) == [
        "Sleet changing to snow by 6 PM; 3–5 in by morning, then colder and windy.",
        "Sleet changing to snow by 6 PM; 3–5 in by morning.",
        "Sleet changing to snow by 6 PM, then colder and windy.",
        "Sleet changing to snow by 6 PM.",
    ]


@pytest.mark.parametrize("room", [600, 420, 300, 200, 150])
def test_day_line_always_fits(room):
    for name in ("fall", "winter", "live-2026-10-24", "frost", "alert", "live-winter-2027-01-19"):
        line = summary.day_line(inputs(name), room)
        assert line is None or textwidth.measure(line, 17, 600) <= room


def test_day_line_fit_picks_the_most_complete_phrasing():
    inp = inputs("winter")
    assert summary.day_line(inp, WIDE).endswith("then colder and windy.")
    assert summary.day_line(inp, 300) == "Sleet changing to snow by 6 PM."
    assert summary.day_line(inp, 100) is None  # nothing fits: no day line rather than an overflow


def test_dry_day_line_wording():
    now = datetime(2026, 10, 25, 9, 30, tzinfo=TZ)
    daily = [{"date": "2026-10-25", "text": "Partly sunny", "high": 75, "low": 49},
             {"date": "2026-10-26", "text": "Partly sunny", "high": 77, "low": 50}]
    hours = [{"t": (now.replace(minute=0) + timedelta(hours=i)).isoformat(), "temp": 70, "pop": 2}
             for i in range(0, 30)]
    inp = summary.Inputs.build(now, TZ, hours, daily, "partly-cloudy")
    assert summary.day_line(inp, WIDE) == "Partly sunny, high 75°; again tomorrow, 77°."
    inp = summary.Inputs.build(now.replace(hour=19), TZ, hours, daily, "partly-cloudy-night")
    assert summary.day_line(inp, WIDE).endswith("; partly sunny tomorrow, 77°.")  # no "again" after a night lead
    daily[0]["text"] = "Snow → blowing snow"  # the 7-day list's short text reads as a sentence here
    inp = summary.Inputs.build(now, TZ, hours, daily, "snow")
    assert summary.day_line(inp, WIDE).startswith("Snow then blowing snow, high 75°;")


def test_snow_chip_counts_only_snow_still_to_come():
    # 3:30 AM, snow ending at 7 AM: the day's NWS total (mostly fallen overnight) must not raise a "snow today" chip.
    now = datetime(2027, 1, 20, 3, 30, tzinfo=TZ)
    hours = [{"t": (now.replace(minute=0) + timedelta(hours=i)).isoformat(), "temp": 20,
              "pop": 70 if i < 4 else 5, "precip_type": "snow", "snow_in": 0.2 if i < 4 else 0}
             for i in range(0, 48)]
    daily = [{"date": "2027-01-20", "high": 22, "low": 10, "snow_in": 1.8},
             {"date": "2027-01-21", "high": 24, "low": 9, "snow_in": 0}]
    inp = summary.Inputs.build(now, TZ, hours, daily, "snow")
    assert not [z for z in summary.hazards(inp) if z.kind == "snow"]
    daily[1]["snow_in"] = 2.5  # tomorrow's total still counts
    assert [z.text for z in summary.hazards(summary.Inputs.build(now, TZ, hours, daily, "snow"))
            if z.kind == "snow"] == ["snow Thursday (2–4 in)"]


def test_day_line_falls_back_to_nws_wording():
    inp = inputs("live-2026-10-24", **{"forecast.hourly": []})
    assert summary.day_line(inp, WIDE) == "Partly sunny, with a high near 72. Southeast wind around 6 mph."
    assert summary.day_line(inp, 300) == "Partly sunny, with a high near 72."


def test_header_budget_matches_the_board():
    # header 1064 px − left group (~352) − right group without the day line (~220) − 24 px breathing room
    assert budget("fall") == pytest.approx(468, abs=1)
    assert summary.header_budget("Savoy, IL", "Wednesday 30 September", None, "12:30 PM") < budget("fall") + 30


def test_chance_and_onset_wording():
    now = datetime(2026, 10, 24, 9, 30, tzinfo=TZ)
    hours = [{"t": (now.replace(minute=0) + timedelta(hours=i)).isoformat(), "temp": 60,
              "pop": 40 if 4 <= i <= 6 else 5} for i in range(1, 30)]
    daily = [{"date": "2026-10-24", "icon": "rain", "text": "Showers", "high": 64, "low": 50},
             {"date": "2026-10-25", "icon": "sunny", "text": "Sunny", "high": 66, "low": 48}]
    inp = summary.Inputs.build(now, TZ, hours, daily, "cloudy")
    assert summary.day_line_candidates(inp)[-1] == "Chance of rain around 1 PM."
    assert summary.callout(inp) is None  # 40 % rain is not notable
    for h in hours:
        h["pop"] = 80 if hours.index(h) <= 2 else 5  # 10 AM to 1 PM, raining now
    inp = summary.Inputs.build(now, TZ, hours, daily, "rain")
    assert summary.day_line_candidates(inp)[-1] == "Rain until 1 PM."
    assert summary.callout(inp) == {"kind": "rain", "headline": "Rain now", "detail": "until 1 PM · 80%"}


# ------------------------------------------------------------------ callout and next chip


def test_callouts():
    assert summary.callout(inputs("winter")) == {
        "kind": "snow", "headline": "Snow by 6 PM", "detail": "Sleet now · heaviest 7–11 PM · 3–5 in"}
    assert summary.callout(inputs("fall")) == {"kind": "storm", "headline": "Storms ~4 PM", "detail": "3–8 PM · 80%"}
    assert summary.callout(inputs("alert")) == {
        "kind": "storm", "headline": "Storms now", "detail": "until 8 PM · 80% · ~0.9 in"}
    assert summary.callout(inputs("live-winter-2027-01-19")) == {
        "kind": "snow", "headline": "Snow by 6 PM", "detail": "Sleet now · heaviest 6 PM–midnight · 4–6 in"}
    assert summary.callout(inputs("live-2026-10-24")) is None
    assert summary.callout(inputs("frost")) is None


def test_callout_for_an_alert_without_precipitation():
    alert = {"event": "Wind Advisory", "ends": "2026-10-24T21:00:00-05:00", "impact": "gusts to 50 mph",
             "severity": "Moderate"}
    inp = inputs("live-2026-10-24", **{"alerts.active": [alert]})
    assert summary.callout(inp) == {"kind": "alert", "headline": "Wind Advisory",
                                    "detail": "until 9 PM · gusts to 50 mph"}


@pytest.mark.parametrize("name,expected", [
    ("fall", "frost possible Sunday night (34°)"),
    ("frost", "freeze likely tonight (31°)"),
    ("alert", "frost possible Sunday night (34°)"),  # the storm's 50 mph gusts belong to the callout
    ("live-2026-10-24", None),
    ("live-winter-2027-01-19", None),  # the second day of snow is the same storm as the callout
])
def test_next_hazard(name, expected):
    inp = inputs(name)
    assert summary.next_hazard(inp, summary.callout_event(inp)) == expected


def test_winter_next_hazard_is_wind_chill():
    # The crafted winter hourly data has no wind, so the day's wind stands in: −1° as Wednesday evening starts.
    inp = inputs("winter")
    assert summary.next_hazard(inp, summary.callout_event(inp)) == "wind chill −1° Wednesday evening"
    assert summary.wind_chill(11, 10) == pytest.approx(-2.3, abs=0.1)  # the board's "−2° Thursday morning"
    assert summary.wind_chill(55, 20) is None and summary.wind_chill(20, 2) is None


def test_hazard_kinds():
    now = datetime(2026, 7, 10, 9, 0, tzinfo=TZ)
    hours = [{"t": (now + timedelta(hours=i)).isoformat(), "temp": 98 if 28 <= i <= 32 else 85, "pop": 0,
              "humidity": 50, "wind_gust": 45 if i == 50 else 15, "rain_in": 0.4 if 60 <= i <= 62 else 0}
             for i in range(1, 72)]
    inp = summary.Inputs.build(now, TZ, hours, [], "sunny")
    texts = [z.text for z in summary.hazards(inp)]
    assert texts == ["heat index 113° Saturday afternoon", "gusts to 45 mph Sunday morning",
                     "heavy rain Sunday (~1.2 in)"]
    inp = summary.Inputs.build(now.replace(month=10), TZ, [], [{"date": "2026-10-11", "low": 33}], "sunny")
    assert [z.text for z in summary.hazards(inp)] == ["frost possible Sunday night (33°)"]
    inp = summary.Inputs.build(now.replace(month=1), TZ, [], [{"date": "2026-01-11", "low": 20}], "sunny")
    assert summary.hazards(inp) == []  # no frost chips in midwinter


# ------------------------------------------------------------------ formatting helpers


def test_formatting():
    t = datetime(2027, 1, 19, 19, tzinfo=TZ)
    assert summary.hour_range(t, t + timedelta(hours=4)) == "7–11 PM"
    assert summary.hour_range(t + timedelta(hours=3), t + timedelta(hours=7)) == "10 PM–2 AM"
    assert summary.hour_range(t - timedelta(hours=1), t + timedelta(hours=5)) == "6 PM–midnight"
    assert [summary.clock_hour(t.replace(hour=h)) for h in (0, 9, 12, 16)] == ["midnight", "9 AM", "noon", "4 PM"]
    assert [summary.snow_range(x) for x in (0.4, 1.0, 2.4, 4.0, 4.78, 6.4, 8.2)] == [
        "0.4 in", "1–2 in", "1–2 in", "3–5 in", "4–6 in", "5–7 in", "6–10 in"]
    now = datetime(2026, 10, 24, 14, 30, tzinfo=TZ)
    assert summary.when(now.replace(hour=22), now) == "tonight"
    assert summary.when(now + timedelta(days=1, hours=13), now) == "Sunday night"  # 3:30 AM Monday
    assert summary.when(now + timedelta(days=5, hours=-7), now) == "Thursday morning"
    assert summary.when(now.replace(hour=16), now) == "this afternoon"


def test_most_severe_alert():
    a = [{"event": "Wind Advisory", "severity": "Moderate"}, {"event": "Tornado Warning", "severity": "Extreme"},
         {"event": "Severe Thunderstorm Warning", "severity": "Severe"}]
    assert summary.most_severe(a)["event"] == "Tornado Warning"
    tie = a[:1] + [{"event": "Flood Advisory", "severity": "Moderate"}]
    assert summary.most_severe(tie)["event"] == "Wind Advisory"  # the source's order breaks ties
    assert summary.most_severe([]) is None


def test_text_width_table_tracks_chromium():
    # Chromium measured 337.28 px for the fall day line in the bundled font; the table must not under-measure.
    w = textwidth.measure("Storms around 4 PM, then colder and windy.", 17, 600)
    assert 337.2 <= w <= 341
    assert textwidth.measure("NOW", 11.5, 800) < textwidth.measure("NOW", 11.5, 800, 0.1)
    assert textwidth.measure("€", 10) == textwidth.measure("M", 10)  # unknown glyphs measure wide


def test_survives_garbage():
    now = datetime(2026, 10, 24, 14, 30, tzinfo=TZ)
    inp = summary.Inputs.build(now, TZ, [{"t": "nonsense"}, "x", {"t": now.isoformat(), "temp": "warm"}],
                               ["x", {"date": "bad"}], {"name": "rain", "windy": True}, ["x"])
    assert summary.day_line(inp, WIDE) is None
    assert summary.callout(inp) is None and summary.next_hazard(inp) is None
