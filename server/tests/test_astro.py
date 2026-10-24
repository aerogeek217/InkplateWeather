"""astro provider verified against timeanddate.com for 40.0617 N, 88.2517 W (Savoy, IL), read 2026-10-24.

Reference pages: timeanddate.com/sun/@40.0617,-88.2517 and /moon/@40.0617,-88.2517 (Oct 2026, Jan 2027),
/moon/phases/@40.0617,-88.2517 (2026, 2027). Local times (CDT in October, CST in January). Moon illumination there
is given at lunar noon (meridian passing). (USNO's API was not reachable from the build machine.)
"""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import shapes
from app.config import AppConfig, LocationCfg, ProviderCfg
from app.providers import astro, base
from app.providers.base import RequestContext
from app.readings import Status

TZ = ZoneInfo("America/Chicago")
LAT, LON = 40.0617, -88.2517
APP = AppConfig(location=LocationCfg(name="Savoy, IL", lat=LAT, lon=LON, tz="America/Chicago"))


def at(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=TZ)


def close(a: datetime | None, b: datetime, minutes: float) -> bool:
    return a is not None and abs((a - b).total_seconds()) <= minutes * 60


# (render time, sunrise, sunset, day length (min), Δ/day (min), moonrise today, the following moonset)
SUN_MOON = [
    ("2026-10-24T14:30", "2026-10-24T07:13", "2026-10-24T18:00", 647, -2, "2026-10-24T16:57", "2026-10-25T06:39"),
    ("2027-01-19T15:30", "2027-01-19T07:11", "2027-01-19T16:56", 585, 2, "2027-01-19T13:34", "2027-01-20T05:43"),
]


@pytest.mark.parametrize("now,rise,sset,length,delta,mrise,mset", SUN_MOON)
def test_sun_and_moon_times(now, rise, sset, length, delta, mrise, mset):
    now = at(now)
    s = astro.sun_info(LAT, LON, now.date(), TZ)
    assert close(s.sunrise, at(rise), 1) and close(s.sunset, at(sset), 1)
    assert abs(s.day_length_min - length) <= 1
    assert s.delta_min_per_day == delta
    m = astro.moon_info(LAT, LON, now, TZ)
    assert close(m.rise, at(mrise), 2) and close(m.set, at(mset), 2)


# (time of lunar noon, illumination % there)
ILLUMINATION = [("2026-10-24T23:42", 98.6), ("2027-01-19T21:39", 91.7), ("2026-10-09T11:52", 1.1),
                ("2026-10-18T19:10", 53.3), ("2027-01-29T05:41", 49.8), ("2027-01-07T11:56", 0.1),
                ("2026-10-26T00:33", 99.8), ("2026-11-01T05:38", 54.3), ("2026-11-05T08:49", 13.6),
                ("2026-11-09T11:52", 0.4), ("2026-11-12T14:25", 11.6)]


@pytest.mark.parametrize("t,pct", ILLUMINATION)
def test_illumination(t, pct):
    frac, _ = astro.moon_phase_angle(at(t))
    assert abs(frac * 100 - pct) <= 1.0


PHASES = [
    ("2026-10-24T14:30", [("full", "2026-10-25T23:11"), ("last", "2026-11-01T14:28"), ("new", "2026-11-09T01:02"),
                          ("first", "2026-11-17T05:47")]),
    ("2027-01-19T15:30", [("full", "2027-01-22T06:17"), ("last", "2027-01-29T04:55"), ("new", "2027-02-06T09:56"),
                          ("first", "2027-02-14T01:58")]),
]


@pytest.mark.parametrize("now,expected", PHASES)
def test_next_principal_phases(now, expected):
    got = astro.principal_phases(at(now), 4)
    assert [k for k, _ in got] == [k for k, _ in expected]
    for (_, t), (_, ref) in zip(got, expected, strict=True):
        assert close(t, at(ref), 2)


def test_meeus_example_49a():
    # Meeus, Astronomical Algorithms, example 49.a: new moon 1977 Feb 18, 03:37:42 TD (≈ 03:36:54 UT).
    t = astro.phase_time(-283)
    assert close(t, datetime.fromisoformat("1977-02-18T03:36:54+00:00"), 1.5)


