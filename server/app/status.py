"""Data for /status and /status.json: per-field provenance, provider health, history coverage.

Everything here is JSON-friendly (ISO times, seconds, plain strings). Secrets in provider options (tokens,
passwords, `*_env` names, credentials in URLs) are redacted.
"""
from __future__ import annotations

import os
import re
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from . import fields as field_registry
from . import messages
from .config import AppConfig
from .paths import REPO_DIR, SERVER_DIR
from .resolver import Resolved

REDACTED = "(hidden)"
_SECRET_KEY = re.compile(r"(token|password|passwd|secret|api_?key|credential|auth)", re.I)
COMMIT_FILE = "DEPLOYED_COMMIT"


# ------------------------------------------------------------------ helpers


def age_text(seconds: float | None) -> str:
    """'45 s', '12 min', '3 h 5 min', '2 d 4 h'; 'in 5 min' for future times; '' when unknown."""
    if seconds is None:
        return ""
    if seconds < 0:
        return "in " + age_text(-seconds)
    s = int(round(seconds))
    if s < 90:
        return f"{s} s"
    m = s // 60
    if m < 90:
        return f"{m} min"
    h, m = divmod(m, 60)
    if h < 48:
        return f"{h} h {m} min" if m else f"{h} h"
    d, h = divmod(h, 24)
    return f"{d} d {h} h" if h else f"{d} d"


def _iso(t: datetime | None) -> str | None:
    return t.isoformat() if t is not None else None


def _secs(td: timedelta | None) -> float | None:
    return round(td.total_seconds(), 1) if td is not None else None


def _since(now: datetime, t: datetime | None) -> float | None:
    return round((now - t).total_seconds(), 1) if t is not None else None


def redact(v: Any, key: str = "") -> Any:
    if key and (_SECRET_KEY.search(key) or key.endswith("_env")):
        return REDACTED
    if isinstance(v, Mapping):
        return {k: redact(x, str(k)) for k, x in v.items()}
    if isinstance(v, list):
        return [redact(x) for x in v]
    if isinstance(v, str) and "://" in v and "@" in v:
        try:
            u = urlsplit(v)
            if u.username or u.password:
                host = u.hostname or ""
                netloc = f"{REDACTED}@{host}" + (f":{u.port}" if u.port else "")
                return urlunsplit((u.scheme, netloc, u.path, u.query, u.fragment))
        except ValueError:
            return REDACTED
    return v


