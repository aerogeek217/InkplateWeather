"""Generated text: the header day line, the radar callout and the "Next:" hazard chip (SPEC: Header, Radar).

Everything here is template-based and driven by the structured forecast (shapes.HourlyPoint / DailyForecast /
Alert as plain dicts). Text that a fixture provides (`summary.day_line`, `radar.callout`, `summary.next_hazard`)
wins over generated text; the view model decides that. The day line is fitted to the room the header leaves
(measured with app.textwidth), choosing the most complete phrasing that fits.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .textwidth import measure

# ------------------------------------------------------------------ thresholds

PRECIP_POP = 30  # an hour "has" precipitation from this chance
LIKELY_POP = 60  # "Rain around 4 PM" rather than "Chance of rain around 4 PM"
DAY_LINE_HOURS = 18
CALLOUT_HOURS = 12
NEXT_HOURS = 72
CALLOUT_THUNDER_POP = 30
CALLOUT_SNOW_IN = 0.5
CALLOUT_RAIN_POP = 60
CALLOUT_RAIN_IN = 0.25
CALLOUT_ICE_IN = 0.05
CALLOUT_ICE_POP = 50
CALLOUT_DETAIL_PARTS = 3  # "Sleet now · heaviest 7–11 PM · 3–5 in": at most three pieces
TREND_DEG = 8  # tomorrow's high this much above/below today's → "warmer" / "colder"
WINDY_MPH = 20  # sustained
WINDY_GUST_MPH = 30
FROST_F, FREEZE_F = 34, 32
FROST_MONTHS = {3, 4, 5, 9, 10, 11}  # frost/freeze chips only in the shoulder seasons
WIND_CHILL_F = 0
HEAT_INDEX_F = 100
GUST_HAZARD_MPH = 40
HEAVY_RAIN_IN = 1.0
SNOW_HAZARD_IN = 1.0
EVENING_HOUR = 15  # from 3 PM the day line talks about tonight and tomorrow

# header geometry (header.j2): left group = icon 34 (−4 margin) + 14 + city + 14 + date;
# right group = [day line] 22 divider(1) 22 battery(30 + 6 + "78%") 22 time
HEADER_WIDTH = 1200 - 2 * 68
HEADER_MIN_GAP = 24  # keep at least this much white between the left and right groups
DAY_LINE_SIZE, DAY_LINE_WEIGHT = 17, 600
ALERT_TAG_SIZE = 13  # header.j2 alert tag: 13 px / 800, 0.08em, uppercase

STORM_ICONS = {"thunderstorm", "hail"}
KIND_WORD = {"storm": "storms", "rain": "rain", "snow": "snow", "ice": "sleet"}
ICE_WORDS = {"freezing-rain": "freezing rain", "sleet": "sleet", "wintry-mix": "wintry mix"}
SKY_TONIGHT = {"clear-night": "Clear", "sunny": "Clear", "partly-cloudy-night": "Partly cloudy",
               "partly-cloudy": "Partly cloudy", "cloudy": "Cloudy", "fog": "Foggy", "haze-smoke": "Hazy"}


# ------------------------------------------------------------------ inputs


@dataclass
class Hour:
    t: datetime
    temp: float | None
    pop: float
    kind: str  # rain | snow | ice
    rain: float = 0.0
    snow: float = 0.0
    ice: float = 0.0
    wind: float | None = None
    gust: float | None = None
    icon: str | None = None
    humidity: float | None = None


def _num(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _dt(v: Any, tz: ZoneInfo) -> datetime | None:
    if v is None:
        return None
    try:
        d = v if isinstance(v, datetime) else datetime.fromisoformat(str(v))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=tz)
    return d.astimezone(tz)


def _date(v: Any) -> date | None:
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def parse_hours(raw: Any, tz: ZoneInfo) -> list[Hour]:
    out = []
    for h in raw or []:
        if not isinstance(h, dict):
            continue
        t = _dt(h.get("t"), tz)
        if t is None:
            continue
        kind = h.get("precip_type") or "rain"
        out.append(Hour(
            t=t, temp=_num(h.get("temp")), pop=_num(h.get("pop")) or 0.0,
            kind=kind if kind in ("rain", "snow", "ice") else "rain",
            rain=_num(h.get("rain_in")) or 0.0, snow=_num(h.get("snow_in")) or 0.0, ice=_num(h.get("ice_in")) or 0.0,
            wind=_num(h.get("wind_speed")), gust=_num(h.get("wind_gust")), icon=h.get("icon"),
            humidity=_num(h.get("humidity")),
        ))
    return sorted(out, key=lambda h: h.t)


@dataclass
class Inputs:
    now: datetime  # in the display timezone
    hours: list[Hour]
    daily: list[dict]
    current: str | None = None  # conditions.current (icon name)
    alerts: list[dict] = field(default_factory=list)
    temp_now: float | None = None

    @classmethod
    def build(cls, now: datetime, tz: ZoneInfo, hourly: Any, daily: Any, current: Any = None,
              alerts: Any = None, temp_now: float | None = None) -> Inputs:
        cur = current.get("name") if isinstance(current, dict) else current
        cur = str(cur).split("/")[-1] if cur else None
        return cls(now=now.astimezone(tz), hours=parse_hours(hourly, tz),
                   daily=[d for d in (daily or []) if isinstance(d, dict)], current=cur,
                   alerts=[a for a in (alerts or []) if isinstance(a, dict)], temp_now=temp_now)

    def day(self, offset: int) -> dict:
        target = self.now.date() + timedelta(days=offset)
        for d in self.daily:
            if _date(d.get("date")) == target:
                return d
        return {}

    def window(self, hours: float) -> list[Hour]:
        """Hours from the current one (the hour containing now) up to now + `hours`."""
        start = self.now - timedelta(hours=1)
        end = self.now + timedelta(hours=hours)
        return [h for h in self.hours if start < h.t < end]


# ------------------------------------------------------------------ formatting


def round_half_up(v: float) -> int:
    return int(math.floor(v + 0.5))


def deg(v: float) -> str:
    n = round_half_up(v)
    return f"−{abs(n)}°" if n < 0 else f"{n}°"


def clock_hour(t: datetime) -> str:
    """'4 PM', 'noon', 'midnight'."""
    if t.hour == 0:
        return "midnight"
    if t.hour == 12:
        return "noon"
    return f"{t.hour % 12} {'AM' if t.hour < 12 else 'PM'}"


def hour_range(a: datetime, b: datetime) -> str:
    """'7–11 PM', '10 PM–2 AM', '6 PM–midnight' (b is the end of the last hour)."""
    ha, hb = a.hour % 12 or 12, b.hour % 12 or 12
    ma, mb = ("AM" if a.hour < 12 else "PM"), ("AM" if b.hour < 12 else "PM")
    if b.hour == 0:
        return f"{ha} {ma}–midnight"
    return f"{ha}–{hb} {mb}" if ma == mb else f"{ha} {ma}–{hb} {mb}"


def snow_range(total: float) -> str:
    """'0.4 in', '1–2 in', '3–5 in', '6–10 in' (expected snowfall as a range around the forecast total, like the
    reference boards and NWS wording)."""
    if total < 1:
        return f"{total:.1f} in"
    n = round_half_up(total)
    if n <= 2:
        return "1–2 in"
    spread = 1 if n <= 6 else 2  # NWS-style two-inch ranges, four inches for big storms
    return f"{n - spread}–{n + spread} in"


def rain_amount(total: float) -> str:
    return f"~{total:.1f} in"


def part_of_day(t: datetime) -> str:
    if 5 <= t.hour < 12:
        return "morning"
    if 12 <= t.hour < 17:
        return "afternoon"
    if 17 <= t.hour < 21:
        return "evening"
    return "night"


def when(t: datetime, now: datetime) -> str:
    """'this afternoon', 'tonight', 'Sunday night', 'Thursday morning' (night runs 9 PM–5 AM)."""
    part = part_of_day(t)
    d = t.date() - timedelta(days=1) if part == "night" and t.hour < 5 else t.date()
    if d == now.date():
        return "tonight" if part == "night" else f"this {part}"
    return f"{d:%A} {part}"


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def _lower(s: str) -> str:
    return s[:1].lower() + s[1:] if s[:2] != s[:2].upper() else s


# ------------------------------------------------------------------ precipitation events


@dataclass
class Event:
    hours: list[Hour]  # the run of wet hours (gaps of ≤ 2 dry hours allowed)
    kind: str  # storm | rain | snow | ice (dominant)
    first_kind: str  # kind of the first wet hour (for "sleet changing to snow")
    start: datetime  # first wet hour
    key_time: datetime  # when it becomes likely (≥ 60 %), else the peak hour
    end: datetime  # end of the last wet hour
    peak_pop: float
    ongoing: bool
    snow: float
    ice: float
    rain: float
    change_time: datetime | None = None  # first hour of the dominant kind when it differs from the first

    @property
    def likely(self) -> bool:
        return self.peak_pop >= LIKELY_POP


def _is_storm(h: Hour, inp: Inputs) -> bool:
    if h.icon:
        return h.icon in STORM_ICONS
    d = inp.day((h.t.date() - inp.now.date()).days)
    return str(d.get("icon") or "").split("/")[-1] in STORM_ICONS


def find_event(inp: Inputs, hours: float) -> Event | None:
    win = inp.window(hours)
    wet = [i for i, h in enumerate(win) if h.pop >= PRECIP_POP]
    if not wet:
        return None
    first = wet[0]
    last = first
    for i in wet[1:]:
        if i - last > 3:  # more than 2 dry hours in between: a separate event
            break
        last = i
    # follow the run past the window end (amounts and end time cover the whole event)
    idx = inp.hours.index(win[last])
    while idx + 1 < len(inp.hours) and inp.hours[idx + 1].pop >= PRECIP_POP:
        idx += 1
    run = inp.hours[inp.hours.index(win[first]):idx + 1]
    run_wet = [h for h in run if h.pop >= PRECIP_POP]
    weights: dict[str, float] = {}
    for h in run_wet:
        k = "storm" if h.kind == "rain" and _is_storm(h, inp) else h.kind
        weights[k] = weights.get(k, 0) + h.pop
    order = ["storm", "snow", "ice", "rain"]  # ties go to the more significant kind
    kind = max(weights, key=lambda k: (weights[k], -order.index(k)))
    if kind == "rain" and any(_is_storm(h, inp) for h in run_wet) and weights.get("storm", 0) > 0:
        kind = "storm"

    def kind_of(h: Hour) -> str:
        return "storm" if h.kind == "rain" and _is_storm(h, inp) else h.kind

    first_kind = kind_of(run_wet[0])
    change = None
    if first_kind != kind and {first_kind, kind} != {"storm", "rain"}:
        change = next((h.t for h in run_wet if kind_of(h) == kind), None)
    peak = max(h.pop for h in run_wet)
    likely = next((h.t for h in run_wet if h.pop >= LIKELY_POP), None)
    key_time = likely or next(h.t for h in run_wet if h.pop == peak)
    current_wet = inp.current is not None and _precip_condition(inp.current)
    ongoing = run_wet[0].t <= inp.now or (current_wet and run_wet[0].t <= inp.now + timedelta(hours=1))
    return Event(
        hours=run, kind=kind, first_kind=first_kind, start=run_wet[0].t, key_time=key_time,
        end=run_wet[-1].t + timedelta(hours=1), peak_pop=peak, ongoing=ongoing,
        snow=sum(h.snow for h in run), ice=sum(h.ice for h in run), rain=sum(h.rain for h in run),
        change_time=change,
    )


def _precip_condition(name: str) -> bool:
    return name in {"drizzle", "rain", "heavy-rain", "thunderstorm", "hail", "flurries", "snow", "heavy-snow",
                    "blowing-snow", "sleet", "freezing-rain", "wintry-mix"}


def _kind_word(kind: str, inp: Inputs, h: Hour | None = None) -> str:
    """'storms', 'rain', 'snow', 'sleet' / 'freezing rain' / 'wintry mix'."""
    if kind == "ice":
        icon = (h.icon if h is not None else None) or (inp.current if inp.current in ICE_WORDS else None)
        return ICE_WORDS.get(icon or "", "sleet")
    return KIND_WORD[kind]


def _event_lead(ev: Event, inp: Inputs) -> str:
    """'Storms around 4 PM', 'Sleet changing to snow by 6 PM', 'Rain until 9 PM', 'Chance of rain around 2 AM'."""
    first_word = _kind_word(ev.first_kind, inp, ev.hours[0])
    word = _kind_word(ev.kind, inp)
    frozen = ev.kind in ("snow", "ice")
    if ev.change_time is not None:
        to = f"changing to {word} by {clock_hour(ev.change_time)}"
        if ev.ongoing:
            return f"{_cap(first_word)} {to}"
        return f"{_cap(first_word)} around {clock_hour(ev.start)}, {to}"
    if ev.ongoing:
        if ev.end - inp.now <= timedelta(hours=24):
            return f"{_cap(word)} until {clock_hour(ev.end)}"
        return f"{_cap(word)} through {ev.end:%A}"
    t = ev.key_time
    at = f"by {clock_hour(t)}" if frozen else f"around {clock_hour(t)}"
    if t.date() != inp.now.date() and t.hour >= 6:
        at = f"tomorrow {at}"
    if not ev.likely:
        return f"Chance of {word} {at}"
    return f"{_cap(word)} {at}"


def _event_amount(ev: Event, now: datetime) -> str | None:
    by_morning = ev.end.date() > now.date() and ev.end.hour <= 12
    if ev.snow >= CALLOUT_SNOW_IN:
        return snow_range(ev.snow) + (" by morning" if by_morning else "")
    if ev.ice >= CALLOUT_ICE_IN:
        return f"ice {ev.ice:.2f} in"
    if ev.rain >= 0.5:
        return rain_amount(ev.rain)
    return None


# ------------------------------------------------------------------ trend words


def _windy(inp: Inputs, after: datetime, until: datetime, day_offsets: tuple[int, ...]) -> bool:
    hrs = [h for h in inp.hours if after <= h.t < until]
    if any(h.wind is not None for h in hrs):
        return any((h.wind or 0) >= WINDY_MPH or (h.gust or 0) >= WINDY_GUST_MPH for h in hrs)
    for off in day_offsets:
        d = inp.day(off)
        if (_num(d.get("wind_speed")) or 0) >= WINDY_MPH or (_num(d.get("wind_gust")) or 0) >= WINDY_GUST_MPH:
            return True
    return False


def _temp_trend(inp: Inputs) -> str | None:
    """'colder' / 'warmer' comparing tomorrow's high with today's (none once today's high is gone, in the
    evening: tonight's temperatures would make every tomorrow look warmer)."""
    tomorrow = _num(inp.day(1).get("high"))
    today = _num(inp.day(0).get("high"))
    if tomorrow is None or today is None:
        return None
    diff = tomorrow - today
    if diff <= -TREND_DEG:
        return "much colder" if diff <= -2 * TREND_DEG else "colder"
    if diff >= TREND_DEG:
        return "much warmer" if diff >= 2 * TREND_DEG else "warmer"
    return None


def _and(words: list[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


# ------------------------------------------------------------------ day line


def day_line_candidates(inp: Inputs) -> list[str]:
    """Phrasings from most to least complete (each a full sentence)."""
    ev = find_event(inp, DAY_LINE_HOURS)
    if ev is not None:
        lead = _event_lead(ev, inp)
        amount = _event_amount(ev, inp.now)
        after = max(ev.end, inp.now)
        words = [w for w in (_temp_trend(inp),) if w]
        if _windy(inp, after, inp.now + timedelta(hours=30), (1,) if ev.end.date() > inp.now.date() else (0, 1)):
            words.append("windy")
        then = f"then {_and(words)}" if words else None
        out = []
        if amount and then:
            out.append(f"{lead}; {amount}, {then}.")
        if amount:
            out.append(f"{lead}; {amount}.")
        if then:
            out.append(f"{lead}, {then}.")
        out.append(f"{lead}.")
        return out
    return _dry_candidates(inp)


def _dry_candidates(inp: Inputs) -> list[str]:
    today, tomorrow = inp.day(0), inp.day(1)
    hi, lo = _num(today.get("high")), _num(today.get("low"))
    evening = inp.now.hour >= EVENING_HOUR or hi is None
    lead = None
    today_text = _sentence_text(today.get("text"))
    if not evening and hi is not None:
        text = today_text
        windy = _windy(inp, inp.now, inp.now.replace(hour=20, minute=0), (0,))
        if windy and "wind" not in text.lower() and "breez" not in text.lower():
            text = f"{text} and windy" if text else "Windy"
        lead = f"{_cap(text)}, high {deg(hi)}" if text else f"High {deg(hi)} today"
    else:
        night = [h for h in inp.hours
                 if inp.now <= h.t <= inp.now.replace(hour=6, minute=0) + timedelta(days=1)
                 and (h.t.hour >= 20 or h.t.hour < 6)]
        if lo is None:
            lo = min((h.temp for h in night if h.temp is not None), default=None)
        icons = [h.icon for h in night if h.icon]
        sky = SKY_TONIGHT.get(max(set(icons), key=icons.count)) if icons else None
        if lo is not None:
            lead = f"{sky} tonight, low {deg(lo)}" if sky else f"Low {deg(lo)} tonight"
    t_hi = _num(tomorrow.get("high"))
    tail = None
    if t_hi is not None:
        words = [w for w in (_temp_trend(inp),) if w]
        t0 = datetime.combine(inp.now.date() + timedelta(days=1), datetime.min.time(), tzinfo=inp.now.tzinfo)
        if _windy(inp, t0 + timedelta(hours=8), t0 + timedelta(hours=20), (1,)):
            words.append("windy")
        t_text = _sentence_text(tomorrow.get("text"))
        if words:
            tail = f"{_and(words)} tomorrow, {deg(t_hi)}"
        elif t_text and not evening and t_text.lower() == today_text.lower():
            tail = f"again tomorrow, {deg(t_hi)}"  # "Partly sunny, high 75°; again tomorrow, 77°."
        elif t_text:
            tail = f"{_lower(t_text)} tomorrow, {deg(t_hi)}"
        else:
            tail = f"tomorrow {deg(t_hi)}"
    out = []
    if lead and tail:
        out.append(f"{lead}; {tail}.")
    if lead:
        out.append(f"{lead}.")
    elif tail:
        out.append(f"{_cap(tail)}.")
    return out


def _sentence_text(text: Any) -> str:
    """The 7-day list's short text, made to read in a sentence ("Snow → blowing snow" → "Snow then blowing snow")."""
    return str(text or "").strip().replace(" → ", " then ")


def _detail_candidates(inp: Inputs) -> list[str]:
    """NWS detailed forecast as a fallback: as many whole sentences as fit, then the short text."""
    d = inp.day(0) if inp.now.hour < 18 or not inp.day(1) else inp.day(1)
    out = []
    detail = str(d.get("detail") or "").strip()
    if detail:
        sentences = [s.strip() + "." for s in detail.replace("!", ".").split(".") if s.strip()]
        for n in range(len(sentences), 0, -1):
            out.append(" ".join(sentences[:n]))
    if d.get("text"):
        out.append(f"{_cap(str(d['text']).strip())}.")
    return out


def fits(text: str, budget: float) -> bool:
    return measure(text, DAY_LINE_SIZE, DAY_LINE_WEIGHT) <= budget


def day_line(inp: Inputs, budget: float) -> str | None:
    """The most complete generated phrasing that fits `budget` px; NWS wording as the fallback."""
    for cands in (day_line_candidates(inp) if inp.hours else [], _detail_candidates(inp)):
        for c in cands:
            if fits(c, budget):
                return c
    return None


def header_budget(city: str, date_text: str, battery_pct: int | None, time_text: str, gap: float = 22) -> float:
    """Width left for the day line (header.j2: right-group gap 22, or 18 with an alert)."""
    left = 34 - 4 + 14 + measure(city, 22, 800, -0.01) + 14 + measure(date_text, 22, 300)
    right = gap + 1 + gap + measure(time_text, 22, 700)  # [text] gap divider gap … time
    if battery_pct is not None:
        right += 30 + 6 + measure(f"{battery_pct}%", 16, 700) + gap
    return HEADER_WIDTH - left - right - HEADER_MIN_GAP


def alert_width(event: str, detail: str) -> float:
    """Width of the header's alert group (tag + detail), header.j2."""
    tag = 8 + 12 + 6 + measure(event, ALERT_TAG_SIZE, 800, 0.08, uppercase=True) + 8
    return tag + (10 + measure(detail, 15, 600) if detail else 0)


# ------------------------------------------------------------------ radar callout


SEVERITY_RANK = {"Extreme": 4, "Severe": 3, "Moderate": 2, "Minor": 1}


def most_severe(alerts: list[dict]) -> dict | None:
    """Most severe alert (stable: the source's order breaks ties)."""
    if not alerts:
        return None
    return max(enumerate(alerts), key=lambda ia: (SEVERITY_RANK.get(str(ia[1].get("severity")), 0), -ia[0]))[1]


def callout(inp: Inputs) -> dict | None:
    """{kind: storm|snow|rain|ice|alert, headline, detail} for a notable event in the next 12 h, else None."""
    ev = find_event(inp, CALLOUT_HOURS)
    if ev is not None and _notable(ev):
        return {"kind": "snow" if ev.kind == "snow" else ev.kind, "headline": _headline(ev, inp),
                "detail": _callout_detail(ev, inp)}
    alert = most_severe(inp.alerts)
    if alert is not None:
        event = str(alert.get("event") or "Weather alert")
        kind = "storm" if any(w in event for w in ("Thunderstorm", "Tornado")) else \
            "snow" if any(w in event for w in ("Winter", "Blizzard", "Snow", "Ice")) else "alert"
        parts = []
        ends = _dt(alert.get("ends"), inp.now.tzinfo)  # type: ignore[arg-type]
        if ends:
            parts.append(f"until {clock_hour(ends)}" + ("" if ends.date() == inp.now.date() else f" {ends:%a}"))
        if alert.get("impact"):
            parts.append(str(alert["impact"]))
        return {"kind": kind, "headline": event, "detail": " · ".join(parts)}
    return None


def _notable(ev: Event) -> bool:
    if ev.kind == "storm":
        return ev.peak_pop >= CALLOUT_THUNDER_POP
    if ev.kind == "snow":
        return ev.snow >= CALLOUT_SNOW_IN
    if ev.kind == "ice":
        return ev.ice >= CALLOUT_ICE_IN or ev.peak_pop >= CALLOUT_ICE_POP
    return ev.peak_pop >= CALLOUT_RAIN_POP or ev.rain >= CALLOUT_RAIN_IN


def _headline(ev: Event, inp: Inputs) -> str:
    word = _cap(_kind_word(ev.kind, inp))
    if ev.change_time is not None:
        return f"{word} by {clock_hour(ev.change_time)}"
    if ev.ongoing:
        return f"{word} now"
    if ev.kind in ("snow", "ice"):
        return f"{word} by {clock_hour(ev.key_time)}"
    return f"{word} ~{clock_hour(ev.key_time)}"


def _callout_detail(ev: Event, inp: Inputs) -> str:
    parts = []
    if ev.change_time is not None and ev.ongoing:
        parts.append(f"{_cap(_kind_word(ev.first_kind, inp, ev.hours[0]))} now")
    if ev.kind == "snow" and ev.snow > 0:
        peak = max(h.snow for h in ev.hours)
        heavy = [h for h in ev.hours if h.snow >= 0.6 * peak]
        if len(heavy) < len([h for h in ev.hours if h.snow > 0]):
            parts.append(f"heaviest {hour_range(heavy[0].t, heavy[-1].t + timedelta(hours=1))}")
        parts.append(snow_range(ev.snow))
    else:
        if not ev.ongoing:
            parts.append(hour_range(ev.start, ev.end))
        else:
            parts.append(f"until {clock_hour(ev.end)}")
        parts.append(f"{round_half_up(ev.peak_pop)}%")
        if ev.ice >= CALLOUT_ICE_IN:
            parts.append(f"ice {ev.ice:.2f} in")
        elif ev.rain >= 0.1:
            parts.append(rain_amount(ev.rain))
    gust = max((h.gust or 0 for h in ev.hours), default=0)
    if gust >= WINDY_GUST_MPH and len(parts) < CALLOUT_DETAIL_PARTS:
        parts.append(f"gusts {round_half_up(gust)} mph")
    return " · ".join(parts[:CALLOUT_DETAIL_PARTS])


# ------------------------------------------------------------------ next hazard chip


def wind_chill(t: float, v: float) -> float | None:
    """NWS wind chill (°F, mph); None outside its range (T > 50 °F or wind < 3 mph)."""
    if t > 50 or v < 3:
        return None
    p = v ** 0.16
    return 35.74 + 0.6215 * t - 35.75 * p + 0.4275 * t * p


def heat_index(t: float, rh: float | None) -> float:
    """NWS heat index (Rothfusz); without humidity the air temperature stands in."""
    if rh is None or t < 80:
        return t
    return (-42.379 + 2.04901523 * t + 10.14333127 * rh - 0.22475541 * t * rh - 6.83783e-3 * t * t
            - 5.481717e-2 * rh * rh + 1.22874e-3 * t * t * rh + 8.5282e-4 * t * rh * rh - 1.99e-6 * t * t * rh * rh)


@dataclass
class Hazard:
    at: datetime
    kind: str  # frost | wind_chill | heat | gust | rain | snow
    text: str


def hazards(inp: Inputs) -> list[Hazard]:
    """Notable hazards in the next ~3 days, earliest first."""
    out: list[Hazard] = []
    now, end = inp.now, inp.now + timedelta(hours=NEXT_HOURS)
    tz = now.tzinfo
    # frost / freeze: each night's low (the daily low follows that day)
    if now.month in FROST_MONTHS:
        for off in range(0, 3):
            low = _num(inp.day(off).get("low"))
            if low is None or low > FROST_F:
                continue
            d = now.date() + timedelta(days=off)
            at = datetime.combine(d + timedelta(days=1), datetime.min.time(), tzinfo=tz) + timedelta(hours=5)
            name = "tonight" if off == 0 else f"{d:%A} night"
            text = f"freeze likely {name} ({deg(low)})" if low <= FREEZE_F else f"frost possible {name} ({deg(low)})"
            out.append(Hazard(at, "frost", text))
            break
    hrs = [h for h in inp.hours if now < h.t <= end]

    def day_wind(h: Hour) -> float | None:
        return h.wind if h.wind is not None else _num(inp.day((h.t.date() - now.date()).days).get("wind_speed"))

    # wind chill: lowest value in the first day part that reaches the threshold
    chills = [(h, wind_chill(h.temp, day_wind(h) or 0)) for h in hrs if h.temp is not None]
    first = next(((h, c) for h, c in chills if c is not None and round_half_up(c) <= WIND_CHILL_F), None)
    if first:
        label = when(first[0].t, now)
        low = min(c for h, c in chills if c is not None and when(h.t, now) == label)
        out.append(Hazard(first[0].t, "wind_chill", f"wind chill {deg(low)} {label}"))
    # heat index
    hot = [(h, heat_index(h.temp, h.humidity)) for h in hrs if h.temp is not None]
    first_hot = next(((h, v) for h, v in hot if v >= HEAT_INDEX_F), None)
    if first_hot:
        label = when(first_hot[0].t, now)
        peak = max(v for h, v in hot if when(h.t, now) == label)
        out.append(Hazard(first_hot[0].t, "heat", f"heat index {deg(peak)} {label}"))
    # gusts
    gusty = next((h for h in hrs if (h.gust or 0) >= GUST_HAZARD_MPH), None)
    if gusty:
        label = when(gusty.t, now)
        peak = max(h.gust or 0 for h in hrs if when(h.t, now) == label)
        out.append(Hazard(gusty.t, "gust", f"gusts to {round_half_up(peak)} mph {label}"))
    else:
        for off in range(0, 4):
            d = inp.day(off)
            g = _num(d.get("wind_gust"))
            if g is not None and g >= GUST_HAZARD_MPH:
                day = now.date() + timedelta(days=off)
                at = datetime.combine(day, datetime.min.time(), tzinfo=tz) + timedelta(hours=12)
                name = "today" if off == 0 else f"{day:%A}"
                out.append(Hazard(max(at, now), "gust", f"gusts to {round_half_up(g)} mph {name}"))
                break
    # heavy rain / snow per calendar day
    for off in range(0, 4):
        day = now.date() + timedelta(days=off)
        day_hrs = [h for h in hrs if h.t.date() == day]
        d = inp.day(off)
        rain = sum(h.rain for h in day_hrs)
        snow = sum(h.snow for h in day_hrs)
        if off > 0 or not day_hrs:  # today's daily total includes snow that has already fallen
            snow = max(snow, _num(d.get("snow_in")) or 0)
        name = "today" if off == 0 else f"{day:%A}"
        at = min((h.t for h in day_hrs if h.pop >= PRECIP_POP), default=None) or \
            datetime.combine(day, datetime.min.time(), tzinfo=tz) + timedelta(hours=12)
        if at > end:
            continue
        if snow >= SNOW_HAZARD_IN:
            out.append(Hazard(max(at, now), "snow", f"snow {name} ({snow_range(snow)})"))
        elif rain >= HEAVY_RAIN_IN:
            out.append(Hazard(max(at, now), "rain", f"heavy rain {name} ({rain_amount(rain)})"))
    return sorted(out, key=lambda z: z.at)


def next_hazard(inp: Inputs, callout_event: Event | None = None) -> str | None:
    """The next notable hazard that the callout doesn't already cover (text without the 'Next: ' prefix)."""
    covered_until = None
    if callout_event is not None:
        covered_until = max(callout_event.end, inp.now + timedelta(hours=CALLOUT_HOURS))
    for z in hazards(inp):
        if covered_until is not None and z.kind in ("rain", "snow", "gust") and z.at <= covered_until:
            continue  # the callout's own storm
        return z.text
    return None


def callout_event(inp: Inputs) -> Event | None:
    """The event the generated callout describes (for next_hazard's de-duplication)."""
    ev = find_event(inp, CALLOUT_HOURS)
    return ev if ev is not None and _notable(ev) else None