@pytest.mark.parametrize("now,name,pct,waxing,nxt", [
    ("2026-10-24T14:30", "waxing gibbous", 98, True, ["2026-10-25", "2026-11-01", "2026-11-09", "2026-11-17"]),
    ("2027-01-19T15:30", "waxing gibbous", 90, True, ["2027-01-22", "2027-01-29", "2027-02-06", "2027-02-14"]),
    ("2026-10-25T09:00", "full moon", 100, True, ["2026-10-25", "2026-11-01", "2026-11-09", "2026-11-17"]),
    ("2026-10-25T23:30", "full moon", 100, False, ["2026-11-01", "2026-11-09", "2026-11-17", "2026-11-24"]),
    ("2026-10-26T00:33", "waning gibbous", 100, False, ["2026-11-01", "2026-11-09", "2026-11-17", "2026-11-24"]),
    ("2026-11-01T05:38", "last quarter", 54, False, ["2026-11-01", "2026-11-09", "2026-11-17", "2026-11-24"]),
    ("2026-11-05T08:49", "waning crescent", 14, False, ["2026-11-09", "2026-11-17", "2026-11-24", "2026-12-01"]),
    ("2026-11-09T11:52", "new moon", 0, True, ["2026-11-17", "2026-11-24", "2026-12-01", "2026-12-08"]),
    ("2026-11-12T14:25", "waxing crescent", 12, True, ["2026-11-17", "2026-11-24", "2026-12-01", "2026-12-08"]),
])
def test_moon_info(now, name, pct, waxing, nxt):
    m = astro.moon_info(LAT, LON, at(now), TZ)
    assert m.phase_name == name
    assert abs(m.illumination - pct) <= 1
    assert m.waxing is waxing
    assert [p.date.isoformat() for p in m.next] == nxt
    order = [astro.PHASE_KINDS.index(p.kind) for p in m.next]  # new → first → full → last → new …
    assert all((b - a) % 4 == 1 for a, b in zip(order, order[1:], strict=False))


# Every moonrise/moonset on these local days, from timeanddate (includes the days astral 3.2's moonrise()/moonset()
# get wrong: Oct 12 set, Oct 28 rise, Jan 9 set, Jan 23 rise).
EVENTS = {
    "2026-10-04": [("set", "15:26")],
    "2026-10-12": [("rise", "09:12"), ("set", "19:00")],
    "2026-10-18": [("rise", "14:27")],
    "2026-10-19": [("set", "00:00"), ("rise", "14:58")],
    "2026-10-24": [("set", "05:27"), ("rise", "16:57")],
    "2026-10-28": [("set", "10:29"), ("rise", "19:31")],
    "2026-11-01": [("set", "13:03"), ("rise", "23:15")],  # DST ends at 2 AM
    "2026-11-12": [("rise", "10:01"), ("set", "18:49")],
    "2027-01-09": [("rise", "08:29"), ("set", "18:36")],
    "2027-01-23": [("set", "07:58"), ("rise", "18:46")],
    "2027-01-29": [("rise", "00:38"), ("set", "10:36")],
}


@pytest.mark.parametrize("day,expected", list(EVENTS.items()))
def test_moon_events(day, expected):
    got = astro.moon_events(LAT, LON, date.fromisoformat(day), TZ)
    assert [k for k, _ in got] == [k for k, _ in expected]
    for (_, t), (_, ref) in zip(got, expected, strict=True):
        assert close(t, at(f"{day}T{ref}"), 1.5)


def test_days_without_a_moonrise_or_moonset():
    m = astro.moon_info(LAT, LON, at("2026-10-04T12:00"), TZ)  # no moonrise on Oct 4: show the day's set
    assert m.rise is None and close(m.set, at("2026-10-04T15:26"), 2)
    m = astro.moon_info(LAT, LON, at("2026-10-18T12:00"), TZ)  # rises 2:27 PM, next set 12:00 AM on the 19th
    assert close(m.rise, at("2026-10-18T14:27"), 2) and close(m.set, at("2026-10-19T00:00"), 2)


def test_dst_change_day():
    s = astro.sun_info(LAT, LON, date(2026, 11, 1), TZ)  # clocks fall back: sunrise is in CST
    assert s.sunrise.utcoffset() == timedelta(hours=-6)
    assert 0 < s.delta_min_per_day + 3 < 3  # about −2 min/day, not an hour


async def test_provider_output_matches_shapes():
    p = base.build("astro", ProviderCfg(type="astro", ttl="1h"), APP)
    now = at("2026-10-24T14:30")
    out = await p.fetch(RequestContext(now=now))
    assert set(out) == {"astro.sun", "astro.moon"}
    for key, r in out.items():
        assert r.status is Status.ok and r.source == "astro" and r.observed_at == now
        shapes.validate(key, r.value)
    assert out["astro.sun"].value["sunrise"] == "2026-10-24T07:14:00-05:00"
    assert out["astro.moon"].value["next"][0] == {"kind": "full", "date": "2026-10-25"}


async def test_provider_never_raises(monkeypatch):
    def boom(*a, **k):
        raise ValueError("Sun never rises")

    monkeypatch.setattr(astro, "sun_info", boom)
    p = base.build("astro", ProviderCfg(type="astro"), APP)
    out = await p.fetch(RequestContext(now=at("2026-10-24T14:30")))
    assert out["astro.sun"].status is Status.missing and out["astro.moon"].status is Status.ok
