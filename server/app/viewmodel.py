"""Resolved readings → DashboardContext: all formatting, icon choice and chart geometry.

Phase-1 rules (enough to reproduce the reference boards from the fixtures); phase 2.5 refines them.
Geometry constants are those of design/source/dashboard.html (reflowed layout, 2026-10-24).
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from . import icons, messages, summary
from .config import AppConfig
from .context import (
    Alert,
    Callout,
    Current,
    DashboardContext,
    ForecastRow,
    Gauge,
    GaugeCell,
    Header,
    Icon,
    Moon,
    MoonPhase,
    PrecipBar,
    PrecipChart,
    Radar,
    ReadingCell,
    Slot,
    Sun,
    TrendChart,
    Wind,
)
from .providers.base import DeviceInfo
from .readings import Status
from .resolver import Resolved
from .textwidth import measure

MINUS = "−"
BOLD_WIND_MPH = 20
BATTERY_LOW_PCT = 15  # header note
BATTERY_STRIP_PCT = 5  # reminder strip
MIN_SECTIONS_FOR_NOTICE = 2  # the day line becomes "Some data is unavailable: …"
MAX_NOTICES = 6  # sentences in the notes box

# ------------------------------------------------------------------ small formatting helpers


def _round(v: float) -> int:
    """Half-up rounding (Python's round() is banker's)."""
    return int(math.floor(v + 0.5))


def _signed(n: int | float, fmt: str = "{}") -> str:
    s = fmt.format(abs(n))
    return f"{MINUS}{s}" if n < 0 else s


def deg(v: float | None) -> str:
    return "" if v is None else _signed(_round(v)) + "°"


def deg1(v: float | None) -> str:
    return "" if v is None else _signed(v, "{:.1f}") + "°"


def pct(v: float | None) -> str:
    return "" if v is None else f"{_round(v)}%"


def clock(t: datetime, tz: str) -> str:
    return messages.clock(t, tz)


def hour_label(t: datetime) -> str:
    """'3 PM', '12 AM' (local time already)."""
    h = t.hour % 12 or 12
    return f"{h} {'AM' if t.hour < 12 else 'PM'}"


def short_clock(t: datetime) -> str:
    """'9 AM' on the hour, else '9:30 AM'."""
    h = t.hour % 12 or 12
    ampm = "AM" if t.hour < 12 else "PM"
    return f"{h} {ampm}" if t.minute == 0 else f"{h}:{t.minute:02d} {ampm}"


def aqi_category(aqi: float) -> str:
    for limit, name in ((50, "Good"), (100, "Moderate"), (150, "Sensitive"), (200, "Unhealthy"),
                        (300, "Very unhealthy")):
        if aqi <= limit:
            return name
    return "Hazardous"


def uv_category(uv: float) -> str:
    u = _round(uv)
    if u <= 2:
        return "Low"
    if u <= 5:
        return "Moderate"
    if u <= 7:
        return "High"
    if u <= 10:
        return "Very high"
    return "Extreme"


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
    if v is None:
        return None
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def _r2(v: float) -> float:
    return round(v, 2)


def next_refresh(now: datetime, minutes: int = 30) -> datetime:
    """Next :00/:30 (for a 30-min refresh) strictly after now."""
    base = now.replace(second=0, microsecond=0)
    m = (base.minute // minutes + 1) * minutes
    return base.replace(minute=0) + timedelta(minutes=m)


# ------------------------------------------------------------------ reading access


class _Data:
    def __init__(self, resolved: Mapping[str, Resolved], tz: str, source_types: dict[str, str]):
        self.resolved = resolved
        self.tz = tz
        self.source_types = source_types

    def get(self, field: str, default: Any = None) -> Any:
        r = self.resolved.get(field)
        if r is None or not r.reading.usable:
            return default
        return r.reading.value

    def records(self, field: str) -> list[dict]:
        """A structured list field (forecast, history, alerts) as its dict entries; anything else → []."""
        v = self.get(field)
        return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []

    def num(self, field: str) -> float | None:
        v = self.get(field)
        try:
            return None if v is None else float(v)
        except (TypeError, ValueError):
            return None

    def slot(self, field: str, text: str, sub: str | None = None, narrow: bool = False) -> Slot:
        """A displayed value with its state. Missing: the full sentence, or for a `narrow` slot (readings and gauge
        cells, ~100 px) "No data" as the message with a short cause ('since 1:12 PM') as `sub`; the full sentence
        then goes to the notes box (`notices`)."""
        r = self.resolved.get(field)
        if r is None or not r.reading.usable or not text:
            if narrow:
                return Slot(status=Status.missing, message=messages.NO_DATA, sub=messages.short(r, self.tz) or None)
            msg = messages.explain(r, self.tz, self.source_types) if r is not None else None
            return Slot(status=Status.missing, message=msg or messages.MISSING.format(label=messages.label_of(field)))
        return Slot(text=text, sub=sub, status=r.reading.status, as_of=messages.as_of(r, self.tz))

    def missing(self, field: str) -> bool:
        """Configured but without a usable value (a field nobody configured doesn't count as missing)."""
        r = self.resolved.get(field)
        return r is not None and not r.reading.usable

    def stale(self, field: str) -> bool:
        r = self.resolved.get(field)
        return r is not None and r.reading.usable and r.reading.status is Status.stale

    def observed(self, field: str) -> datetime | None:
        r = self.resolved.get(field)
        return r.reading.observed_at if r is not None and r.reading.usable else None


# ------------------------------------------------------------------ sections


def _alert(d: _Data, now: datetime, tz: ZoneInfo, budget: float) -> Alert | None:
    """Header takeover for the most severe active alert: 'until 9 AM Wed · 3–5 in snow' ('until 9 PM' today).

    Fitting order (user decision 2026-10-24): full tag + full detail; abbreviated tag (messages.ALERT_ABBREVIATIONS)
    + full detail; abbreviated tag + "until …" only; abbreviated tag alone; never overflow (the tag loses words).
    """
    a = summary.most_severe(d.records("alerts.active"))
    if a is None:
        return None
    event = str(a.get("event") or "Weather alert")
    short_event = messages.abbreviate_event(event)
    until = None
    ends = _dt(a.get("ends"), tz)
    if ends and ends > now:
        until = f"until {short_clock(ends)}" + ("" if ends.date() == now.date() else f" {ends:%a}")
    impact = str(a["impact"]) if a.get("impact") else None
    full = " · ".join(p for p in (until, impact) if p)
    for ev, detail in ((event, full), (short_event, full), (short_event, until or ""), (short_event, "")):
        if summary.alert_width(ev, detail) <= budget:
            return Alert(event=ev, detail=detail)
    words = short_event.split()
    while len(words) > 1 and summary.alert_width(" ".join(words) + "…", "") > budget:
        words.pop()
    return Alert(event=" ".join(words) + "…", detail="")


def _current_code(d: _Data) -> tuple[str, bool]:
    """conditions.current as (icon code, source says windy). Accepts a name or {name, windy}."""
    v = d.get("conditions.current")
    if isinstance(v, dict):
        return str(v.get("name") or ""), bool(v.get("windy"))
    return str(v or ""), False


BATTERY_TEXT_GAP = 10  # header.j2: between "12%" and "Battery low: charge soon"
BATTERY_TEXT_SIZE, BATTERY_TEXT_WEIGHT = 16, 600


def battery_notes(pct: int | None) -> tuple[str | None, str | None]:
    """(header text, reminder strip) for the display's battery level."""
    if pct is None or pct > BATTERY_LOW_PCT:
        return None, None
    strip = messages.BATTERY_STRIP.format(pct=pct) if pct <= BATTERY_STRIP_PCT else None
    return messages.BATTERY_LOW, strip


def _header(d: _Data, now: datetime, device: DeviceInfo, cfg: AppConfig | None, inp: summary.Inputs,
            sections: list[str]) -> Header:
    tz = ZoneInfo(d.tz)
    code, windy = _current_code(d)
    city = cfg.location.name if cfg else ""
    date_text = f"{now:%A} {now.day} {now:%B}"
    batt = device.battery_pct
    time_text = clock(now, d.tz)
    battery_text, battery_strip = battery_notes(batt)
    # The day line (or alert, or notice) yields the room the battery note takes.
    extra = BATTERY_TEXT_GAP + measure(battery_text, BATTERY_TEXT_SIZE, BATTERY_TEXT_WEIGHT) if battery_text else 0
    budget = summary.header_budget(city, date_text, batt, time_text) - extra
    alert = _alert(d, now, tz, summary.header_budget(city, date_text, batt, time_text, gap=18) - extra)
    name = icons.condition(code)
    mods = icons.header_modifiers(name, d.num("wind.gust"), d.num("outdoor.temperature"), bool(alert), windy)
    label = "Now: " + (code.split("/")[-1].replace("-", " ") if code else "unknown")
    if "wind" in mods:
        label += " and windy"
    if "frost" in mods:
        label += ", frost"
    day_line = d.get("summary.day_line")  # fixtures carry their own text; live text is generated to fit
    if not day_line or not summary.fits(str(day_line), budget):
        day_line = summary.day_line(inp, budget)
    notice = None
    if len(sections) >= MIN_SECTIONS_FOR_NOTICE:
        notice = messages.sections_missing(sections, lambda t: summary.fits(t, budget))
    nxt = next_refresh(now, (cfg.server.refresh_minutes if cfg else 30))
    return Header(
        icon=Icon(name=name, modifiers=mods, label=label),
        city=city,
        date_text=date_text,
        day_line=day_line,
        alert=alert,
        next_update=None if alert else f"{nxt.hour % 12 or 12}:{nxt.minute:02d}",
        battery_pct=batt,
        battery_low=batt is not None and batt <= BATTERY_LOW_PCT,
        battery_fill=round(20 * max(0, min(100, batt)) / 100, 1) if batt is not None else 0,
        time_text=time_text,
        notice=notice,
        battery_text=battery_text,
        battery_strip=battery_strip,
    )


def radar_configured(d: _Data) -> bool:
    """A radar source is set up: radar.frame is configured and some source was tried for real. Phase 2 has none
    (the field is absent from the config, or a fixture doesn't carry it); then the panel says radar comes later and
    the header doesn't count it as missing."""
    r = d.resolved.get("radar.frame")
    return r is not None and any(a.note != "no_such_key" for a in r.tried)


def _radar(d: _Data, tz: ZoneInfo, inp: summary.Inputs) -> Radar:
    frame = d.get("radar.frame")
    callout = d.get("radar.callout")  # fixture text wins over generated text
    if not (isinstance(callout, dict) and callout.get("headline")):
        callout = summary.callout(inp)
    hazard = d.get("summary.next_hazard") or summary.next_hazard(inp, summary.callout_event(inp))
    co = None
    if isinstance(callout, dict) and callout.get("headline"):
        co = Callout(kind=callout.get("kind", "rain"), headline=callout["headline"], detail=callout.get("detail", ""))
    if not isinstance(frame, dict):
        r = d.resolved.get("radar.frame")
        msg = (messages.explain(r, d.tz, d.source_types) or messages.MISSING.format(label="Radar")
               if radar_configured(d) else messages.RADAR_LATER)
        return Radar(site_time="", callout=co, next_chip=f"Next: {hazard}" if hazard else None,
                     status=Status.missing, message=msg)
    t = _dt(frame.get("time"), tz)
    types = set(frame.get("types") or [])
    return Radar(
        site_time=" ".join(x for x in (frame.get("site", ""), clock(t, d.tz) if t else "") if x),
        legend="types" if types & {"snow", "ice"} else "intensity",
        lightning_legend=bool(frame.get("lightning")),
        callout=co,
        next_chip=f"Next: {hazard}" if hazard else None,
        svg_layers=frame.get("svg_layers", ""),
        aria_label=frame.get("aria_label", "Radar map"),
    )


def _readings(d: _Data) -> list[ReadingCell]:
    def temp_rh(prefix: str) -> Slot:
        t = d.num(f"{prefix}.temperature")
        rh = d.num(f"{prefix}.humidity")
        return d.slot(f"{prefix}.temperature", deg1(t), pct(rh) if rh is not None else None, narrow=True)

    p = d.num("pressure.sea_level")
    trend = d.num("pressure.trend_3h")
    arrow = ""
    if trend is not None and p is not None:
        arrow = " ↓" if trend <= -0.02 else " ↑" if trend >= 0.02 else " →"
    aqi = d.num("air.aqi")
    uv = d.num("uv.index")
    return [
        ReadingCell(label="Inside", slot=temp_rh("indoor")),
        ReadingCell(label="Garage", slot=temp_rh("garage")),
        ReadingCell(label="Pressure" + arrow,
                    slot=d.slot("pressure.sea_level", f"{p:.2f}" if p is not None else "", narrow=True)),
        ReadingCell(label="Air quality", slot=d.slot("air.aqi", str(_round(aqi)) if aqi is not None else "",
                                                       aqi_category(aqi) if aqi is not None else None, narrow=True)),
        ReadingCell(label="UV index", slot=d.slot("uv.index", str(_round(uv)) if uv is not None else "",
                                                    uv_category(uv) if uv is not None else None, narrow=True)),
    ]


READING_FIELDS = ["indoor.temperature", "garage.temperature", "pressure.sea_level", "air.aqi", "uv.index"]
STATION_NOW_FIELDS = ["outdoor.temperature", "outdoor.feels_like", "outdoor.humidity", "wind.speed"]


CALM_MPH = 1  # below this the wind reads "Calm"
GUST_SHOWN_ABOVE = 5  # "gusts N" only when gusts are at least this much above the speed


def _wind(from_deg: float | None, speed: float | None, gust: float | None = None) -> Wind:
    calm = speed is not None and speed < CALM_MPH
    s = None if speed is None else _round(speed)
    g = None if gust is None or s is None else _round(gust)
    return Wind(
        dir_text="" if calm else icons.wind_dir(from_deg),
        from_deg=None if calm else from_deg,
        speed="" if s is None else str(s),
        gust=str(g) if g is not None and not calm and g >= s + GUST_SHOWN_ABOVE else None,
        bold=s is not None and s >= BOLD_WIND_MPH,
        calm=calm,
    )


def _current(d: _Data, inp: summary.Inputs) -> Current:
    t = d.num("outdoor.temperature")
    today = inp.day(0) or (inp.daily[0] if inp.daily else {})
    feels = d.num("outdoor.feels_like")
    rh = d.num("outdoor.humidity")
    feels_parts = []
    if feels is not None:
        feels_parts.append(f"Feels {deg(feels)}")
    if rh is not None:
        feels_parts.append(f"Humidity {pct(rh)}")
    speed = d.num("wind.speed")
    nh, nl = d.num("normals.high"), d.num("normals.low")
    feels_stale = ((feels is not None and d.stale("outdoor.feels_like"))
                   or (rh is not None and d.stale("outdoor.humidity")))
    wind_stale = speed is not None and d.stale("wind.speed")
    temp_stale = t is not None and d.stale("outdoor.temperature")
    shown = [f for f, s in (("outdoor.temperature", temp_stale), ("outdoor.feels_like", feels_stale),
                            ("outdoor.humidity", feels_stale), ("wind.speed", wind_stale)) if s and d.stale(f)]
    oldest = min((t_ for f in shown if (t_ := d.observed(f)) is not None), default=None)
    return Current(
        high_low_stale=d.stale("forecast.daily"),
        feels_stale=feels_stale,
        wind_stale=wind_stale,
        normals_stale=d.stale("normals.high") or d.stale("normals.low"),
        as_of=messages.AS_OF.format(time=clock(oldest, d.tz)) if oldest else None,
        temp=d.slot("outdoor.temperature", deg(t)),
        high=f"H {deg(today['high'])}" if today.get("high") is not None else None,
        low=f"L {deg(today['low'])}" if today.get("low") is not None else None,
        feels=" · ".join(feels_parts) or None,
        wind=_wind(d.num("wind.direction"), speed, d.num("wind.gust")) if speed is not None else None,
        normals=f"Average {deg(nh)} / {deg(nl)}" if nh is not None and nl is not None else None,
    )


# --- temperature trend (544 × 81 viewBox: plot area 0–60, night bars and day labels below the axis) ---

TREND_X0, TREND_X1 = 30.0, 540.0
PAST_H, FUTURE_H = 23.0, 47.5
PX_PER_H = (TREND_X1 - TREND_X0) / (PAST_H + FUTURE_H)
NOW_X = TREND_X0 + PAST_H * PX_PER_H
TREND_AXIS = 60.0
# The 2026-10-24 reflow scaled the original 70 px plot to 60 px; every vertical offset inside the plot scaled with
# it (labels too), so label placement decisions are unchanged.
_TS = TREND_AXIS / 70.0
TREND_TOP = 4.0 * _TS
LABEL_ABOVE, LABEL_BELOW, LABEL_BOTTOM_MAX, LABEL_CAP = 5.6 * _TS, 9.75 * _TS, 66.0 * _TS, 9.0 * _TS
GRID_MIN, GRID_MAX = 2.0 * _TS, 68.0 * _TS  # gridlines kept only inside this band
GRID_LABEL_DY = _r2(3.0 * _TS)  # gridline label baseline below its line
NIGHT_Y, DAY_LABEL_Y = TREND_AXIS + 2, TREND_AXIS + 18  # night bars and day names, below the axis


def _trend_scale(values: list[float]) -> tuple[float, float]:
    """(lo, span) of the y domain: at least 30°, ≥ 1.5° under the minimum (even number), ≥ 1° over the max."""
    lo = 2 * math.floor((min(values) - 1.5) / 2)
    span = max(30, 2 * math.ceil((max(values) - lo + 1) / 2))
    return lo, span


def _downsample_future(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Board cadence: first hour, the next, then every 3 h from the first, plus the last point."""
    if len(points) <= 2:
        return points
    h0 = points[0][0]
    keep = []
    for i, (h, v) in enumerate(points):
        off = round(h - h0)
        if off in (0, 1) or off % 3 == 0 or i == len(points) - 1:
            keep.append((h, v))
    return keep


def _runs(vals: list[float]) -> list[tuple[int, int, float]]:
    """Runs of equal values: (first index, last index, value)."""
    runs: list[tuple[int, int, float]] = []
    for i, v in enumerate(vals):
        if runs and runs[-1][2] == v:
            runs[-1] = (runs[-1][0], i, v)
        else:
            runs.append((i, i, v))
    return runs


TREND_LABEL_SIZE = 12  # trend.j2 point labels: 12 px / 700, text-anchor middle
LABEL_ASCENT = 0.73 * TREND_LABEL_SIZE  # digit/degree height above the baseline
LABEL_TOP_MIN = 0.5  # label tops stay inside the svg
TREND_VIEW_W = 544.0
EXTREME_MIN_DEG = 2  # an upcoming low/high must differ from its neighbours by this much to be labelled
LABEL_SHIFTS = (0.0, -10.0, 10.0, -18.0, 18.0)
DAY_LABEL_GAP = 12  # min space between NOW and a day name under the axis
Span = tuple[float, float]


def _trend_items(past: list[tuple[float, float]], fut: list[tuple[float, float]]) -> list[tuple[Span, float, bool]]:
    """Labelled points ((first, last) hours of the run, value, is_low): past low, upcoming lows/highs, end value."""
    items: list[tuple[Span, float, bool]] = []
    if past:  # past low (middle of the lowest run)
        vals = [v for _, v in past]
        a, b, v = min(_runs(vals), key=lambda r: r[2])
        items.append(((past[a][0], past[b][0]), v, True))
    if len(fut) >= 3:
        vals = [v for _, v in fut]
        runs = _runs(vals)
        ext: list[tuple[Span, float, bool]] = []  # interior extremes
        for j in range(1, len(runs) - 1):
            a, b, v = runs[j]
            if a == 0 or b == len(vals) - 1:
                continue
            prev_v, next_v = runs[j - 1][2], runs[j + 1][2]
            if (v < prev_v and v < next_v) or (v > prev_v and v > next_v):
                ext.append(((fut[a][0], fut[b][0]), v, v < prev_v))
        # zigzag filter: drop small wiggles (a low/high pair closer than EXTREME_MIN_DEG) so only real
        # turns get a label; the endpoints anchor the comparison.
        while ext:
            chain = [vals[0], *(e[1] for e in ext), vals[-1]]
            diffs = [abs(chain[i + 1] - chain[i]) for i in range(len(chain) - 1)]
            i = min(range(len(diffs)), key=diffs.__getitem__)
            if diffs[i] >= EXTREME_MIN_DEG:
                break
            if i == 0:  # start point – first extreme
                del ext[0]
            elif i == len(diffs) - 1:  # last extreme – end point
                del ext[-1]
            else:  # two neighbouring extremes (a low and a high): drop the pair
                del ext[i - 1:i + 1]
        items += ext
        items.append(((fut[-1][0], fut[-1][0]), vals[-1], vals[-1] < vals[-2]))
    return items


def _seg_hits_box(p: tuple[float, float], q: tuple[float, float], box: tuple[float, float, float, float]) -> bool:
    x0, y0, x1, y1 = box
    n = max(1, int(math.hypot(q[0] - p[0], q[1] - p[1])))
    for i in range(n + 1):
        t = i / n
        px, py = p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t
        if x0 <= px <= x1 and y0 <= py <= y1:
            return True
    return False


def _place_label(px: float, py: float, low: bool, text: str, gridline_ys: list[float],
                 placed: list[tuple[float, float, float, float]], line: list[tuple[float, float]],
                 now_pt: tuple[float, float]) -> tuple[float, float] | None:
    """Label position for a chart point: below a low / above a high when that is clear, else nudged sideways or
    to the other side. Never outside the chart, never over another label, the NOW marker or (if avoidable) the
    line. The first choice is the reference boards' rule, so their labels stay where they were."""
    w = measure(text, TREND_LABEL_SIZE, 700)
    base_x = min(max(px, TREND_X0 + 8), TREND_X1 - 8)
    sides = ("below", "above") if low else ("above", "below")
    cands = [(side, dx) for side in sides for dx in LABEL_SHIFTS]
    cands.sort(key=lambda c: (abs(c[1]) > 0, sides.index(c[0]), abs(c[1])))  # both sides unshifted first

    def ok(side: str, dx: float, strict: bool) -> tuple[float, float] | None:
        lx = min(max(base_x + dx, w / 2 + 1), TREND_VIEW_W - w / 2 - 1)
        if side == "below":
            ly = py + LABEL_BELOW
            if ly > LABEL_BOTTOM_MAX or any(ly - LABEL_CAP <= g <= ly for g in gridline_ys):
                return None
        else:
            ly = py - LABEL_ABOVE
        box = (lx - w / 2, ly - LABEL_ASCENT, lx + w / 2, ly + 0.5)
        if box[1] < LABEL_TOP_MIN:
            return None
        if any(box[0] - 2 < b[2] and b[0] < box[2] + 2 and box[1] - 1 < b[3] and b[1] < box[3] + 1 for b in placed):
            return None
        if box[0] - 2 <= now_pt[0] <= box[2] + 2:  # the NOW rule runs the full plot height
            return None
        # The line may clip a label's corners (the reference boards do, at sharp lows); it must not run through
        # the middle of the text.
        core = (lx - w / 4, ly - LABEL_ASCENT * 0.8, lx + w / 4, ly - LABEL_ASCENT * 0.2)
        if strict and any(_seg_hits_box(a, b, core) for a, b in zip(line, line[1:], strict=False)):
            return None
        return _r2(lx), _r2(ly)

    for strict in (True, False):
        for side, dx in cands:
            pos = ok(side, dx, strict)
            if pos is not None:
                return pos
    return None


def _trend(d: _Data, now: datetime, tz: ZoneInfo, sun: dict | None) -> TrendChart:
    t_now = d.num("outdoor.temperature")

    def series(field: str, key: str) -> list[tuple[float, float]]:
        out = []
        for p in d.records(field):
            t = _dt(p.get("t"), tz)
            try:
                v = float(p[key])
            except (KeyError, TypeError, ValueError):
                continue
            if t is not None and math.isfinite(v):
                out.append(((t - now).total_seconds() / 3600, v))
        return sorted(out)

    past = series("outdoor.temperature_history", "v")
    past = [(h, v) for h, v in past if -PAST_H - 1e-6 <= h <= 1e-6]
    fut = series("forecast.hourly", "temp")
    fut = _downsample_future([(h, v) for h, v in fut if 1e-6 < h <= FUTURE_H + 1e-6])
    if t_now is None and past:
        t_now = past[-1][1]
    if t_now is None or not (past or fut):
        return TrendChart(aria_label="Temperature trend unavailable", axis_y=TREND_AXIS, grid_label_dy=GRID_LABEL_DY,
                          night_y=NIGHT_Y, day_label_y=DAY_LABEL_Y, message=messages.NO_TREND)

    values = [v for _, v in past] + [v for _, v in fut] + [t_now]
    lo, span = _trend_scale(values)
    items = _trend_items(past, fut)
    # Headroom: a label above the highest labelled point must stay inside the chart; widen the domain upward
    # (2° at a time) until it does. Past and future values both count (the scale covers the whole timeline).
    for _ in range(40):
        k = (TREND_AXIS - TREND_TOP) / span
        tops = [TREND_AXIS - (v - lo) * k for _, v, low in items if not low]
        if not tops or min(tops) - LABEL_ABOVE - LABEL_ASCENT >= LABEL_TOP_MIN:
            break
        span += 2
    k = (TREND_AXIS - TREND_TOP) / span

    def x(h: float) -> float:
        return _r2(NOW_X + h * PX_PER_H)

    def y(v: float) -> float:
        return _r2(TREND_AXIS - (v - lo) * k)

    grid = [(y(g), f"{g}°") for g in reversed(range(int(math.ceil(lo / 10) * 10), lo + span + 1, 10))
            if GRID_MIN <= TREND_AXIS - (g - lo) * k <= GRID_MAX]  # top to bottom
    gridline_ys = [g for g, _ in grid]

    # midnights and day names
    start = now - timedelta(hours=PAST_H)
    end = now + timedelta(hours=FUTURE_H)
    midnights, day_labels = [], []
    m = datetime.combine(start.date() + timedelta(days=1), datetime.min.time(), tzinfo=tz)
    while m < end:
        mx = x((m - now).total_seconds() / 3600)
        midnights.append(mx)
        day_labels.append((float(_round(mx + 4.5)), f"{m:%a}".upper()))
        m = datetime.combine(m.date() + timedelta(days=1), datetime.min.time(), tzinfo=tz)
    if not midnights or midnights[0] - TREND_X0 >= 30:
        day_labels.insert(0, (TREND_X0 + 4, f"{start:%a}".upper()))
    # Keep day names clear of the NOW label: one just after NOW (late evening) moves right, one running into it
    # from the left is left out.
    now_half = measure("NOW", 11.5, 800) / 2 + DAY_LABEL_GAP
    clear = []
    for lx, s in day_labels:
        if lx + measure(s, 11.5, 700) < NOW_X - now_half or lx > NOW_X + now_half:
            clear.append((lx, s))
        elif lx > NOW_X:
            clear.append((float(_round(NOW_X + now_half)), s))
    day_labels = clear

    # nights: sunset → sunrise, extrapolated from today's times (day-length change split evenly)
    nights = []
    if sun:
        rise, sset = _dt(sun.get("sunrise"), tz), _dt(sun.get("sunset"), tz)
        delta = float(sun.get("delta_min_per_day") or 0)
        if rise and sset:
            for dd in range(-2, 4):
                s = sset + timedelta(days=dd, minutes=dd * delta / 2)
                r = rise + timedelta(days=dd + 1, minutes=-(dd + 1) * delta / 2)
                x0 = max(TREND_X0, NOW_X + (s - now).total_seconds() / 3600 * PX_PER_H)
                x1 = min(TREND_X1, NOW_X + (r - now).total_seconds() / 3600 * PX_PER_H)
                if x1 - x0 > 0.5:
                    nights.append((_r2(x0), _r2(x1 - x0)))

    past_pts = [(x(h), y(v)) for h, v in past]
    now_pt = (x(0), y(t_now))
    fut_pts = [(x(h), y(v)) for h, v in fut]

    line = [*past_pts, now_pt, *fut_pts]
    placed: list[tuple[float, float, float, float]] = []
    points: list[tuple[float, float, float, float, str]] = []
    for (ha, hb), v, low in items:
        px, py = (x(ha) + x(hb)) / 2, y(v)  # middle of the run, as the reference boards place it
        text = deg(v)
        pos = _place_label(px, py, low, text, gridline_ys, placed, line, now_pt)
        if pos is None:
            continue  # nowhere to put it without covering another label: leave the point unlabelled
        lx, ly = pos
        w = measure(text, TREND_LABEL_SIZE, 700)
        placed.append((lx - w / 2, ly - LABEL_ASCENT, lx + w / 2, ly + 0.5))
        points.append((_r2(px), py, lx, ly, text))

    aria = f"Temperature, past 24 hours and next 48: now {deg(t_now)}"
    return TrendChart(
        aria_label=aria,
        gridlines=grid,
        midnights=midnights,
        day_labels=day_labels,
        past=past_pts,
        future=[now_pt, *fut_pts],
        nights=nights,
        now=now_pt,
        points=points,
        axis_y=TREND_AXIS,
        grid_label_dy=GRID_LABEL_DY,
        night_y=NIGHT_Y,
        day_label_y=DAY_LABEL_Y,
        past_message=messages.NO_HISTORY if not past else None,
    )


# --- precip chart (300 wide) ---

PRECIP_HOURS = 12
# (axis y, px per %, label size, label dy, hour-label y, svg height). The 2026-10-24 reflow raised the baseline
# 8 px and scaled the bars to fit (48/56 normal, 42/50 with the key); labels below the baseline moved up 8.
PRECIP_NORMAL = (48.0, 0.5 * 48 / 56, 10.5, -4.0 * 48 / 56, 62.0, 66)
PRECIP_COMPACT = (42.0, 0.36 * 42 / 50, 9.5, -3.0 * 42 / 50, 54.0, 56)
BAR_PITCH, BAR_W = 25.0, 15.0
PRECIP_NAMES = {"rain": "rain", "snow": "snow", "ice": "ice"}
KEY_ORDER = ["ice", "snow", "rain"]


def _rain_fill(pop: float) -> str:
    if pop >= 70:
        return "#000000"
    if pop >= 30:
        return "#494949"
    if pop >= 20:
        return "#929292"
    return "#B6B6B6"


KEY_MIN_POP = 10  # hours below this chance don't count toward the type, key or patterning
EMPTY_MAX_POP = 10  # every hour below this: no bars, one sentence (approved 2026-10-24)
COLD_F = 34  # ... "rain or snow" when any of those hours gets this cold
SNOW_SHOWN_IN, ICE_SHOWN_IN, RAIN_SHOWN_IN = 0.05, 0.01, 0.1  # amounts worth a heading


def _precip(d: _Data, now: datetime, tz: ZoneInfo) -> PrecipChart:
    hourly = [h for h in d.records("forecast.hourly") if _dt(h.get("t"), tz)]
    rows = sorted(((_dt(h["t"], tz), h) for h in hourly), key=lambda r: r[0])
    rows = [(t, h) for t, h in rows if t > now][:PRECIP_HOURS]
    if not rows:
        return PrecipChart(title="Chance of precip", right_text="", empty_text=messages.NO_HOURLY)

    def kind(h: dict) -> str:
        k = h.get("precip_type") or "rain"
        return k if k in PRECIP_NAMES else "rain"

    def pop_of(h: dict) -> float:
        return float(h.get("pop") or 0)

    kinds = [kind(h) for _, h in rows]
    weights: dict[str, float] = {}
    for (_, h), k in zip(rows, kinds, strict=True):
        if pop_of(h) >= KEY_MIN_POP:
            weights[k] = weights.get(k, 0) + pop_of(h)
    present = [k for k in KEY_ORDER if k in weights]
    if present:  # dominant type by summed chance (ties: ice, then snow)
        dominant = max(present, key=lambda k: (weights[k], -KEY_ORDER.index(k)))
    else:  # nothing likely: the type most hours would bring (rain on a tie)
        dominant = max(("rain", "snow", "ice"), key=lambda k: (kinds.count(k), k == "rain"))
    patterned = any(k in ("snow", "ice") for k in present)
    key = present if len(present) > 1 else []
    compact = bool(key)
    axis, scale, size, dy, hour_y, height = PRECIP_COMPACT if compact else PRECIP_NORMAL
    title = f"Chance of {PRECIP_NAMES[dominant]}"
    pops = ", ".join(f"{hour_label(t)} {_round(pop_of(h))}%" for t, h in rows)
    aria = f"Chance of precipitation, next 12 hours: {pops}"

    if all(pop_of(h) < EMPTY_MAX_POP for _, h in rows):
        temps = [float(h["temp"]) for _, h in rows if h.get("temp") is not None]
        cold = bool(temps) and min(temps) <= COLD_F
        return PrecipChart(title=title, right_text="next 12 h", height=height, aria_label=aria, axis_y=axis,
                           label_size=size, hour_y=hour_y,
                           empty_text=messages.NO_PRECIP_COLD if cold else messages.NO_PRECIP)

    bars, hour_labels = [], []
    for i, ((t, h), k) in enumerate(zip(rows, kinds, strict=True)):
        pop = pop_of(h)
        bh = _r2(pop * scale)
        bx = 5 + i * BAR_PITCH
        by = _r2(axis - bh)
        if patterned:
            fill = {"snow": "url(#wBarSnow)", "ice": "url(#wBarMix)"}.get(k, "#B6B6B6")
        else:
            fill = _rain_fill(pop)
        label = str(_round(pop)) if pop >= 20 else None
        bars.append(PrecipBar(x=bx, y=by, height=bh, fill=fill, label=label,
                              label_y=_r2(by + dy) if label else None))
        if i % 3 == 0:
            hour_labels.append((3.0, hour_label(t), "start") if i == 0
                               else (bx + BAR_W / 2, hour_label(t), "middle"))

    snow = sum(float(h.get("snow_in") or 0) for _, h in rows)
    ice = sum(float(h.get("ice_in") or 0) for _, h in rows)
    rain = sum(float(h.get("rain_in") or 0) for _, h in rows)
    amounts = []
    if snow >= SNOW_SHOWN_IN:
        amounts.append(summary.snow_range(snow))
    if ice >= ICE_SHOWN_IN:
        amounts.append(f"ice {ice:.2f} in")
    if not amounts and dominant == "rain" and rain >= RAIN_SHOWN_IN:
        amounts.append(summary.rain_amount(rain))
    return PrecipChart(
        title=title,
        right_text=" · ".join(amounts) if amounts else "next 12 h",
        right_bold=bool(amounts),
        patterned=patterned,
        height=height,
        bars=bars,
        hour_labels=hour_labels,
        key=key,
        aria_label=aria,
        axis_y=axis,
        label_size=size,
        hour_y=hour_y,
    )


def _amount(v: float | None) -> str:
    return "" if v is None else f"{v:.2f}"


def _snow_in(v: float | None) -> str:
    if v is None:
        return ""
    return f"{v:.0f} in" if abs(v - round(v)) < 0.05 else f"{v:.1f} in"


SNOW_ON_GROUND_IN = 1.0
SNOW_SEASON_MONTHS = {11, 12, 1, 2, 3}


def winter_gauge(depth: float | None, season: float | None, now: datetime) -> bool:
    """SPEC: the gauge switches to liquid totals + NWS snow when snow is on the ground (≥ 1 in) or, from November
    to March, once the season has had any snow. (A spring snow that melted in April doesn't keep winter mode.)"""
    if depth is not None and depth >= SNOW_ON_GROUND_IN:
        return True
    return now.month in SNOW_SEASON_MONTHS and season is not None and season > 0


def gauge_fields(d: _Data, now: datetime) -> list[str]:
    """Fields behind the four gauge cells (normal or winter set)."""
    if winter_gauge(d.num("snow.depth"), d.num("snow.season"), now):
        return ["rain.today", "rain.month", "snow.depth", "snow.season"]
    return ["rain.today", "rain.week", "rain.month", "rain.year"]


def _gauge(d: _Data, now: datetime) -> Gauge:
    depth, season = d.num("snow.depth"), d.num("snow.season")
    winter = winter_gauge(depth, season, now)
    month = f"{now:%B}"

    def cell(field: str, text: str, label: str) -> GaugeCell:
        return GaugeCell(value=d.slot(field, text, narrow=True), label=label)

    if winter:
        return Gauge(title="Gauge", note="liquid in · snow via NWS", note_size=11, cells=[
            cell("rain.today", _amount(d.num("rain.today")), "Today"),
            cell("rain.month", _amount(d.num("rain.month")), month),
            cell("snow.depth", _snow_in(depth), "Snow depth · NWS"),
            cell("snow.season", _snow_in(season), "Season snow · NWS"),
        ])
    return Gauge(title="Rain gauge", note="inches", cells=[
        cell("rain.today", _amount(d.num("rain.today")), "Today"),
        cell("rain.week", _amount(d.num("rain.week")), "This week"),
        cell("rain.month", _amount(d.num("rain.month")), month),
        cell("rain.year", _amount(d.num("rain.year")), f"{now:%Y}"),
    ])


# --- sun & moon ---

ARC_CX, ARC_Y, ARC_RX, ARC_RY = 115.0, 46.0, 101.0, 36.0


def _sun(d: _Data, now: datetime, tz: ZoneInfo) -> Sun | None:
    s = d.get("astro.sun")
    if not isinstance(s, dict):
        return None
    rise, sset = _dt(s.get("sunrise"), tz), _dt(s.get("sunset"), tz)
    if not rise or not sset:
        return None
    length = s.get("day_length_min")
    length = int(length) if length is not None else int((sset - rise).total_seconds() // 60)
    delta = _round(float(s.get("delta_min_per_day") or 0))
    arc = None
    if rise <= now <= sset:
        f = (now - rise) / (sset - rise)
        th = math.pi * (1 - f)
        arc = (_r2(ARC_CX + ARC_RX * math.cos(th)), _r2(ARC_Y - ARC_RY * math.sin(th)))
    delta_text = f"{MINUS}{abs(delta)} m/day" if delta < 0 else f"+{delta} m/day" if delta > 0 else "0 m/day"
    return Sun(
        rise=clock(rise, d.tz), set=clock(sset, d.tz), day_length=f"{length // 60} h {length % 60} m",
        delta=delta_text, arc_end=arc,
        aria_label=f"Sun path: rises {clock(rise, d.tz)}, sets {clock(sset, d.tz)}; "
                   f"{length // 60} hours {length % 60} minutes of daylight",
    )


MOON_R = 25.0


def moon_shadow(illum: float, waxing: bool) -> tuple[float, str]:
    """(terminator x-radius, path of the dark part) for the 36 px moon (r 25, centered at 0,0)."""
    k = max(0.0, min(1.0, illum))
    rx = _r2(MOON_R * abs(2 * k - 1))
    gibbous = k >= 0.5
    if waxing:  # lit on the right: dark on the left
        outer, inner = "0 0 0", ("0 0 1" if gibbous else "0 0 0")
    else:
        outer, inner = "0 0 1", ("0 0 0" if gibbous else "0 0 1")
    return rx, f"M0,-25 A25,25 {outer} 0,25 A{rx:g},25 {inner} 0,-25 Z"


def _moon(d: _Data, tz: ZoneInfo) -> Moon | None:
    m = d.get("astro.moon")
    if not isinstance(m, dict):
        return None
    illum = float(m.get("illumination") or 0)
    illum_frac = illum / 100 if illum > 1 else illum
    name = m.get("phase_name", "")
    waxing = m.get("waxing")
    if waxing is None:
        waxing = "waxing" in name or "first" in name
    rx, path = moon_shadow(illum_frac, bool(waxing))
    rise, sset = _dt(m.get("rise"), tz), _dt(m.get("set"), tz)
    parts = []
    if rise:
        parts.append(f"Rises {clock(rise, d.tz)}")
    if sset:
        parts.append(f"{'sets' if parts else 'Sets'} {clock(sset, d.tz)}")
    labels = {"full": "Full", "last": "Last", "new": "New", "first": "First"}
    nxt = []
    for p in (m.get("next") or [])[:4]:
        dt = _date(p.get("date"))
        if p.get("kind") in labels and dt:
            nxt.append(MoonPhase(kind=p["kind"], label=labels[p["kind"]], date=f"{dt.month}/{dt.day}"))
    pct_text = f"{_round(illum_frac * 100)}%"
    return Moon(
        illumination=pct_text, phase_name=name, terminator_rx=rx, waxing=bool(waxing),
        rise_set=" · ".join(parts), next=nxt, shadow_path=path,
        aria_label=f"Moon: {name}, {_round(illum_frac * 100)} percent lit",
    )


# --- 7-day list ---


SNOW_DAY_IN = 0.5  # a day with at least this much snow shows snowflake + amount instead of %


def _forecast(daily: list[dict], now: datetime) -> list[ForecastRow]:
    dated = [(_date(day.get("date")), day) for day in daily]
    if all(dt is not None for dt, _ in dated):  # drop days already past (a forecast cached overnight)
        dated = [(dt, day) for dt, day in dated if dt >= now.date()]
    rows = []
    for i, (dt, day) in enumerate(dated[:7]):
        dt = dt or (now.date() + timedelta(days=i))
        kind = day.get("precip_type") or "rain"
        kind = kind if kind in ("rain", "snow", "ice") else "rain"
        snow = float(day.get("snow_in") or 0)
        if snow >= SNOW_DAY_IN:
            kind, ptext = "snow", _snow_in(snow)
        else:
            ptext = pct(float(day.get("pop") or 0))
        wd = day.get("wind_dir")
        if isinstance(wd, str) and wd.upper() in icons.WIND_DIRS:
            wd = icons.WIND_DIRS.index(wd.upper()) * 45.0
        speed = day.get("wind_speed")
        name = icons.forecast(day.get("icon"))
        mods = icons.forecast_modifiers(name, None if speed is None else _round(float(speed)))
        label = str(day.get("text", "")) + (", windy" if mods else "")
        is_today = dt == now.date()
        rows.append(ForecastRow(
            day="Today" if is_today else f"{dt:%a}",
            is_today=is_today,
            icon=Icon(name=name, modifiers=mods, label=label),
            text=day.get("text", ""),
            wind=_wind(None if wd is None else float(wd), speed),
            precip_kind=kind,
            precip_text=ptext,
            high=deg(day.get("high")),
            low=deg(day.get("low")),
        ))
    return rows


# ------------------------------------------------------------------ entry point


def build(resolved: Mapping[str, Resolved], now: datetime, device: DeviceInfo | None = None,
          cfg: AppConfig | None = None) -> DashboardContext:
    tz_name = cfg.location.tz if cfg else "America/Chicago"
    tz = ZoneInfo(tz_name)
    now = now.astimezone(tz)
    device = device or DeviceInfo()
    types = {n: p.type for n, p in cfg.providers.items()} if cfg else {}
    d = _Data(resolved, tz_name, types)
    daily = d.records("forecast.daily")
    inp = summary.Inputs.build(now, tz, d.records("forecast.hourly"), daily, _current_code(d)[0],
                               d.records("alerts.active"), d.num("outdoor.temperature"))
    sun_raw = d.get("astro.sun")
    sun = _sun(d, now, tz) or Sun(rise="", set="", day_length="", delta="", arc_end=None,
                                  message=_section_message(d, "astro.sun"))
    moon = _moon(d, tz) or Moon(illumination="", phase_name="", terminator_rx=0, waxing=True, rise_set="", next=[],
                                message=_section_message(d, "astro.moon"))
    forecast = _forecast(daily, now)
    readings = _readings(d)
    gauge = _gauge(d, now)
    sections = missing_sections(d, readings, gauge, sun, moon, forecast)
    return DashboardContext(
        now=now,
        tz=tz_name,
        width=cfg.server.width if cfg else 1200,
        height=cfg.server.height if cfg else 825,
        header=_header(d, now, device, cfg, inp, sections),
        radar=_radar(d, tz, inp),
        readings=readings,
        current=_current(d, inp),
        trend=_trend(d, now, tz, sun_raw if isinstance(sun_raw, dict) else None),
        precip=_precip(d, now, tz),
        gauge=gauge,
        sun=sun,
        moon=moon,
        forecast=forecast,
        forecast_message=None if forecast else _section_message(d, "forecast.daily"),
        notices=_notices(d, now, cfg)[:MAX_NOTICES],
    )


# ------------------------------------------------------------------ error states (phase 2.6)


def _section_message(d: _Data, field: str) -> str:
    """Sentence for a section that has nothing to draw."""
    r = d.resolved.get(field)
    return (messages.explain(r, d.tz, d.source_types) if r is not None else None) or \
        messages.MISSING.format(label=messages.label_of(field))


def missing_sections(d: _Data, readings: list[ReadingCell], gauge: Gauge, sun: Sun, moon: Moon,
                     forecast: list[ForecastRow]) -> list[str]:
    """Sections that can't show their main content, most important first (messages.SECTION_NAMES)."""
    out = []
    if d.missing("alerts.active"):
        out.append("alerts")
    if radar_configured(d) and not isinstance(d.get("radar.frame"), dict):
        out.append("radar")
    if d.missing("forecast.daily") or (d.resolved.get("forecast.daily") is not None and not forecast):
        out.append("forecast")
    elif d.missing("forecast.hourly"):  # both gone reads as just "forecast"
        out.append("hourly")
    if d.missing("outdoor.temperature"):
        out.append("current")
    if sum(c.slot.status is Status.missing for c in readings) >= 2:
        out.append("readings")
    if all(c.value.status is Status.missing for c in gauge.cells):
        out.append("gauge")
    if sun.message or moon.message:
        out.append("sun_moon")
    return out


def _primary_failed(d: _Data, r: Resolved, cfg: AppConfig | None) -> tuple[bool, str | None]:
    """(note it, primary source) for a value that came from a fallback source.

    Live: the first source tried failed (error, too old, no value), so the second one won. A source that simply
    doesn't provide the value (no_such_key) is a config matter, not an outage: not noted. Fixtures serve every field
    from one provider; a fixture reading attributed to another source names the configured first source.
    """
    winner = r.reading.source
    first = r.tried[0]
    if first.source != winner and first.outcome is not Status.ok:
        return first.note != "no_such_key", first.source
    srcs = cfg.fields.get(r.field) if cfg else None
    if srcs and srcs[0].from_ != winner and messages.source_type(srcs[0].from_, d.source_types) != "fixture":
        return True, srcs[0].from_
    return True, None


def _fallback_notes(d: _Data, cfg: AppConfig | None) -> list[str]:
    notes: dict[tuple[str, str, str | None], None] = {}
    for field, what in messages.FALLBACK_GROUPS.items():
        r = d.resolved.get(field)
        if r is None or not r.reading.usable or not r.fallback_used:
            continue
        show, primary = _primary_failed(d, r, cfg)
        if show:
            notes[(what, r.reading.source, primary)] = None
    return [messages.fallback_note(what, winner, primary, d.source_types) for what, winner, primary in notes]


def _notices(d: _Data, now: datetime, cfg: AppConfig | None) -> list[str]:
    """Notes box, most important first: alerts that couldn't be checked, missing and stale readings (full sentences
    for the cells' short forms), a stale forecast, then fallbacks used."""
    out = []
    if d.missing("alerts.active"):
        out.append(_section_message(d, "alerts.active"))
    if d.missing("forecast.hourly") and not d.missing("forecast.daily"):
        out.append(_section_message(d, "forecast.hourly"))
    cells = [d.resolved[f] for f in READING_FIELDS + gauge_fields(d, now) if f in d.resolved]
    out += messages.missing_notes([r for r in cells if not r.reading.usable], d.tz, d.source_types)
    out += messages.stale_notes(cells, d.tz)
    times = {clock(t, d.tz): t for f in ("forecast.daily", "forecast.hourly")
             if d.stale(f) and (t := d.observed(f)) is not None}
    if times:
        out.append(messages.STALE_SECTION.format(what="Forecast", time=min(times.items(), key=lambda x: x[1])[0]))
    out += _fallback_notes(d, cfg)
    return list(dict.fromkeys(out))
