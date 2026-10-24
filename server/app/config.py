"""Load and validate config/config.yaml (see config/config.example.yaml)."""
from __future__ import annotations

import os
import re
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator

from . import fields as field_registry

_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h|d)\s*$")
_UNIT_SECONDS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(v: Any) -> timedelta:
    """Accept '90s', '10m', '1.5h', '2d', a number of seconds, or a timedelta."""
    if isinstance(v, timedelta):
        return v
    if isinstance(v, (int, float)):
        return timedelta(seconds=v)
    m = _DURATION.match(str(v))
    if not m:
        raise ValueError(f"bad duration {v!r}; use e.g. 30s, 10m, 2h")
    return timedelta(seconds=float(m.group(1)) * _UNIT_SECONDS[m.group(2)])


Duration = Annotated[timedelta, BeforeValidator(parse_duration)]


class LocationCfg(BaseModel):
    name: str
    lat: float
    lon: float
    tz: str = "America/Chicago"


class NwsCfg(BaseModel):
    user_agent: str = "(InkplateWeather, https://github.com/aerogeek217/InkplateWeather)"
    station: str = "KCMI"


class ProviderCfg(BaseModel):
    """One provider instance. Type-specific options (host, url, token_env, …) are kept as extras."""

    model_config = ConfigDict(extra="allow")
    type: str
    ttl: Duration = timedelta(0)

    @property
    def options(self) -> dict[str, Any]:
        return dict(self.model_extra or {})


class SourceCfg(BaseModel):
    """One entry in a field's priority list."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)
    from_: str = Field(alias="from")
    key: str | None = None  # provider-native key; defaults to the field name
    entity: str | None = None  # alias for key, for Home Assistant entity ids (sensor.pms5003_aqi)
    max_age: Duration | None = None  # older readings are "stale"; None = never stale

    def model_post_init(self, _ctx: Any) -> None:
        if self.key is None and self.entity is not None:
            self.key = self.entity

    @property
    def options(self) -> dict[str, Any]:
        return dict(self.model_extra or {})


class ServerCfg(BaseModel):
    width: int = 1200
    height: int = 825
    refresh_minutes: int = 30  # Inkplate wakes at :00/:30
    fixture: str | None = None  # when set, /display.png renders this fixture instead of live data
    cache_dir: str = "cache"
    history_sample_minutes: float = 10  # background station sampler for the history provider; 0 disables


class AppConfig(BaseModel):
    location: LocationCfg
    nws: NwsCfg = NwsCfg()
    server: ServerCfg = ServerCfg()
    providers: dict[str, ProviderCfg] = {}
    fields: dict[str, list[SourceCfg]] = {}

    @field_validator("fields")
    @classmethod
    def _known_fields(cls, v: dict[str, list[SourceCfg]]) -> dict[str, list[SourceCfg]]:
        for name in v:
            if name not in field_registry.FIELDS:
                raise ValueError(f"unknown field {name!r}; add it to app/fields.py")
        return v

    def model_post_init(self, _ctx: Any) -> None:
        for name, sources in self.fields.items():
            for s in sources:
                if s.from_ not in self.providers:
                    raise ValueError(f"field {name!r} uses unknown provider {s.from_!r}")


DEFAULT_PATH = Path(os.environ.get("INKPLATE_CONFIG", "config/config.yaml"))


def load(path: str | Path | None = None) -> AppConfig:
    p = Path(path) if path else DEFAULT_PATH
    with open(p, encoding="utf-8") as f:
        return AppConfig.model_validate(yaml.safe_load(f) or {})
