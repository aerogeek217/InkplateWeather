"""Provider protocol. Contract shared by all workstreams; change only with the orchestrator."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from ..config import AppConfig, ProviderCfg
from ..readings import Reading


@dataclass(frozen=True)
class DeviceInfo:
    """What the Inkplate reports in its request (`/display.png?batt=&mv=&reason=`)."""

    battery_pct: int | None = None  # 0–100
    voltage_mv: int | None = None
    wake_reason: str | None = None  # timer | button | retry | boot


@dataclass(frozen=True)
class RequestContext:
    now: datetime  # timezone-aware; fixtures pin this to their own time
    device: DeviceInfo = field(default_factory=DeviceInfo)
    config: AppConfig | None = None


@runtime_checkable
class Provider(Protocol):
    name: str  # instance name from config, e.g. "ecowitt"
    ttl: timedelta  # results younger than this are served from cache

    async def fetch(self, ctx: RequestContext) -> dict[str, Reading]:
        """Return readings keyed by provider-native keys (config `key:` refers to these).

        Raise on total failure; the resolver records it as an error for every field using this provider.
        Return a Reading with status=missing for individual keys the source didn't report.
        """
        ...


ProviderFactory = type  # a class taking (name: str, cfg: ProviderCfg, app: AppConfig)

_REGISTRY: dict[str, ProviderFactory] = {}


def register(type_name: str):
    """Class decorator: `@register("ecowitt_local")`."""

    def deco(cls):
        _REGISTRY[type_name] = cls
        return cls

    return deco


def build(name: str, cfg: ProviderCfg, app: AppConfig) -> Provider:
    try:
        cls = _REGISTRY[cfg.type]
    except KeyError:
        raise ValueError(f"provider {name!r}: unknown type {cfg.type!r}") from None
    return cls(name, cfg, app)
