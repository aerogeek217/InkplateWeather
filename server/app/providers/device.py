"""`device` provider: the Inkplate's own battery and wake reason, from its `/display.png?batt=&mv=&reason=` request.

Config: `device: {type: device}`. Keys are the field names (`device.battery_pct` in %, `device.voltage_mv` in mV,
`device.wake_reason` as text), so fields need no `key:`. A value the request didn't include is a missing Reading
with note `not_in_request`. observed_at is the request time (`ctx.now`).
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from ..config import AppConfig, ProviderCfg
from ..readings import Reading, Status
from .base import RequestContext, register

_KEYS: tuple[tuple[str, str, str | None], ...] = (
    ("device.battery_pct", "battery_pct", "%"),
    ("device.voltage_mv", "voltage_mv", "mV"),
    ("device.wake_reason", "wake_reason", None),
)


@register("device")
class DeviceProvider:
    def __init__(self, name: str, cfg: ProviderCfg | None = None, app: AppConfig | None = None):
        self.name = name
        self.ttl = timedelta(0)  # always from the current request; never cached

    async def fetch(self, ctx: RequestContext) -> dict[str, Reading]:
        out: dict[str, Reading] = {}
        for key, attr, unit in _KEYS:
            value: Any = getattr(ctx.device, attr, None)
            if value is None:
                out[key] = Reading(source=self.name, status=Status.missing, note="not_in_request")
            else:
                out[key] = Reading(value=value, unit=unit, observed_at=ctx.now, source=self.name)
        return out
