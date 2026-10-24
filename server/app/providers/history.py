"""`history` provider: the station's own recent readings, recorded by the server (see app/history.py).

Config: `history: {type: history, ttl: 0s}`. Keys (= field names, so no `key:` is needed):

- `outdoor.temperature_history`: list of shapes.HistoryPoint for the last 24 h, oldest first, ≤ 1 point per 10 min,
  only when coverage is adequate (≥ 20 h span, no gap > 2 h); otherwise missing with note `insufficient_history`
  so the field falls back to NWS observations. `observed_at` is the newest point's time.
- `pressure.trend_3h`: inHg change of sea-level pressure versus ~3 h earlier (±30 min); missing when no point is
  near 3 h before the latest one.

Optional option `sample_minutes` (default 10; 0 disables): how often the server's background sampler records the
station fields between Inkplate requests (used until `server.history_sample_minutes` exists in config.py).
"""
from __future__ import annotations

from datetime import timedelta

from .. import shapes
from ..cache import cache_dir
from ..config import AppConfig, ProviderCfg
from ..history import HistoryStore, get_store
from ..readings import Reading, missing
from .base import RequestContext, register

HISTORY_KEY = "outdoor.temperature_history"
TREND_KEY = "pressure.trend_3h"


@register("history")
class HistoryProvider:
    def __init__(self, name: str, cfg: ProviderCfg, app: AppConfig | None = None,
                 store: HistoryStore | None = None):
        self.name = name
        self.ttl = cfg.ttl if cfg else timedelta(0)
        if store is None:
            if app is None:
                raise ValueError(f"provider {name!r}: history needs the app config (for the cache dir)")
            store = get_store(cache_dir(app))
        self.store = store

    async def fetch(self, ctx: RequestContext) -> dict[str, Reading]:
        now = ctx.now
        out: dict[str, Reading] = {}

        pts, why = self.store.chart(now)
        if why:
            out[HISTORY_KEY] = missing(self.name, why)
        else:
            value = shapes.dump([shapes.HistoryPoint(t=p.t.astimezone(now.tzinfo), v=round(p.v, 1)) for p in pts])
            out[HISTORY_KEY] = Reading(value=value, unit="°F", observed_at=pts[-1].t.astimezone(now.tzinfo),
                                       source=self.name)

        change, latest, why = self.store.pressure_trend(now)
        if why or change is None or latest is None:
            out[TREND_KEY] = missing(self.name, why or "insufficient_history")
        else:
            out[TREND_KEY] = Reading(value=round(change, 3), unit="inHg", observed_at=latest.astimezone(now.tzinfo),
                                     source=self.name)
        return out
