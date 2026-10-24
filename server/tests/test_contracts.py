"""Phase-2 contracts: structured shapes (fixtures must satisfy them) and the condition mapping."""
import json

import pytest

from app import conditions, shapes
from app.paths import FIXTURES_DIR, ICONS_DIR


@pytest.mark.parametrize("name", ["fall", "winter"])
def test_fixtures_match_shapes(name):
    readings = json.loads((FIXTURES_DIR / f"{name}.json").read_text(encoding="utf-8"))["readings"]
    for field in shapes.FIELD_SHAPES:
        raw = readings[field]
        value = raw["value"] if isinstance(raw, dict) and "value" in raw and "observed_at" in raw else raw
        shapes.validate(field, value)


def test_every_condition_has_an_icon():
    for name in conditions.CONDITIONS:
        assert (ICONS_DIR / "conditions" / f"{name}.svg").is_file(), name
    assert set(conditions.NWS_CODES.values()) <= set(conditions.CONDITIONS)
    assert set(conditions.WMO_CODES.values()) <= set(conditions.CONDITIONS)
    assert set(conditions.SEVERITY) == set(conditions.CONDITIONS)


@pytest.mark.parametrize("url,text,expected", [
    ("https://api.weather.gov/icons/land/day/skc?size=medium", "Sunny", ("sunny", False)),
    ("https://api.weather.gov/icons/land/night/sct?size=medium", "Partly Cloudy", ("partly-cloudy-night", False)),
    ("https://api.weather.gov/icons/land/day/rain,20/tsra,60?size=medium", "Showers And Thunderstorms Likely",
     ("thunderstorm", False)),
    ("https://api.weather.gov/icons/land/day/wind_bkn?size=medium", "Mostly Cloudy", ("cloudy", True)),
    ("https://api.weather.gov/icons/land/day/snow,90?size=medium", "Heavy Snow", ("heavy-snow", False)),
    ("https://api.weather.gov/icons/land/day/rain_snow,70?size=medium", "Rain And Snow", ("wintry-mix", False)),
    ("https://api.weather.gov/icons/land/night/ovc?size=medium", "Patchy Drizzle", ("drizzle", False)),
    ("https://api.weather.gov/icons/land/day/sct?size=medium", "Sunny and Breezy", ("partly-cloudy", True)),
    (None, None, ("cloudy", False)),
])
def test_from_nws(url, text, expected):
    c = conditions.from_nws(url, text)
    assert (c.name, c.windy) == expected


@pytest.mark.parametrize("code,day,expected", [(0, True, "sunny"), (0, False, "clear-night"), (2, False,
                         "partly-cloudy-night"), (65, True, "heavy-rain"), (75, True, "heavy-snow"),
                         (66, True, "freezing-rain"), (95, True, "thunderstorm"), (None, True, "cloudy")])
def test_from_wmo(code, day, expected):
    assert conditions.from_wmo(code, day).name == expected


def test_precip_type():
    assert [conditions.precip_type(n) for n in ("snow", "wintry-mix", "thunderstorm", "flurries")] == [
        "snow", "ice", "rain", "snow"]
