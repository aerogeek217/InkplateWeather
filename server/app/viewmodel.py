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

from . import icons, messages
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

MINUS = "−"
BOLD_WIND_MPH = 20
BATTERY_LOW_PCT = 15

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
    d = v if isinstance(v, datetime) else datetime.fromisoformat(str(v))
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

    def num(self, field: str) -> float | None:
        v = self.get(field)
        try:
            return None if v is None else float(v)
        except (TypeError, ValueError):
            return None

    def slot(self, field: str, text: str, sub: str | None = None) -> Slot:
        r = self.resolved.get(field)
        if r is None:
            return Slot(status=Status.missing, message=messages.MISSING.format(label=_label(field)))
        if not r.reading.usable or not text:
            return Slot(status=Status.missing, message=messages.explain(r, self.tz, self.source_types))
        return Slot(text=text, sub=sub, status=r.reading.status, as_of=messages.as_of(r, self.tz))


def _label(field: str) -> str:
    from . import fields

    f = fields.FIELDS.get(field)
    return f.label if f else field


# ------------------------------------------------------------------ sections


def _header(d: _Data, now: datetime, device: DeviceInfo, cfg: AppConfig | None, daily: list[dict]) -> Header:
    tz = ZoneInfo(d.tz)
    code = d.get("conditions.current") or ""
    alerts = d.get("alerts.active") or []
    alert = None
    if alerts:
        a = alerts[0]
        ends = _dt(a.get("ends"), tz)
        detail = []
        if ends:
            when = short_clock(ends)
            if ends.date() != now.date():
                when += f" {ends:%a}"
            detail.append(f"until {when}")
        if a.get("impact"):
            detail.append(a["impact"])
        alert = Alert(event=a.get("event", "Weather alert"), detail=" · ".join(detail))
    name = icons.condition(code)
    mods = icons.header_modifiers(name, d.num("wind.gust"), d.num("outdoor.temperature"), bool(alert))
    label = "Now: " + (code.replace("-", " ") if code else "unknown")
    if "wind" in mods:
        label += " and windy"
    batt = device.battery_pct
    nxt = next_refresh(now, (cfg.server.refresh_minutes if cfg else 30))
    return Header(
        icon=Icon(name=name, modifiers=mods, label=label),
        city=cfg.location.name if cfg else "",
        date_text=f"{now:%A} {now.day} {now:%B}",
        day_line=d.get("summary.day_line"),
        alert=alert,
        next_update=None if alert else f"{nxt.hour % 12 or 12}:{nxt.minute:02d}",
        battery_pct=batt,
        battery_low=batt is not None and batt <= BATTERY_LOW_PCT,
        battery_fill=round(20 * max(0, min(100, batt)) / 100, 1) if batt is not None else 0,
        time_text=clock(now, d.tz),
    )


def _radar(d: _Data, tz: ZoneInfo) -> Radar:
    frame = d.get("radar.frame")
    callout = d.get("radar.callout")
    hazard = d.get("summary.next_hazard")
    co = None
    if isinstance(callout, dict) and callout.get("headline"):
        co = Callout(kind=callout.get("kind", "rain"), headline=callout["headline"], detail=callout.get("detail", ""))
    if not isinstance(frame, dict):
        r = d.resolved.get("radar.frame")
        msg = messages.explain(r, d.tz, d.source_types) if r else messages.MISSING.format(label="Radar")
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
        return d.slot(f"{prefix}.temperature", deg1(t), pct(rh) if rh is not None else None)

    p = d.num("pressure.sea_level")
    trend = d.num("pressure.trend_3h")
    arrow = ""
    if trend is not None:
        arrow = " ↓" if trend <= -0.02 else " ↑" if trend >= 0.02 else " →"
    aqi = d.num("air.aqi")
    uv = d.num("uv.index")
    return [
        ReadingCell(label="Inside", slot=temp_rh("indoor")),
        ReadingCell(label="Garage", slot=temp_rh("garage")),
        ReadingCell(label="Pressure" + arrow, slot=d.slot("pressure.sea_level", f"{p:.2f}" if p is not None else "")),
        ReadingCell(label="Air quality", slot=d.slot("air.aqi", str(_round(aqi)) if aqi is not None else "",
                                                       aqi_category(aqi) if aqi is not None else None)),
        ReadingCell(label="UV index", slot=d.slot("uv.index", str(_round(uv)) if uv is not None else "",
                                                    uv_category(uv) if uv is not None else None)),
    ]