def summarize(value: Any) -> Any:
    """Scalars as they are; structured values as a short description (full values stay out of /status)."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= 200 else value[:197] + "…"
    if isinstance(value, list):
        return f"list of {len(value)}"
    if isinstance(value, Mapping):
        keys = list(value)
        more = f", +{len(keys) - 6}" if len(keys) > 6 else ""
        return "{" + ", ".join(str(k) for k in keys[:6]) + more + "}"
    return type(value).__name__


def display(value: Any, unit: str | None) -> str:
    v = summarize(value)
    if v is None:
        return ""
    if isinstance(v, float):
        v = f"{v:.3f}".rstrip("0").rstrip(".")
    return f"{v} {unit}" if unit and not isinstance(value, (list, Mapping)) else str(v)


def deployed_commit() -> str:
    """Commit hash from DEPLOYED_COMMIT (env var, or a file next to the app or in the repo root), else 'unknown'."""
    env = os.environ.get(COMMIT_FILE, "").strip()
    if env:
        return env[:64]
    for d in (SERVER_DIR, REPO_DIR, Path.cwd()):
        p = d / COMMIT_FILE
        try:
            text = p.read_text(encoding="utf-8").strip().splitlines()
        except OSError:
            continue
        if text and text[0].strip():
            return text[0].strip()[:64]
    return "unknown"


# ------------------------------------------------------------------ sections


def field_entry(r: Resolved, now: datetime, tz: str, types: dict[str, str]) -> dict:
    rd = r.reading
    f = field_registry.FIELDS.get(r.field)
    unit = rd.unit or (f.unit if f else None)
    return {
        "field": r.field,
        "label": f.label if f else r.field,
        "status": str(rd.status),
        "value": summarize(rd.value),
        "display": display(rd.value, unit) if rd.usable else "",
        "unit": unit,
        "source": rd.source or None,
        "source_type": types.get(rd.source) if rd.source else None,
        "observed_at": _iso(rd.observed_at),
        "age_s": _since(now, rd.observed_at),
        "age": age_text(_since(now, rd.observed_at)),
        "fallback_used": r.fallback_used and rd.usable,
        "note": rd.note,
        "message": messages.explain(r, tz, types),
        "as_of": messages.as_of(r, tz),
        "attempts": [{
            "source": a.source,
            "key": a.key,
            "outcome": str(a.outcome),
            "note": a.note,
            "age_s": _secs(a.age),
            "age": age_text(_secs(a.age)),
            "last_seen": _iso(a.last_seen),
        } for a in r.tried],
    }


def provider_entries(state, cfg: AppConfig, now: datetime, resolved: Mapping[str, Resolved],
                     fixture: str | None) -> list[dict]:
    used_by: dict[str, list[str]] = {}
    for name, r in resolved.items():
        for a in r.tried:
            used_by.setdefault(a.source, [])
            if name not in used_by[a.source]:
                used_by[a.source].append(name)
    health = state.cache.health() if getattr(state, "cache", None) is not None else {}
    build_errors = getattr(state, "provider_errors", None) or {}
    out = []
    names = list(cfg.providers)
    if fixture and "fixture" not in names:
        names.insert(0, "fixture")
    for name in names:
        pcfg = cfg.providers.get(name)
        h = health.get(name)
        cached_at = h.cached_at if h else None
        out.append({
            "name": name,
            "type": pcfg.type if pcfg else "fixture",
            "ttl_s": _secs(pcfg.ttl) if pcfg else None,
            "ttl": age_text(_secs(pcfg.ttl)) if pcfg and pcfg.ttl else "0 (every request)",
            "options": redact(pcfg.options) if pcfg else {"name": fixture},
            "used_by": used_by.get(name, []),
            "build_error": build_errors.get(name),
            "served": h.served if h else None,
            "served_at": _iso(h.served_at) if h else None,
            "from_cache": bool(h and h.served in ("cache", "fallback")),
            "last_fetch": _iso(h.last_fetch) if h else None,
            "last_fetch_age": age_text(_since(now, h.last_fetch)) if h and h.last_fetch else "",
            "last_success": _iso(h.last_success) if h else None,
            "last_success_age": age_text(_since(now, h.last_success)) if h and h.last_success else "",
            "last_error": h.last_error if h else None,
            "last_error_at": _iso(h.last_error_at) if h else None,
            "last_error_age": age_text(_since(now, h.last_error_at)) if h and h.last_error_at else "",
            "last_duration_s": h.last_duration_s if h else None,
            "cached_at": _iso(cached_at),
            "cache_age": age_text(_since(now, cached_at)) if cached_at else "",
            "fetching": bool(h and h.fetching),
        })
    return out


def history_entry(state, now: datetime) -> dict:
    store = getattr(state, "history", None)
    if store is None:
        return {"series": {}, "chart": None, "trend": None}
    series = store.summary()
    for s in series.values():
        last = datetime.fromisoformat(s["last"]) if s["last"] else None
        s["last_age"] = age_text(_since(now, last)) if last else ""
    pts, why = store.chart(now)
    change, _latest, twhy = store.pressure_trend(now)
    return {
        "file": str(store.path) if store.path else None,
        "series": series,
        "chart": {"ok": not why, "points": len(pts), "note": why},
        "trend": {"ok": not twhy, "value": change, "note": twhy},
    }


def build(state, cfg: AppConfig, now: datetime, resolved: Mapping[str, Resolved],
          real_now: datetime | None = None) -> dict:
    """`now` is the resolve time (a fixture's pinned time in fixture mode); provider and history ages use the
    wall clock (`real_now`)."""
    fixture = cfg.server.fixture
    tz = cfg.location.tz
    real_now = real_now or datetime.now(ZoneInfo(tz))
    types = {n: p.type for n, p in cfg.providers.items()}
    if fixture:
        types.setdefault("fixture", "fixture")
    entries = [field_entry(r, now, tz, types) for r in resolved.values()]
    counts: dict[str, int] = {}
    for e in entries:
        counts[e["status"]] = counts.get(e["status"], 0) + 1
    last = getattr(state, "last_request", None)
    loaded = getattr(state, "config_loaded_at", None)
    path = getattr(state, "config_path", None)
    sampler = getattr(state, "sampler", None)
    return {
        "generated_at": real_now.isoformat(),
        "resolved_at": now.isoformat(),
        "mode": "fixture" if fixture else "live",
        "fixture": fixture,
        "deployed_commit": deployed_commit(),
        "location": cfg.location.name,
        "timezone": tz,
        "config": {"path": str(path) if path else None, "loaded_at": _iso(loaded),
                   "is_example": bool(path and Path(path).name == "config.example.yaml")},
        "cache_dir": str(getattr(state, "cache_dir", "") or "") or None,
        "history_sampler": bool(sampler is not None and not sampler.done()),
        "last_display_request": None if not last else {
            "at": last["at"].isoformat(),
            "age": age_text(_since(real_now, last["at"])),
            "battery_pct": last["device"].battery_pct,
            "voltage_mv": last["device"].voltage_mv,
            "wake_reason": last["device"].wake_reason,
        },
        "counts": counts,
        "fields": entries,
        "providers": provider_entries(state, cfg, real_now, resolved, fixture),
        "history": history_entry(state, real_now),
    }
