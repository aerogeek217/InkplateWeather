"""Config-driven source selection with fallbacks and provenance.

For each field, sources are tried in priority order. The first fresh reading wins. If none is fresh
but some are merely stale, the first stale one is used (status=stale). Otherwise the field resolves to
a missing Reading whose note explains why. Every source tried is recorded for /status and messages.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta

from pydantic import BaseModel

from .config import SourceCfg
from .readings import Reading, Status

# What a provider produced for one request: its readings by native key, or the exception it raised.
ProviderResult = Mapping[str, Reading] | BaseException


class Attempt(BaseModel):
    source: str
    key: str
    outcome: Status  # ok = used; stale / missing / error = why it was passed over (or used as stale)
    note: str | None = None
    age: timedelta | None = None
    last_seen: datetime | None = None  # observed_at of the source's reading, even when rejected


class Resolved(BaseModel):
    field: str
    reading: Reading  # winner, or a missing Reading if nothing qualified
    tried: list[Attempt]

    @property
    def fallback_used(self) -> bool:
        """True when the winner isn't the first configured source."""
        return bool(self.tried) and self.reading.source != self.tried[0].source


def _age(r: Reading, now: datetime) -> timedelta | None:
    return now - r.observed_at if r.observed_at else None


def resolve_field(
    field: str, sources: list[SourceCfg], results: Mapping[str, ProviderResult], now: datetime
) -> Resolved:
    tried: list[Attempt] = []
    stale: Reading | None = None
    for src in sources:
        key = src.key or field
        res = results.get(src.from_)
        if res is None:
            tried.append(Attempt(source=src.from_, key=key, outcome=Status.missing, note="not_fetched"))
            continue
        if isinstance(res, BaseException):
            note = f"provider_error:{type(res).__name__}"
            tried.append(Attempt(source=src.from_, key=key, outcome=Status.error, note=note))
            continue
        r = res.get(key)
        if r is None:
            tried.append(Attempt(source=src.from_, key=key, outcome=Status.missing, note="no_such_key"))
            continue
        if not r.usable:
            outcome = r.status if r.status in (Status.missing, Status.error) else Status.missing
            tried.append(Attempt(source=src.from_, key=key, outcome=outcome, note=r.note or "no_value",
                                 age=_age(r, now), last_seen=r.observed_at))
            continue
        age = _age(r, now)
        if r.status is Status.stale or (src.max_age is not None and age is not None and age > src.max_age):
            tried.append(Attempt(source=src.from_, key=key, outcome=Status.stale, note="too_old", age=age,
                                 last_seen=r.observed_at))
            if stale is None:
                stale = r.model_copy(update={"source": r.source or src.from_, "status": Status.stale})
            continue
        tried.append(Attempt(source=src.from_, key=key, outcome=Status.ok, age=age, last_seen=r.observed_at))
        return Resolved(field=field, reading=r.model_copy(update={"source": r.source or src.from_}), tried=tried)

    if stale is not None:
        return Resolved(field=field, reading=stale, tried=tried)
    note = tried[0].note if tried else "no_sources_configured"
    return Resolved(field=field, reading=Reading(status=Status.missing, note=note), tried=tried)


def resolve(
    fields_cfg: Mapping[str, list[SourceCfg]], provider_results: Mapping[str, ProviderResult], now: datetime
) -> dict[str, Resolved]:
    return {f: resolve_field(f, srcs, provider_results, now) for f, srcs in fields_cfg.items()}
