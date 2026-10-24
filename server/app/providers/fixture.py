"""`fixture` provider: serves canned readings from server/fixtures/<name>.json (phase 1).

Fixture file layout:

    {
      "now": "2026-10-24T14:30:00-05:00",        # pinned render time (timezone-aware ISO)
      "device": {"battery_pct": 78, ...},          # defaults for the device fields
      "readings": {
        "outdoor.temperature": 58.4,               # bare value: observed_at = now, status ok
        "radar.frame": {"value": {...}, "observed_at": "...", "unit": null, "status": "ok"}
      }
    }

A reading given as an object with a "value" key is taken as a full Reading; anything else is the value.
Readings are keyed by field name, so config `key:` defaults work. `radar.frame` may name an
`artwork` file (relative to the fixtures dir) whose SVG layers are loaded into `svg_layers`.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..config import AppConfig, ProviderCfg
from ..paths import FIXTURES_DIR
from ..readings import Reading, Status
from .base import DeviceInfo, RequestContext, register


class FixtureError(ValueError):
    pass


def fixture_path(name: str, base: Path = FIXTURES_DIR) -> Path:
    if not name or any(c in name for c in "/\\.") or name.startswith("_"):
        raise FixtureError(f"bad fixture name {name!r}")
    p = base / f"{name}.json"
    if not p.is_file():
        raise FixtureError(f"no fixture named {name!r}")
    return p


def available(base: Path = FIXTURES_DIR) -> list[str]:
    return sorted(p.stem for p in base.glob("*.json") if "." not in p.stem)


@lru_cache(maxsize=16)
def _load_raw(path: str, mtime: float) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load(name: str, base: Path = FIXTURES_DIR) -> dict[str, Any]:
    p = fixture_path(name, base)
    return _load_raw(str(p), p.stat().st_mtime)


def fixture_now(name: str, base: Path = FIXTURES_DIR) -> datetime:
    now = datetime.fromisoformat(load(name, base)["now"])
    if now.tzinfo is None:
        raise FixtureError(f"fixture {name!r}: 'now' must include a UTC offset")
    return now


def fixture_device(name: str, base: Path = FIXTURES_DIR) -> DeviceInfo:
    d = load(name, base).get("device", {})
    return DeviceInfo(battery_pct=d.get("battery_pct"), voltage_mv=d.get("voltage_mv"),
                      wake_reason=d.get("wake_reason"))


def _artwork(value: dict[str, Any], base: Path) -> dict[str, Any]:
    art = value.get("artwork")
    if not art:
        return value
    p = (base / art).resolve()
    if base.resolve() not in p.parents:
        raise FixtureError(f"artwork path escapes the fixtures dir: {art!r}")
    text = p.read_text(encoding="utf-8")
    return {**value, "svg_layers": text}


def readings_for(name: str, base: Path = FIXTURES_DIR, source: str = "fixture") -> dict[str, Reading]:
    data = load(name, base)
    now = fixture_now(name, base)
    out: dict[str, Reading] = {}
    for key, raw in data.get("readings", {}).items():
        if isinstance(raw, dict) and "value" in raw:
            r = Reading.model_validate({"observed_at": now, "source": source, **raw})
            if "age_minutes" in raw:  # convenience for stale/outage fixtures
                r = r.model_copy(update={"observed_at": now - timedelta(minutes=raw["age_minutes"])})
        else:
            r = Reading(value=raw, observed_at=now, source=source)
        if key == "radar.frame" and isinstance(r.value, dict):
            r = r.model_copy(update={"value": _artwork(r.value, base)})
        if r.value is None and r.status is Status.ok:
            r = r.model_copy(update={"status": Status.missing, "note": r.note or "no_value"})
        out[key] = r
    return out


@register("fixture")
class FixtureProvider:
    """Config: `fixture: {type: fixture, name: fall}`; `name` defaults to `fall`."""

    def __init__(self, name: str, cfg: ProviderCfg, app: AppConfig | None = None,
                 fixture: str | None = None, base: Path = FIXTURES_DIR):
        self.name = name
        self.ttl = cfg.ttl if cfg else timedelta(0)
        opts = cfg.options if cfg else {}
        self.fixture = fixture or opts.get("name") or "fall"
        self.base = Path(opts.get("dir", base))

    async def fetch(self, ctx: RequestContext) -> dict[str, Reading]:
        return readings_for(self.fixture, self.base, source=self.name)
