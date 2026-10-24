"""FastAPI app: /display.png (Inkplate), /preview, /status, /admin/reload, /health, /fixtures/{name}.png.

Run from server/:  uvicorn app.main:app --host 0.0.0.0 --port 8080
Config: env INKPLATE_CONFIG (default config/config.yaml); falls back to config/config.example.yaml.
Cache: env INKPLATE_CACHE_DIR, else `server.cache_dir` (provider last-good results, station history, last-good PNG).
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from . import collector, config, history, status, viewmodel
from .cache import ProviderCache
from .cache import cache_dir as cache_dir
from .config import AppConfig
from .context import DashboardContext
from .paths import ASSETS_DIR, REPO_DIR
from .providers.base import DeviceInfo, RequestContext
from .providers.fixture import FixtureError, fixture_device, fixture_now
from .render import environment, render_html
from .render.browser import Renderer
from .render.quantize import quantize_png
from .resolver import Resolved

log = logging.getLogger("inkplate")

SLEEP_MARGIN_S = 20
SLEEP_MIN_S, SLEEP_MAX_S = 60, 3600
WAKE_REASONS = {"timer", "button", "retry", "boot"}
HISTORY_SAMPLE_MINUTES = 10  # default for the background station sampler (0 disables)
SAMPLER_FIRST_DELAY_S = 5.0


# ------------------------------------------------------------------ config


def _candidates(p: Path) -> list[Path]:
    """A relative path is tried against the working directory, then the repo root."""
    return [p] if p.is_absolute() else [Path.cwd() / p, REPO_DIR / p]


def config_path() -> Path:
    """The config file to load: INKPLATE_CONFIG (default config/config.yaml), else the example config."""
    wanted = Path(os.environ.get("INKPLATE_CONFIG", "config/config.yaml"))
    for p in _candidates(wanted):
        if p.is_file():
            return p
    for p in _candidates(Path("config/config.example.yaml")):
        if p.is_file():
            log.warning("config %s not found; using the example config %s", wanted, p)
            return p
    raise FileNotFoundError(f"no config at {wanted} and no config/config.example.yaml")


def load_config() -> AppConfig:
    p = config_path()
    log.info("config: %s", p)
    return config.load(p)


def config_error_text(e: BaseException) -> str:
    """A readable config error without echoing input values (they could contain secrets)."""
    if isinstance(e, ValidationError):
        lines = []
        for err in e.errors():
            loc = ".".join(str(x) for x in err.get("loc", ())) or "(config)"
            lines.append(f"{loc}: {err.get('msg', 'invalid')}")
        return "\n".join(lines)
    if isinstance(e, yaml.YAMLError):
        return f"YAML syntax error: {e}"
    return f"{type(e).__name__}: {e}"


def sleep_seconds(now: datetime, refresh_minutes: int = 30) -> int:
    """Seconds until the next :00/:30 (in now's timezone) plus a margin, clamped to the firmware's range."""
    nxt = viewmodel.next_refresh(now, refresh_minutes)
    secs = math.ceil((nxt - now).total_seconds()) + SLEEP_MARGIN_S
    return max(SLEEP_MIN_S, min(SLEEP_MAX_S, secs))


def history_sample_minutes(cfg: AppConfig) -> float:
    """`server.history_sample_minutes` (once config.py has it), else the history provider's `sample_minutes`."""
    v = getattr(cfg.server, "history_sample_minutes", None)
    if v is None:
        v = next((p.options["sample_minutes"] for p in cfg.providers.values()
                  if p.type == "history" and "sample_minutes" in p.options), None)
    try:
        return max(0.0, float(v)) if v is not None else HISTORY_SAMPLE_MINUTES
    except (TypeError, ValueError):
        log.warning("bad history sample interval %r; using %s min", v, HISTORY_SAMPLE_MINUTES)
        return HISTORY_SAMPLE_MINUTES


# ------------------------------------------------------------------ app state


def _apply_config(state, cfg: AppConfig, path: Path | None) -> dict[str, list[str]]:
    """Install a config: providers are rebuilt on next use, caches kept for providers whose config didn't change."""
    new_dir = cache_dir(cfg)
    if getattr(state, "cache", None) is None or state.cache_dir != new_dir:
        state.cache = ProviderCache(new_dir)
        state.cache_dir = new_dir
    state.history = history.get_store(new_dir)
    kept = state.cache.configure(cfg)
    state.config = cfg
    state.config_path = path
    state.config_loaded_at = datetime.now(ZoneInfo(cfg.location.tz))
    state.providers = None
    state.provider_errors = {}
    return kept


def providers_for(state, cfg: AppConfig) -> dict:
    if state.providers is None:
        errors: dict[str, str] = {}
        state.providers = collector.build_providers(cfg, errors)
        state.provider_errors = errors
    return state.providers


async def sample_history(state) -> None:
    """Resolve just the recorded station fields (primary source only) so history fills between requests."""
    cfg: AppConfig = state.config
    if cfg.server.fixture:
        return
    fields = {f: cfg.fields[f][:1] for f in history.RECORDED if cfg.fields.get(f)}
    if not fields:
        return
    ctx = RequestContext(now=collector.now_for(cfg), device=DeviceInfo(), config=cfg)
    await collector.collect(providers_for(state, cfg), fields, ctx, cache=state.cache, history=state.history)


async def _sampler_loop(state, minutes: float) -> None:
    await asyncio.sleep(SAMPLER_FIRST_DELAY_S)
    while True:
        try:
            await sample_history(state)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("history sampler failed")
        await asyncio.sleep(minutes * 60)


def _start_sampler(state) -> None:
    _stop_sampler(state)
    cfg: AppConfig = state.config
    minutes = history_sample_minutes(cfg)
    has_history = any(p.type == "history" for p in cfg.providers.values())
    if cfg.server.fixture or minutes <= 0 or not has_history:
        return
    log.info("history sampler: every %s min", minutes)
    state.sampler = asyncio.create_task(_sampler_loop(state, minutes), name="history-sampler")


def _stop_sampler(state) -> None:
    task = getattr(state, "sampler", None)
    if task is not None and not task.done():
        task.cancel()
    state.sampler = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    st = app.state
    path = config_path()
    _apply_config(st, config.load(path), path)
    st.renderer = Renderer(st.config.server.width, st.config.server.height)
    st.last_request = None
    st.reload_lock = asyncio.Lock()
    st.sampler = None
    _start_sampler(st)
    try:
        yield
    finally:
        task = st.sampler
        _stop_sampler(st)
        if task is not None:
            with suppress(asyncio.CancelledError, Exception):
                await task
        await st.renderer.close()


app = FastAPI(title="Inkplate weather", lifespan=lifespan)
app.mount("/assets", StaticFiles(directory=str(ASSETS_DIR)), name="assets")


def _cfg(request: Request) -> AppConfig:
    return request.app.state.config


async def resolve_now(cfg: AppConfig, fixture: str | None, device: DeviceInfo | None = None,
                      app_state=None) -> tuple[datetime, DeviceInfo, dict[str, Resolved]]:
    """Gather and resolve every field: from a fixture, or live (through the cache, recording history)."""
    if fixture:
        now = fixture_now(fixture)
        dev = fixture_device(fixture)
        if device is not None:  # real values from the Inkplate win over the fixture's defaults
            dev = replace(dev, **{k: v for k, v in vars(device).items() if v is not None})
        ctx = RequestContext(now=now, device=dev, config=cfg)
        return now, dev, await collector.collect_fixture(fixture, ctx)
    now = collector.now_for(cfg)
    dev = device or DeviceInfo()
    ctx = RequestContext(now=now, device=dev, config=cfg)
    if app_state is None:
        return now, dev, await collector.collect(collector.build_providers(cfg), cfg.fields, ctx)
    providers = providers_for(app_state, cfg)
    resolved = await collector.collect(providers, cfg.fields, ctx, cache=app_state.cache, history=app_state.history)
    return now, dev, resolved


async def build_context(cfg: AppConfig, fixture: str | None, device: DeviceInfo | None = None,
                        app_state=None) -> DashboardContext:
    now, dev, resolved = await resolve_now(cfg, fixture, device, app_state)
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
    request.app.state.last_request = {"at": real_now, "device": device}
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


# ------------------------------------------------------------------ /status


async def _status(request: Request) -> dict:
    st = request.app.state
    cfg: AppConfig = st.config
    last = st.last_request
    device = last["device"] if last else None
    now, _dev, resolved = await resolve_now(cfg, cfg.server.fixture, device, st)
    return status.build(st, cfg, now, resolved)


@app.get("/status.json")
async def status_json(request: Request) -> JSONResponse:
    return JSONResponse(await _status(request), headers={"Cache-Control": "no-store"})


@app.get("/status", response_class=HTMLResponse)
async def status_page(request: Request) -> HTMLResponse:
    data = await _status(request)
    html = environment().get_template("status.html.j2").render(s=data)
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


# ------------------------------------------------------------------ /admin/reload

RELOAD_FORM = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Reload config</title>
<style>body{font:15px/1.5 system-ui,sans-serif;max-width:40rem;margin:2rem auto;padding:0 1rem}
code{background:#eee;padding:0 .25rem}button{font:inherit;padding:.4rem 1rem}</style></head>
<body><h1>Reload config</h1>
<p>Re-reads the config file, rebuilds the providers and keeps cached data for providers whose settings didn't
change. If the new config has an error, the current one stays in effect and the error is shown.</p>
<form method="post" action="/admin/reload"><button type="submit">Reload now</button></form>
<p>From a shell: <code>curl -X POST http://&lt;server&gt;:8080/admin/reload</code></p>
<p><a href="/status">Back to status</a></p></body></html>
"""


@app.get("/admin/reload", response_class=HTMLResponse)
async def reload_form() -> HTMLResponse:
    return HTMLResponse(RELOAD_FORM)


@app.post("/admin/reload")
async def reload_config(request: Request) -> PlainTextResponse:
    st = request.app.state
    async with st.reload_lock:
        try:
            path = config_path()
            new = config.load(path)
            if not new.server.fixture:
                collector.build_providers(new)  # unknown types / bad options are config errors too
        except Exception as e:
            log.warning("config reload rejected: %s", e)
            return PlainTextResponse("Config error; keeping the previous config.\n\n" + config_error_text(e) + "\n",
                                     status_code=400)
        old: AppConfig = st.config
        changes = _apply_config(st, new, path)
        notes = []
        if (old.server.width, old.server.height) != (new.server.width, new.server.height):
            notes.append("Display size changed: restart the server for it to take effect.")
        _start_sampler(st)
    lines = [f"Reloaded {path}.",
             f"Mode: {'fixture ' + repr(new.server.fixture) if new.server.fixture else 'live'}; "
             f"{len(new.providers)} providers, {len(new.fields)} fields.",
             f"Cache kept: {', '.join(changes['kept']) or 'none'}.",
             f"Cache reset (config changed): {', '.join(changes['reset']) or 'none'}.", *notes]
    log.info("config reloaded from %s", path)
    return PlainTextResponse("\n".join(lines) + "\n")


def _int(v: str | None, lo: int, hi: int) -> int | None:
    try:
        n = int(float(v)) if v is not None else None
    except ValueError:
        return None
    return n if n is not None and lo <= n <= hi else None
