"""DashboardContext: the only thing templates see (built by viewmodel.build).

Contract between the view model and the templates. Top-level sections and the `Slot` model are frozen;
the chart/gauge sub-models may gain fields as the template port needs them (record changes in the PR).
Every string here is already formatted for display; templates never format numbers or call providers.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from .readings import Status


class Slot(BaseModel):
    """One displayed value with its data state (ARCHITECTURE: Error handling)."""

    text: str = ""  # "58°", "29.94"; empty when missing
    sub: str | None = None  # small gray suffix: "41%", "Good"
    status: Status = Status.ok
    as_of: str | None = None  # "as of 1:12 PM" when stale
    message: str | None = None  # plain-language sentence shown instead of text when missing


class Icon(BaseModel):
    name: str  # path under assets/icons without .svg, e.g. "conditions/thunderstorm"
    modifiers: list[str] = []  # e.g. ["wind"], ["frost"], ["alert"] (phase 2.5 rules)
    label: str = ""  # aria-label, e.g. "Now: cloudy and breezy"


class Alert(BaseModel):
    event: str  # "Winter Storm Warning"
    detail: str  # "until 9 AM Wed · 3–5 in snow"


class Header(BaseModel):
    icon: Icon
    city: str
    date_text: str  # "Saturday 24 October"
    day_line: str | None = None  # replaced by `alert` when one is active
    alert: Alert | None = None
    next_update: str | None = None  # "3:00"; not rendered since the 2026-10-24 reflow (kept for the model)
    battery_pct: int | None = None
    battery_low: bool = False
    battery_fill: float = 0  # width of the battery icon's fill bar (0–20 px)
    time_text: str  # "2:30 PM"
    notice: str | None = None  # header summary sentence when ≥ 2 sections are missing (phase 2.6)


class Callout(BaseModel):
    kind: str  # storm | snow | rain → callout icon
    headline: str  # "Storms ~4 PM"
    detail: str  # "22 mi west · ENE 15 mph · ~0.5 in"


class Radar(BaseModel):
    site_time: str  # "KILX 2:25 PM"
    legend: str = "intensity"  # intensity (Light…Heavy) | types (Rain / Ice/sleet / Snow)
    lightning_legend: bool = False
    callout: Callout | None = None
    next_chip: str | None = None  # "Next: frost possible Sunday night (34°)"
    svg_layers: str = ""  # phase 1: fixture artwork (map + echoes + overlays); phase 3: generated
    aria_label: str = ""
    status: Status = Status.ok
    message: str | None = None


class ReadingCell(BaseModel):
    label: str  # "Inside", "Pressure ↓"
    slot: Slot


class Wind(BaseModel):
    dir_text: str  # "S"
    from_deg: float | None  # arrow rotation source; None = calm/unknown
    speed: str  # "14"
    gust: str | None = None  # "26"
    bold: bool = False  # gusts ≥ 20 mph (forecast list rule)


class Current(BaseModel):
    temp: Slot  # "58°"
    high: str | None  # "H 60°"
    low: str | None  # "L 38°"
    feels: str | None  # "Feels 58° · Humidity 64%"
    wind: Wind | None
    normals: str | None  # "Average 63° / 42°"


class TrendChart(BaseModel):
    """Geometry in the 544 × 81 viewBox of the design. Coordinates computed by the view model."""

    aria_label: str = ""
    gridlines: list[tuple[float, str]] = []  # (y, "60°")
    midnights: list[float] = []  # x of dotted day lines
    day_labels: list[tuple[float, str]] = []  # (x, "SAT")
    past: list[tuple[float, float]] = []  # solid line + gray area
    future: list[tuple[float, float]] = []  # dashed line
    nights: list[tuple[float, float]] = []  # (x, width) black bars under the axis
    now: tuple[float, float] | None = None
    points: list[tuple[float, float, float, float, str]] = []  # (dot x, dot y, label x, label y, "42°")
    axis_y: float = 60  # x axis (bottom of the plot area)
    grid_label_dy: float = 2.57  # gridline label baseline below its line
    night_y: float = 62  # top of the night bars
    day_label_y: float = 78  # baseline of the day names and NOW


class PrecipBar(BaseModel):
    x: float
    y: float
    height: float
    fill: str  # "#494949" or "url(#…)" pattern id from the template
    label: str | None = None  # "80" when ≥ 20 %
    label_y: float | None = None


class PrecipChart(BaseModel):
    title: str  # "Chance of rain"
    right_text: str  # "next 12 h" or "3–5 in · ice 0.05 in"
    right_bold: bool = False
    patterned: bool = False  # winter style (outlined, patterned bars)
    height: int = 66
    bars: list[PrecipBar] = []
    hour_labels: list[tuple[float, str, str]] = []  # (x, "6 PM", text-anchor)
    key: list[str] = []  # precip types shown in the key (only when > 1 type)
    aria_label: str = ""
    axis_y: float = 48  # baseline of the bars (48 normally, 42 when the key needs room)
    label_size: float = 10.5  # percent-label font size
    hour_y: float = 62  # baseline of the hour labels


class GaugeCell(BaseModel):
    value: Slot
    label: str  # "Today", "Snow depth · NWS"


class Gauge(BaseModel):
    title: str  # "Rain gauge" | "Gauge"
    note: str  # "inches" | "liquid in · snow via NWS"
    cells: list[GaugeCell]
    note_size: int = 12  # the winter note is longer and set at 11 px


class Sun(BaseModel):
    rise: str  # "7:17 AM"
    set: str
    day_length: str  # "10 h 51 m"
    delta: str  # "−2 m/day"
    arc_end: tuple[float, float] | None  # sun position on the arc (None at night)
    aria_label: str = ""


class MoonPhase(BaseModel):
    kind: str  # full | last | new | first
    label: str  # "Full"
    date: str  # "10/26"


class Moon(BaseModel):
    illumination: str  # "97%"
    phase_name: str  # "waxing gibbous"
    terminator_rx: float  # inner arc x-radius of the 36 px phase drawing
    waxing: bool
    rise_set: str  # "Rises 5:21 PM · sets 6:04 AM"
    next: list[MoonPhase]
    aria_label: str = ""
    shadow_path: str = ""  # dark part of the 36 px phase drawing (viewBox -28 -28 56 56, r 25)


class ForecastRow(BaseModel):
    day: str  # "Today", "Sun"
    is_today: bool
    icon: Icon
    text: str  # "Storms late"
    wind: Wind
    precip_kind: str  # rain | snow | ice
    precip_text: str  # "80%" | "4 in"
    high: str  # "60°"
    low: str  # "38°"


class DashboardContext(BaseModel):
    now: datetime
    tz: str
    width: int = 1200
    height: int = 825
    header: Header
    radar: Radar
    readings: list[ReadingCell]  # Inside · Garage · Pressure · Air quality · UV index
    current: Current
    trend: TrendChart
    precip: PrecipChart
    gauge: Gauge
    sun: Sun
    moon: Moon
    forecast: list[ForecastRow]  # 7 rows, today first
    notices: list[str] = []  # extra plain-language notes (fallback used, battery strip …)