def _wind(from_deg: float | None, speed: float | None, gust: float | None = None) -> Wind:
    return Wind(
        dir_text=icons.wind_dir(from_deg),
        from_deg=from_deg,
        speed="" if speed is None else str(_round(speed)),
        gust=None if gust is None else str(_round(gust)),
        bold=speed is not None and _round(speed) >= BOLD_WIND_MPH,
    )


def _current(d: _Data, daily: list[dict]) -> Current:
    t = d.num("outdoor.temperature")
    today = daily[0] if daily else {}
    feels = d.num("outdoor.feels_like")
    rh = d.num("outdoor.humidity")
    feels_parts = []
    if feels is not None:
        feels_parts.append(f"Feels {deg(feels)}")
    if rh is not None:
        feels_parts.append(f"Humidity {pct(rh)}")
    speed = d.num("wind.speed")
    nh, nl = d.num("normals.high"), d.num("normals.low")
    return Current(
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


def _trend(d: _Data, now: datetime, tz: ZoneInfo, sun: dict | None) -> TrendChart:
    hist = d.get("outdoor.temperature_history") or []
    hourly = d.get("forecast.hourly") or []
    t_now = d.num("outdoor.temperature")

    def hours(t: Any) -> float:
        return (_dt(t, tz) - now).total_seconds() / 3600

    past = sorted((hours(p["t"]), float(p["v"])) for p in hist if p.get("v") is not None)
    past = [(h, v) for h, v in past if -PAST_H - 1e-6 <= h <= 1e-6]
    fut = sorted((hours(p["t"]), float(p["temp"])) for p in hourly if p.get("temp") is not None)
    fut = _downsample_future([(h, v) for h, v in fut if 1e-6 < h <= FUTURE_H + 1e-6])
    if t_now is None and past:
        t_now = past[-1][1]
    if t_now is None or not (past or fut):
        return TrendChart(aria_label="Temperature trend unavailable", axis_y=TREND_AXIS, grid_label_dy=GRID_LABEL_DY,
                          night_y=NIGHT_Y, day_label_y=DAY_LABEL_Y)

    values = [v for _, v in past] + [v for _, v in fut] + [t_now]
    lo, span = _trend_scale(values)
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

    def label_pos(px: float, py: float, low: bool) -> tuple[float, float]:
        lx = _r2(min(max(px, TREND_X0 + 8), TREND_X1 - 8))
        if low:
            below = py + LABEL_BELOW
            if below <= LABEL_BOTTOM_MAX and not any(below - LABEL_CAP <= g <= below for g in gridline_ys):
                return lx, _r2(below)
        return lx, _r2(py - LABEL_ABOVE)

    points: list[tuple[float, float, float, float, str]] = []

    def add(px: float, v: float, low: bool) -> None:
        py = y(v)
        lx, ly = label_pos(px, py, low)
        points.append((_r2(px), py, lx, ly, deg(v)))

    if past:  # past low (middle of the lowest run)
        vals = [v for _, v in past]
        lo_run = min(_runs(vals), key=lambda r: r[2])
        add((past_pts[lo_run[0]][0] + past_pts[lo_run[1]][0]) / 2, lo_run[2], True)
    if len(fut) >= 3:  # upcoming lows/highs (interior extremes), then the end value
        vals = [v for _, v in fut]
        runs = _runs(vals)
        for j in range(1, len(runs) - 1):
            a, b, v = runs[j]
            if a == 0 or b == len(vals) - 1:
                continue
            prev_v, next_v = runs[j - 1][2], runs[j + 1][2]
            if v < prev_v and v < next_v:
                add((fut_pts[a][0] + fut_pts[b][0]) / 2, v, True)
            elif v > prev_v and v > next_v:
                add((fut_pts[a][0] + fut_pts[b][0]) / 2, v, False)
        add(fut_pts[-1][0], vals[-1], vals[-1] < vals[-2])

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


def _snow_range(total: float) -> str:
    if total < 1:
        return f"{total:.1f} in"
    lo, hi = math.floor(total * 0.75), math.ceil(total * 1.25)
    return f"{lo} in" if lo == hi else f"{lo}–{hi} in"


def _precip(d: _Data, now: datetime, tz: ZoneInfo) -> PrecipChart:
    hourly = d.get("forecast.hourly") or []
    rows = sorted(((_dt(h["t"], tz), h) for h in hourly), key=lambda r: r[0])
    rows = [(t, h) for t, h in rows if t > now][:PRECIP_HOURS]
    if not rows:
        return PrecipChart(title="Chance of precip", right_text="unavailable")

    def kind(h: dict) -> str:
        k = h.get("precip_type") or "rain"
        return k if k in PRECIP_NAMES else "rain"

    kinds = [kind(h) for _, h in rows]
    weights: dict[str, float] = {}
    for (_, h), k in zip(rows, kinds, strict=True):
        weights[k] = weights.get(k, 0) + float(h.get("pop") or 0)
    present = [k for k in KEY_ORDER if k in kinds]
    dominant = max(present, key=lambda k: (weights.get(k, 0), -KEY_ORDER.index(k)))
    patterned = any(k in ("snow", "ice") for k in kinds)
    key = present if len(present) > 1 else []
    compact = bool(key)
    axis, scale, size, dy, hour_y, height = PRECIP_COMPACT if compact else PRECIP_NORMAL

    bars, hour_labels = [], []
    for i, ((t, h), k) in enumerate(zip(rows, kinds, strict=True)):
        pop = float(h.get("pop") or 0)
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
    amounts = []
    if snow > 0:
        amounts.append(_snow_range(snow))
    if ice > 0:
        amounts.append(f"ice {ice:.2f} in")
    pops = ", ".join(f"{hour_label(t)} {_round(float(h.get('pop') or 0))}%" for t, h in rows)
    return PrecipChart(
        title=f"Chance of {PRECIP_NAMES[dominant]}",
        right_text=" · ".join(amounts) if amounts else "next 12 h",
        right_bold=bool(amounts),
        patterned=patterned,
        height=height,
        bars=bars,
        hour_labels=hour_labels,
        key=key,
        aria_label=f"Chance of precipitation, next 12 hours: {pops}",
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


def _gauge(d: _Data, now: datetime) -> Gauge:
    depth, season = d.num("snow.depth"), d.num("snow.season")
    winter = (depth or 0) > 0 or (season or 0) > 0
    month = f"{now:%B}"

    def cell(field: str, text: str, label: str) -> GaugeCell:
        return GaugeCell(value=d.slot(field, text), label=label)

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


def _forecast(daily: list[dict], now: datetime) -> list[ForecastRow]:
    rows = []
    for i, day in enumerate(daily[:7]):
        dt = _date(day.get("date")) or (now.date() + timedelta(days=i))
        kind = day.get("precip_type") or "rain"
        kind = kind if kind in ("rain", "snow", "ice") else "rain"
        snow = float(day.get("snow_in") or 0)
        if kind == "snow" and snow >= 0.5:
            ptext = _snow_in(snow)
        else:
            ptext = pct(float(day.get("pop") or 0))
        wd = day.get("wind_dir")
        if isinstance(wd, str) and wd.upper() in icons.WIND_DIRS:
            wd = icons.WIND_DIRS.index(wd.upper()) * 45.0
        rows.append(ForecastRow(
            day="Today" if i == 0 else f"{dt:%a}",
            is_today=i == 0,
            icon=Icon(name=icons.forecast(day.get("icon")), label=day.get("text", "")),
            text=day.get("text", ""),
            wind=_wind(None if wd is None else float(wd), day.get("wind_speed")),
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
    daily = [x for x in (d.get("forecast.daily") or []) if isinstance(x, dict)]
    sun_raw = d.get("astro.sun")
    sun = _sun(d, now, tz) or Sun(rise="", set="", day_length="", delta="", arc_end=None)
    moon = _moon(d, tz) or Moon(illumination="", phase_name="", terminator_rx=0, waxing=True, rise_set="", next=[])
    return DashboardContext(
        now=now,
        tz=tz_name,
        width=cfg.server.width if cfg else 1200,
        height=cfg.server.height if cfg else 825,
        header=_header(d, now, device, cfg, daily),
        radar=_radar(d, tz),
        readings=_readings(d),
        current=_current(d, daily),
        trend=_trend(d, now, tz, sun_raw if isinstance(sun_raw, dict) else None),
        precip=_precip(d, now, tz),
        gauge=_gauge(d, now),
        sun=sun,
        moon=moon,
        forecast=_forecast(daily, now),
    )
