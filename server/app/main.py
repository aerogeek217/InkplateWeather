"""FastAPI app: /display.png (Inkplate), /preview, /health, /fixtures/{name}.png.

Run from server/:  uvicorn app.main:app --host 0.0.0.0 --port 8080
Config: env INKPLATE_CONFIG (default config/config.yaml); falls back to config/config.example.yaml.
"""
from __future__ import annotations

import logging
import math
import os
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import collector, config, viewmodel
from .config import AppConfig
from .context import DashboardContext
from .paths import ASSETS_DIR, REPO_DIR
from .providers.base import DeviceInfo, RequestContext
from .providers.fixture import FixtureError, fixture_device, fixture_now
from .render import render_html
from .render.browser import Renderer
from .render.quantize import quantize_png

log = logging.getLogger("inkplate")

SLEEP_MARGIN_S = 20
SLEEP_MIN_S, SLEEP_MAX_S = 60, 3600
WAKE_REASONS = {"timer", "button", "retry", "boot"}


# ------------------------------------------------------------------ config


def _candidates(p: Path) -> list[Path]:
    """A relative path is tried against the working directory, then the repo root."""
    return [p] if p.is_absolute() else [Path.cwd() / p, REPO_DIR / p]


def load_config() -> AppConfig:
    wanted = Path(os.environ.get("INKPLATE_CONFIG", "config/config.yaml"))
    for p in _candidates(wanted):
        if p.is_file():
            log.info("config: %s", p)
            return config.load(p)
    for p in _candidates(Path("config/config.example.yaml")):
        if p.is_file():
            log.warning("config %s not found; using the example config %s", wanted, p)
            return config.load(p)
    raise FileNotFoundError(f"no config at {wanted} and no config/config.example.yaml")


def cache_dir(cfg: AppConfig) -> Path:
    return Path(os.environ.get("INKPLATE_CACHE_DIR") or cfg.server.cache_dir)


def sleep_seconds(now: datetime, refresh_minutes: int = 30) -> int:
    """Seconds until the next :00/:30 (in now's timezone) plus a margin, clamped to the firmware's range."""
    nxt = viewmodel.next_refresh(now, refresh_minutes)
    secs = math.ceil((nxt - now).total_seconds()) + SLEEP_MARGIN_S
    return max(SLEEP_MIN_S, min(SLEEP_MAX_S, secs))


# ------------------------------------------------------------------ app


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.config = load_config()
    app.state.renderer = Renderer(app.state.config.server.width, app.state.config.server.height)
    app.state.providers = None
    try:
        yield
    finally:
        await app.state.renderer.close()


app = FastAPI(title="Inkplate weather", lifespan=lifespan)
app.mount("/assets", StaticFiles(directory=str(ASSETS_DIR)), name="assets")


def _cfg(request: Request) -> AppConfig:
    return request.app.state.config


async def build_context(cfg: AppConfig, fixture: str | None, device: DeviceInfo | None = None,
                        app_state=None) -> DashboardContext:
    if fixture:
        now = fixture_now(fixture)
        dev = fixture_device(fixture)
        if device is not None:  # real values from the Inkplate win over the fixture's defaults
            dev = replace(dev, **{k: v for k, v in vars(device).items() if v is not None})
        ctx = RequestContext(now=now, device=dev, config=cfg)
        resolved = await collector.collect_fixture(fixture, ctx)
    else:
        now = collector.now_for(cfg)
        dev = device or DeviceInfo()
        ctx = RequestContext(now=now, device=dev, config=cfg)
        if app_state is not None and app_state.providers is None:
            app_state.providers = collector.build_providers(cfg)
        providers = app_state.providers if app_state is not None else collector.build_providers(cfg)
        resolved = await collector.collect(providers, cfg.fields, ctx)
    return viewmodel.build(resolved, now, dev, cfg)


async def render_png(renderer: Renderer, dctx: DashboardContext) -> bytes:
    png = await renderer.screenshot(render_html(dctx, asset_base="/assets/"))
    return quantize_png(png)


def _save_last_good(cfg: AppConfig, png: bytes) -> None:
    try:
        d = cache_dir(cfg)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "last-good.png.tmp"
        tmp.write_bytes(png)
        tmp.replace(d / "last-good.png")
    except OSError as e:
        log.warning("could not save last-good image: %s", e)


def _last_good(cfg: AppConfig) -> bytes | None:
    p = cache_dir(cfg) / "last-good.png"
    try:
        return p.read_bytes()
    except OSError:
        return None


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@app.get("/display.png")
async def display(request: Request, batt: str | None = Query(None), mv: str | None = Query(None),
                  reason: str | None = Query(None)) -> Response:
    cfg = _cfg(request)
    device = DeviceInfo(battery_pct=_int(batt, 0, 100), voltage_mv=_int(mv, 0, 10000),
                        wake_reason=reason if reason in WAKE_REASONS else None)
    real_now = datetime.now(ZoneInfo(cfg.location.tz))
    headers = {"X-Sleep-Seconds": str(sleep_seconds(real_now, cfg.server.refresh_minutes)),
               "Cache-Control": "no-store"}
    try:
        dctx = await build_context(cfg, cfg.server.fixture, device, request.app.state)
        png = await render_png(request.app.state.renderer, dctx)
        _save_last_good(cfg, png)
    except Exception:
        log.exception("render failed")
        png = _last_good(cfg)
        if png is None:
            raise HTTPException(status_code=503, detail="render failed and no last good image") from None
        # TODO(2.6): burn an error strip into the last good image.
    return Response(content=png, media_type="image/png", headers=headers)


@app.get("/preview", response_class=HTMLResponse)
async def preview(request: Request, fixture: str | None = Query(None)) -> HTMLResponse:
    cfg = _cfg(request)
    try:
        dctx = await build_context(cfg, fixture or cfg.server.fixture, None, request.app.state)
    except FixtureError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None
    return HTMLResponse(render_html(dctx, asset_base="/assets/"))


@app.get("/fixtures/{name}.png")
async def fixture_png(request: Request, name: str) -> Response:
    try:
        dctx = await build_context(_cfg(request), name)
    except FixtureError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None
    png = await render_png(request.app.state.renderer, dctx)
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-store"})


def _int(v: str | None, lo: int, hi: int) -> int | None:
    try:
        n = int(float(v)) if v is not None else None
    except ValueError:
        return None
    return n if n is not None and lo <= n <= hi else None


