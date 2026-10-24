"""The value every provider produces and the resolver passes on.

Contract shared by all workstreams; change only with the orchestrator.
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel


class Status(StrEnum):
    ok = "ok"
    stale = "stale"  # older than the field's max_age, but still shown (gray + "as of")
    missing = "missing"  # no value: the slot shows a plain-language sentence instead
    error = "error"  # the source failed; treated like missing by the template


class Reading(BaseModel):
    value: Any = None  # canonical unit (°F, %, mph, inHg, in, deg, AQI, …) or structured (forecast lists)
    unit: str | None = None
    observed_at: datetime | None = None  # timezone-aware
    source: str = ""  # provider instance name from config, e.g. "ecowitt"
    status: Status = Status.ok
    note: str | None = None  # machine-friendly reason code; messages.py turns it into plain language

    @property
    def usable(self) -> bool:
        return self.status in (Status.ok, Status.stale) and self.value is not None


def missing(source: str = "", note: str | None = None) -> Reading:
    return Reading(source=source, status=Status.missing, note=note)


def error(source: str, note: str) -> Reading:
    return Reading(source=source, status=Status.error, note=note)
