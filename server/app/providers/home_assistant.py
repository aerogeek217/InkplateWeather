"""`home_assistant` provider: any Home Assistant entity, read from HA's REST API (`GET <url>/api/states`).

Config:

    providers:
      ha: {type: home_assistant, url: "http://homeassistant.local:8123", token_env: HA_TOKEN, ttl: 60s}
    fields:
      air.aqi: [{from: ha, key: sensor.pms5003_aqi, max_age: 20m}, {from: om, max_age: 3h}]

Readings are keyed by **entity id**, so a field's source must name the entity with `key:`
(`{from: ha, key: sensor.xyz}`). The resolver looks up `key` (or the field name); an `entity:` option is
kept as an extra by `SourceCfg` but is not used for the lookup.

`token_env` names the environment variable holding a long-lived access token (default `HA_TOKEN`); the token
itself never goes in the config file. One request per fetch returns every entity.

Each entity becomes a Reading:
- value: the state parsed as a number when numeric, else the state string (`"on"`, `"Clear"`);
- unit: `attributes.unit_of_measurement`, converted to the canonical units of `app/fields.py` when it is a
  temperature (°C → °F), pressure (hPa/mbar/kPa/mmHg → inHg), speed (km/h, m/s, kn → mph) or precipitation
  (mm → in, mm/h → in/h); other units (µg/m³, AQI, %) pass through unchanged;
- observed_at: `last_reported` (HA ≥ 2024.3: the sensor's last report, even when the value didn't change), else
  `last_updated`;
- `unavailable` / `unknown` states: a missing Reading with that note, observed_at = `last_changed` (when it
  stopped reporting), so the screen can say "… hasn't reported since 1:12 PM".

A missing token or url, a network failure, a non-200 answer (e.g. 401 for a bad token) or bad JSON raises, and
the resolver records it as an error for every field using this provider.
"""
from __future__ import annotations

import math
import os
from datetime import datetime, timedelta
from typing import Any

import httpx

from ..config import AppConfig, ProviderCfg, parse_duration
from ..readings import Reading, Status
from .base import RequestContext, register
from .ecowitt_local import to_canonical

DEFAULT_TIMEOUT_S = 5.0
DEFAULT_TOKEN_ENV = "HA_TOKEN"
NO_VALUE_STATES = {"unavailable", "unknown", "none", ""}
# Only these quantities are converted; lux, µg/m³, %, ° etc. pass through with HA's own unit.
CONVERTED = {"°F", "inHg", "mph", "in", "in/h"}


class HomeAssistantConfigError(RuntimeError):
    """The provider is missing its url or token."""


def _time(v: Any) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        t = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo is not None else None


def _number(state: str) -> float | None:
    try:
        v = float(state)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def parse_state(entity: dict[str, Any], source: str = "ha") -> Reading:
    """One `/api/states` entry → Reading in canonical units where the unit is known."""
    state = entity.get("state")
    attrs = entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}
    unit = attrs.get("unit_of_measurement")
    unit = unit if isinstance(unit, str) and unit.strip() else None
    if state is None or str(state).strip().lower() in NO_VALUE_STATES:
        note = str(state).strip().lower() if state else "no_value"
        return Reading(source=source, status=Status.missing, note=note,
                       observed_at=_time(entity.get("last_changed")))
    observed_at = _time(entity.get("last_reported")) or _time(entity.get("last_updated"))
    number = _number(str(state))
    if number is None:
        return Reading(value=str(state), unit=unit, observed_at=observed_at, source=source)
    conv = to_canonical(number, unit) if unit else None
    if conv is None or conv[1] not in CONVERTED:
        return Reading(value=number, unit=unit, observed_at=observed_at, source=source)
    value, canonical = conv
    return Reading(value=value, unit=canonical, observed_at=observed_at, source=source)


def parse_states(data: Any, source: str = "ha") -> dict[str, Reading]:
    """The whole `/api/states` list → Readings keyed by entity id."""
    if not isinstance(data, list):
        raise ValueError(f"unexpected Home Assistant response: expected a list, got {type(data).__name__}")
    out: dict[str, Reading] = {}
    for e in data:
        if isinstance(e, dict) and isinstance(e.get("entity_id"), str):
            out[e["entity_id"]] = parse_state(e, source)
    return out


@register("home_assistant")
class HomeAssistantProvider:
    """Config options: `url` (HA base URL), `token_env` (default HA_TOKEN), `timeout` (default 5s)."""

    def __init__(self, name: str, cfg: ProviderCfg, app: AppConfig | None = None):
        self.name = name
        self.ttl = cfg.ttl if cfg else timedelta(0)
        opts = cfg.options if cfg else {}
        self.url = str(opts.get("url") or "").strip().rstrip("/")
        self.token_env = str(opts.get("token_env") or DEFAULT_TOKEN_ENV)
        t = opts.get("timeout")
        self.timeout = parse_duration(t).total_seconds() if t is not None else DEFAULT_TIMEOUT_S

    def _token(self) -> str:
        token = os.environ.get(self.token_env, "").strip()
        if not token:
            raise HomeAssistantConfigError(
                f"provider {self.name!r} (home_assistant): environment variable {self.token_env} is not set; "
                "put a Home Assistant long-lived access token in it")
        return token

    async def fetch(self, ctx: RequestContext) -> dict[str, Reading]:
        if not self.url:
            raise HomeAssistantConfigError(f"provider {self.name!r} (home_assistant): no `url` configured")
        headers = {"Authorization": f"Bearer {self._token()}", "Accept": "application/json"}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(f"{self.url}/api/states", headers=headers)
        resp.raise_for_status()
        return parse_states(resp.json(), source=self.name)
