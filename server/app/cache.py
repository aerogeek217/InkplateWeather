"""Per-provider TTL cache with last-good fallback, persisted under the cache dir.

Semantics (used by `collector.collect`):

- **TTL**: a provider's last successful result is reused while it is younger than the provider's `ttl`
  (`ttl: 0s` means fetch on every request).
- **Single flight**: at most one fetch per provider runs at a time; concurrent requests await the same fetch.
  The fetch runs as a background task, so a request that gives up after its time budget doesn't cancel it: a slow
  source still refreshes the cache for the next request (each fetch is capped at `FETCH_MAX_S`).
- **Last good**: if a fetch raises or times out, the last good result is served instead (its Readings keep their
  original `observed_at`, so the resolver marks them stale by `max_age`); without one, the exception is returned.
  Individual keys that come back with `status=error` are filled from the last good result the same way, and a result
  in which *every* reading is an error counts as a failed fetch. A `missing` key is taken at face value (the source
  answered and said it has no value), so a sensor that stops reporting isn't papered over.
- **Cap**: a last-good value is not served once its source last delivered it more than
  `max(LAST_GOOD_MAX_AGE, 2 × ttl)` ago, so the screen never shows days-old data as merely "stale".
- **Persistence**: last-good results are written to `<cache_dir>/providers/<name>.json` (atomic replace) and read
  back lazily after a restart; missing or corrupt files are ignored. Each file records a signature of the provider's
  config, so a changed config (or `/admin/reload`) never serves results fetched with the old settings.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import AppConfig
from .providers.base import Provider, RequestContext
from .readings import Reading, Status

log = logging.getLogger(__name__)

LAST_GOOD_MAX_AGE = timedelta(hours=24)
FETCH_MAX_S = 60.0  # hard cap on one background fetch, even when nobody waits for it any more
NO_PERSIST_TYPES = {"device", "fixture", "history"}  # request-specific or local data: memory only
FILE_VERSION = 1

ProviderResult = dict[str, Reading] | BaseException


def cache_dir(cfg: AppConfig) -> Path:
    """The writable cache directory: env INKPLATE_CACHE_DIR, else `server.cache_dir` from config."""
    return Path(os.environ.get("INKPLATE_CACHE_DIR") or cfg.server.cache_dir)


def config_signature(name: str, cfg: AppConfig) -> str:
    """Hash of everything that changes what a provider fetches (its options plus location and NWS settings).

    `ttl` is left out: it only changes how long a result is reused, not what the result is.
    """
    pcfg = cfg.providers[name]
    data = {"provider": pcfg.model_dump(mode="json", exclude={"ttl"}), "location": cfg.location.model_dump(),
            "nws": cfg.nws.model_dump()}
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[:16]


@dataclass
class Entry:
    fetched_at: datetime  # last successful fetch
    readings: dict[str, Reading]
    key_times: dict[str, datetime] = field(default_factory=dict)  # when each key last came back usable
    sig: str | None = None

    def key_time(self, key: str) -> datetime:
        return self.key_times.get(key, self.fetched_at)


@dataclass
class Health:
    """What /status shows per provider."""

    name: str
    type: str = ""
    ttl: timedelta = timedelta(0)
    last_fetch: datetime | None = None  # start of the last fetch attempt
    last_success: datetime | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None
    last_duration_s: float | None = None
    served: str | None = None  # how the last request was answered: live | cache | fallback | error
    served_at: datetime | None = None
    cached_at: datetime | None = None  # fetched_at of the result held in the cache
    fetching: bool = False


@dataclass
class _State:
    health: Health
    entry: Entry | None = None
    loaded: bool = False  # disk checked
    inflight: asyncio.Task | None = None


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name) or "_"


def _describe(e: BaseException) -> str:
    text = str(e).strip()
    return f"{type(e).__name__}: {text}" if text else type(e).__name__


class ProviderCache:
    def __init__(self, directory: Path | None = None, clock: Callable[[], datetime] | None = None):
        self.dir = Path(directory) / "providers" if directory is not None else None
        self._clock = clock or (lambda: datetime.now(UTC))
        self._states: dict[str, _State] = {}
        self._sigs: dict[str, str] = {}
        self._types: dict[str, str] = {}

    # ------------------------------------------------------------ config

    def configure(self, cfg: AppConfig) -> dict[str, list[str]]:
        """Adopt a (new) config: drop cached results of providers whose config changed or that were removed.

        Returns {"kept": [...], "reset": [...]} (provider names) for the reload report.
        """
        kept, reset = [], []
        new_sigs = {n: config_signature(n, cfg) for n in cfg.providers}
        for name in set(self._states) | set(self._sigs) | set(new_sigs):
            old, new = self._sigs.get(name), new_sigs.get(name)
            if new is None:
                self.forget(name)
                continue
            if old is not None and old != new:
                self.forget(name)
                reset.append(name)
            elif old is not None:
                kept.append(name)
        self._sigs = new_sigs
        self._types = {n: p.type for n, p in cfg.providers.items()}
        for name, pcfg in cfg.providers.items():
            h = self._state(name).health
            h.type, h.ttl = pcfg.type, pcfg.ttl
        return {"kept": sorted(kept), "reset": sorted(reset)}

    def forget(self, name: str) -> None:
        self._states.pop(name, None)  # a fetch still in flight for it finishes but isn't stored
        p = self._path(name)
        if p is not None:
            try:
                p.unlink(missing_ok=True)
            except OSError as e:
                log.warning("cache: could not remove %s: %s", p, e)

    # ------------------------------------------------------------ inspection

    def health(self) -> dict[str, Health]:
        return {n: s.health for n, s in self._states.items()}

    def entry(self, name: str) -> Entry | None:
        st = self._state(name)
        self._load(name, st)
        return st.entry

    # ------------------------------------------------------------ fetching

    async def get(self, p: Provider, ctx: RequestContext, timeout: float) -> ProviderResult:
        """The provider's readings: cached while fresh, else fetched (single flight), else last good."""
        name = p.name
        st = self._state(name)
        h = st.health
        ttl = _ttl(p)
        h.ttl = ttl
        self._load(name, st)
        now = self._clock()
        if st.entry is not None and ttl > timedelta(0) and now - st.entry.fetched_at < ttl:
            self._served(h, "cache", now)
            return dict(st.entry.readings)
        if st.inflight is None or st.inflight.done():
            st.inflight = asyncio.create_task(self._refresh(p, ctx, st), name=f"fetch:{name}")
        try:
            return await asyncio.wait_for(asyncio.shield(st.inflight), timeout)
        except TimeoutError:
            log.warning("provider %s: no answer within %.1f s; using its last good result if any", name, timeout)
            exc = TimeoutError(f"no answer within {timeout:.0f} s")
            h.last_error, h.last_error_at = _describe(exc), self._clock()
            return self._fallback(p, st, exc)

    async def _refresh(self, p: Provider, ctx: RequestContext, st: _State) -> ProviderResult:
        h = st.health
        start = self._clock()
        t0 = time.monotonic()
        h.last_fetch, h.fetching = start, True
        try:
            result: ProviderResult = await asyncio.wait_for(p.fetch(ctx), timeout=FETCH_MAX_S)
            if not isinstance(result, dict):
                raise TypeError(f"fetch() returned {type(result).__name__}, expected dict")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # recorded; the resolver sees the exception (or the last good result)
            log.warning("provider %s failed: %s", p.name, _describe(e))
            result = e
        finally:
            h.fetching = False
            h.last_duration_s = round(time.monotonic() - t0, 3)

        now = self._clock()
        if self._states.get(p.name) is not st:  # forgotten (config changed) while fetching: don't store
            return result
        if isinstance(result, BaseException):
            h.last_error, h.last_error_at = _describe(result), now
            return self._fallback(p, st, result)

        errors = [k for k, r in result.items() if r.status is Status.error]
        if result and len(errors) == len(result):
            first = result[errors[0]]
            h.last_error, h.last_error_at = f"every reading failed ({first.note or 'error'})", now
            return self._fallback(p, st, RuntimeError(h.last_error), partial=result)

        merged, key_times, substituted = dict(result), {}, False
        old = st.entry
        for k, r in result.items():
            if r.usable:
                key_times[k] = now
            elif r.status is Status.error and old is not None:
                prev = old.readings.get(k)
                if prev is not None and prev.usable and self._within_cap(p, now, old.key_time(k)):
                    merged[k], key_times[k], substituted = prev, old.key_time(k), True
        h.last_success = now
        if errors:
            h.last_error, h.last_error_at = f"{len(errors)} reading(s) failed: {', '.join(sorted(errors)[:5])}", now
        st.entry = Entry(fetched_at=now, readings=merged, key_times=key_times, sig=self._sigs.get(p.name))
        h.cached_at = now
        self._served(h, "fallback" if substituted else "live", now)
        self._save(p, st.entry)
        return dict(merged)

    def _fallback(self, p: Provider, st: _State, exc: BaseException,
                  partial: dict[str, Reading] | None = None) -> ProviderResult:
        now = self._clock()
        old = st.entry
        good: dict[str, Reading] = dict(partial or {})
        if old is not None:
            for k, r in old.readings.items():
                if r.usable and self._within_cap(p, now, old.key_time(k)):
                    good[k] = r
        if old is None or not any(r.usable for r in good.values()):
            self._served(st.health, "error", now)
            return exc
        self._served(st.health, "fallback", now)
        return good

    def _within_cap(self, p: Provider, now: datetime, t: datetime) -> bool:
        return now - t <= max(LAST_GOOD_MAX_AGE, 2 * _ttl(p))

    @staticmethod
    def _served(h: Health, how: str, now: datetime) -> None:
        h.served, h.served_at = how, now

    def _state(self, name: str) -> _State:
        st = self._states.get(name)
        if st is None:
            st = self._states[name] = _State(health=Health(name=name, type=self._types.get(name, "")))
        return st

    # ------------------------------------------------------------ persistence

    def _path(self, name: str) -> Path | None:
        return self.dir / f"{_safe_name(name)}.json" if self.dir is not None else None

    def _persist(self, p: Provider) -> bool:
        return (self.dir is not None and self._types.get(p.name) not in NO_PERSIST_TYPES
                and getattr(p, "cache_persist", True))

    def _save(self, p: Provider, entry: Entry) -> None:
        if not self._persist(p):
            return
        path = self._path(p.name)
        try:
            data = {
                "version": FILE_VERSION,
                "name": p.name,
                "type": self._types.get(p.name, ""),
                "sig": entry.sig,
                "fetched_at": entry.fetched_at.isoformat(),
                "key_times": {k: t.isoformat() for k, t in entry.key_times.items()},
                "readings": {k: r.model_dump(mode="json") for k, r in entry.readings.items()},
            }
            text = json.dumps(data, separators=(",", ":"))
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(path)
        except Exception as e:  # never fail a request over the cache file
            log.warning("cache: could not save %s: %s", path, _describe(e))

    def _load(self, name: str, st: _State) -> None:
        if st.loaded:
            return
        st.loaded = True
        path = self._path(name)
        if path is None or st.entry is not None or not path.is_file():
            return
        try:
            data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
            if data.get("version") != FILE_VERSION or data.get("name") != name:
                return
            sig = self._sigs.get(name)
            if sig is not None and data.get("sig") != sig:
                log.info("cache: %s was fetched with a different config; ignoring it", name)
                return
            fetched_at = _aware(datetime.fromisoformat(data["fetched_at"]))
            readings = {k: Reading.model_validate(v) for k, v in data["readings"].items()}
            key_times = {k: _aware(datetime.fromisoformat(v)) for k, v in (data.get("key_times") or {}).items()}
        except Exception as e:
            log.warning("cache: ignoring unreadable %s: %s", path, _describe(e))
            return
        st.entry = Entry(fetched_at=fetched_at, readings=readings, key_times=key_times, sig=data.get("sig"))
        st.health.cached_at = fetched_at


def _ttl(p: Provider) -> timedelta:
    ttl = getattr(p, "ttl", None)
    return ttl if isinstance(ttl, timedelta) else timedelta(0)


def _aware(t: datetime) -> datetime:
    return t if t.tzinfo is not None else t.replace(tzinfo=UTC)
