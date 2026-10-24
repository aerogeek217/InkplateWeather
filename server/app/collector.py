"""Run providers concurrently within a time budget, then resolve every configured field."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from datetime import datetime

from . import fields as field_registry
from .config import AppConfig, ProviderCfg, SourceCfg
from .providers import base
from .providers.base import Provider, RequestContext
from .providers.fixture import FixtureProvider
from .resolver import ProviderResult, Resolved, resolve

log = logging.getLogger(__name__)

BUDGET_S = 8.0


def _import_providers() -> None:
    """Import provider modules so their @register decorators run."""
    import importlib
    import pkgutil

    from . import providers

    for m in pkgutil.iter_modules(providers.__path__):
        if m.name != "base":
            try:
                importlib.import_module(f"{providers.__name__}.{m.name}")
            except Exception:  # a broken optional provider must not take the server down
                log.exception("could not import provider module %s", m.name)


def build_providers(cfg: AppConfig) -> dict[str, Provider]:
    _import_providers()
    return {name: base.build(name, pcfg, cfg) for name, pcfg in cfg.providers.items()}


async def _fetch(p: Provider, ctx: RequestContext, budget: float) -> ProviderResult:
    try:
        return await asyncio.wait_for(p.fetch(ctx), timeout=budget)
    except Exception as e:  # recorded as an error for every field using this provider
        log.warning("provider %s failed: %s", getattr(p, "name", p), e)
        return e


async def collect(providers: Mapping[str, Provider], fields_cfg: Mapping[str, list[SourceCfg]],
                  ctx: RequestContext, budget: float = BUDGET_S) -> dict[str, Resolved]:
    used = {s.from_ for sources in fields_cfg.values() for s in sources}
    names = [n for n in providers if n in used]
    results = await asyncio.gather(*(_fetch(providers[n], ctx, budget) for n in names))
    return resolve(fields_cfg, dict(zip(names, results, strict=True)), ctx.now)


def fixture_fields(source: str = "fixture") -> dict[str, list[SourceCfg]]:
    """Every known field from the fixture provider (fixture renders ignore the live field config)."""
    return {name: [SourceCfg.model_validate({"from": source})] for name in field_registry.FIELDS}


async def collect_fixture(name: str, ctx: RequestContext) -> dict[str, Resolved]:
    p = FixtureProvider("fixture", ProviderCfg(type="fixture"), None, fixture=name)
    return await collect({"fixture": p}, fixture_fields(), ctx)


def now_for(cfg: AppConfig) -> datetime:
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo(cfg.location.tz))
