"""Shapes of the structured field values (forecast lists, alerts, sun, moon, history).

Contract between providers and the view model; change only with the orchestrator. Providers build these models
and put `dump(...)` (plain JSON-style dicts, ISO datetimes) into `Reading.value`, which is exactly what the fixtures
carry, so the view model reads live and fixture data the same way. Units are the canonical ones from fields.py.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

PrecipType = Literal["rain", "snow", "ice"]  # "ice" covers sleet and freezing rain; rain is the default


class HistoryPoint(BaseModel):
    """outdoor.temperature_history: past 24 h, oldest first (°F)."""

    t: datetime
    v: float


class HourlyPoint(BaseModel):
    """forecast.hourly: from the current hour, at least 48 entries, hourly."""

    t: datetime  # start of the hour, timezone-aware
    temp: float  # °F
    pop: int = Field(ge=0, le=100)  # chance of precipitation, %
    precip_type: PrecipType = "rain"  # what would fall (dominant), even when pop is 0
    rain_in: float = 0.0  # expected liquid amounts for the hour, inches
    snow_in: float = 0.0
    ice_in: float = 0.0
    wind_speed: float | None = None  # mph
    wind_gust: float | None = None
    wind_dir: float | None = None  # degrees FROM
    icon: str | None = None  # condition name (conditions.py), when the source gives one


class DailyForecast(BaseModel):
    """forecast.daily: 7 entries, today first (calendar days in the display timezone)."""

    date: date
    icon: str  # condition name from conditions.py (e.g. "thunderstorm"); modifiers are added by the view model
    text: str  # short description, ≤ ~24 chars ("Storms late", "Partly cloudy")
    high: float | None  # °F; None when the source no longer has today's high (evening)
    low: float | None
    wind_dir: float | None  # degrees FROM, daytime prevailing
    wind_speed: float | None  # mph, daytime sustained (upper end if a range)
    wind_gust: float | None = None
    pop: int = Field(0, ge=0, le=100)
    precip_type: PrecipType = "rain"
    snow_in: float = 0.0  # expected snowfall for the day, inches
    ice_in: float = 0.0
    detail: str | None = None  # longer source text (NWS detailedForecast), for the day line fallback


class Alert(BaseModel):
    """alerts.active: most severe first."""

    event: str  # "Winter Storm Warning"
    ends: datetime | None  # NWS `ends`, else `expires`
    onset: datetime | None = None
    headline: str = ""
    impact: str | None = None  # short key impact for the header ("3–5 in snow"), None if not derivable
    severity: str = "Unknown"  # Extreme | Severe | Moderate | Minor | Unknown
    url: str | None = None


class SunInfo(BaseModel):
    """astro.sun for today in the display timezone."""

    sunrise: datetime
    sunset: datetime
    day_length_min: int
    delta_min_per_day: int  # today's day length minus yesterday's, rounded


class MoonPhaseDate(BaseModel):
    kind: Literal["new", "first", "full", "last"]
    date: date  # local date


class MoonInfo(BaseModel):
    """astro.moon now."""

    phase_name: str  # new moon | waxing crescent | first quarter | waxing gibbous | full moon | waning gibbous |
    #                  last quarter | waning crescent
    illumination: int = Field(ge=0, le=100)  # % lit
    waxing: bool
    rise: datetime | None  # next rise/set around today (None if none that day)
    set: datetime | None
    next: list[MoonPhaseDate]  # the next four principal phases, in order


def dump(v: BaseModel | list[BaseModel]) -> Any:
    """Plain JSON-style value for Reading.value."""
    if isinstance(v, list):
        return [x.model_dump(mode="json", exclude_none=False) for x in v]
    return v.model_dump(mode="json", exclude_none=False)


# Which shape each structured field uses (tests validate fixtures and provider output against these).
FIELD_SHAPES: dict[str, tuple[type[BaseModel], bool]] = {  # field → (model, is_list)
    "outdoor.temperature_history": (HistoryPoint, True),
    "forecast.hourly": (HourlyPoint, True),
    "forecast.daily": (DailyForecast, True),
    "alerts.active": (Alert, True),
    "astro.sun": (SunInfo, False),
    "astro.moon": (MoonInfo, False),
}


def validate(field: str, value: Any) -> Any:
    """Validate a structured value against its shape (raises pydantic.ValidationError)."""
    model, is_list = FIELD_SHAPES[field]
    if is_list:
        return [model.model_validate(x) for x in value]
    return model.model_validate(value)
