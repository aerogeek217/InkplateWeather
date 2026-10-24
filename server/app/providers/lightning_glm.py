"""`lightning_glm` provider: GOES-East GLM flashes in the radar extent, last 30 minutes → `lightning.flashes`.

The work happens in a background poller (`app.lightning`), started by the server once the provider is built and
woken by the radar: it only downloads GLM files while the radar shows echoes in view. `fetch` just reads the
poller's rolling list, so it never waits on the network.

Value: [{"t": iso UTC, "lat", "lon"}, …] (oldest first). Config: `glm: {type: lightning_glm}`; optional `always`
(poll even without echoes, default false), `bucket`, `timeout`.
"""
from __future__ import annotations

from datetime import datetime

from .. import lightning, radarmap
from ..cache import cache_dir
from ..config import AppConfig, ProviderCfg
from ..readings import Reading
from .base import RequestContext, register

MARGIN = 10.0  # map units: bolts just outside the edge still merge with ones inside


@register("lightning_glm")
class GlmProvider:
    def __init__(self, name: str, cfg: ProviderCfg, app: AppConfig):
        self.name = name
        self.ttl = cfg.ttl
        opts = cfg.options
        store = lightning.Store(cache_dir(app) / "lightning.json")
        self.poller = lightning.Poller(store, radarmap.extent().bbox(MARGIN), app.nws.user_agent,
                                       bucket=str(opts.get("bucket", lightning.BUCKET)),
                                       timeout=float(opts.get("timeout", 15)), always=bool(opts.get("always", False)))

    def start(self) -> None:
        self.poller.start()

    def stop(self) -> None:
        self.poller.stop()

    def note_radar(self, frame: dict | None, now: datetime) -> None:
        self.poller.note_radar(frame, now)

    async def fetch(self, ctx: RequestContext) -> dict[str, Reading]:
        store = self.poller.store
        polling = self.poller.active(ctx.now)
        at = store.polled_at if polling and store.polled_at else ctx.now
        return {"lightning.flashes": Reading(value=store.recent(ctx.now), observed_at=at, source=self.name)}
