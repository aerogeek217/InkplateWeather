"""Canonical field registry: every value the screen can show, with its unit and kind.

Providers convert to these units. Config (`fields:`) maps each field to sources in priority order.
`label` is the plain-language name used in messages ("Garage sensor hasn't reported …").
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Kind(StrEnum):
    number = "number"
    text = "text"
    series = "series"  # list of {t, v} points
    structured = "structured"  # provider-specific dict/list documented on the field


@dataclass(frozen=True)
class Field:
    name: str
    unit: str | None
    kind: Kind
    label: str
    doc: str = ""


_N, _T, _S, _X = Kind.number, Kind.text, Kind.series, Kind.structured

FIELDS: dict[str, Field] = {
    f.name: f
    for f in [
        # Outdoor station
        Field("outdoor.temperature", "°F", _N, "Outdoor temperature"),
        Field("outdoor.humidity", "%", _N, "Outdoor humidity"),
        Field("outdoor.dew_point", "°F", _N, "Dew point"),
        Field("outdoor.feels_like", "°F", _N, "Feels-like temperature"),
        Field("outdoor.temperature_history", "°F", _S, "Temperature history",
              "past 24 h, [{t: iso datetime, v: °F}], oldest first"),
        Field("wind.speed", "mph", _N, "Wind"),
        Field("wind.gust", "mph", _N, "Wind gusts"),
        Field("wind.direction", "deg", _N, "Wind direction", "degrees the wind blows FROM"),
        Field("uv.index", None, _N, "UV index"),
        Field("solar.radiation", "W/m2", _N, "Solar radiation"),
        # Indoor / garage
        Field("indoor.temperature", "°F", _N, "Inside sensor"),
        Field("indoor.humidity", "%", _N, "Inside humidity"),
        Field("garage.temperature", "°F", _N, "Garage sensor"),
        Field("garage.humidity", "%", _N, "Garage humidity"),
        Field("pressure.sea_level", "inHg", _N, "Pressure"),
        Field("pressure.trend_3h", "inHg", _N, "Pressure trend", "change over the last 3 h"),
        # Rain gauge
        Field("rain.today", "in", _N, "Rain today"),
        Field("rain.week", "in", _N, "Rain this week"),
        Field("rain.month", "in", _N, "Rain this month"),
        Field("rain.year", "in", _N, "Rain this year"),
        Field("rain.rate", "in/h", _N, "Rain rate"),
        # Public data
        Field("air.aqi", "AQI", _N, "Air quality"),
        Field("snow.depth", "in", _N, "Snow depth"),
        Field("snow.season", "in", _N, "Season snowfall"),
        Field("normals.high", "°F", _N, "Average high"),
        Field("normals.low", "°F", _N, "Average low"),
        Field("conditions.current", None, _T, "Current conditions", "icon name from design/icons/conditions"),
        Field("forecast.hourly", None, _X, "Hourly forecast",
              "[{t, temp, pop, precip_type: rain|snow|ice|none, snow_in, ice_in, rain_in}], next 48 h+"),
        Field("forecast.daily", None, _X, "Forecast",
              "[{date, icon, text, high, low, wind_dir, wind_speed, pop, precip_type, snow_in}], today first"),
        Field("alerts.active", None, _X, "Weather alerts", "[{event, ends, headline, impact, severity}]"),
        Field("astro.sun", None, _X, "Sun times", "{sunrise, sunset, day_length_min, delta_min_per_day}"),
        Field("astro.moon", None, _X, "Moon",
              "{phase_name, illumination, rise, set, phase_angle, next: [{kind: full|last|new|first, date}]}"),
        # Radar (phase 3 replaces the phase-1 fixture artwork)
        Field("radar.frame", None, _X, "Radar", "{site, time, layers...}; see providers/radar_*"),
        # Generated text (phase 2 derives these from forecast/radar; phase 1 reads them from fixtures)
        Field("summary.day_line", None, _T, "Day summary"),
        Field("summary.next_hazard", None, _T, "Next hazard"),
        Field("radar.callout", None, _X, "Radar callout", "{kind: storm|snow|rain, headline, detail}"),
        # Device (from the Inkplate's request)
        Field("device.battery_pct", "%", _N, "Display battery"),
        Field("device.voltage_mv", "mV", _N, "Display battery voltage"),
        Field("device.wake_reason", None, _T, "Wake reason"),
    ]
}


def get(name: str) -> Field:
    try:
        return FIELDS[name]
    except KeyError:
        raise KeyError(f"unknown field {name!r}; add it to app/fields.py") from None
