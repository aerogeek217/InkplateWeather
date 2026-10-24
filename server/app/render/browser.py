"""One persistent headless Chromium (Playwright, async) that turns dashboard HTML into a PNG.

Pages are loaded under RENDER_ORIGIN; every request to it is answered from memory (the page) or from
server/assets (fonts), and anything else is aborted, so rendering never touches the network.
"""
from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import time
from pathlib import Path

from ..paths import ASSETS_DIR
from . import RENDER_ORIGIN

log = logging.getLogger(__name__)

WIDTH, HEIGHT = 1200, 825


def _use_project_browsers() -> None:
    """Prefer browsers installed into the venv (`PLAYWRIGHT_BROWSERS_PATH=0 playwright install`) when present."""
    if "PLAYWRIGHT_BROWSERS_PATH" in os.environ:
        return
    try:
        import playwright

        local = Path(playwright.__file__).parent / "driver" / "package" / ".local-browsers"
        if local.is_dir() and any(local.glob("chromium*")):
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "0"
    except Exception:  # pragma: no cover - best effort
        pass


class Renderer:
    def __init__(self, width: int = WIDTH, height: int = HEIGHT, assets_dir: Path = ASSETS_DIR):
        self.width = width
        self.height = height
        self.assets_dir = assets_dir.resolve()
        self._pw = None
        self._browser = None
        self._context = None
        self._lock = asyncio.Lock()
        self._html = ""
        self.last_ms: float | None = None

    async def start(self) -> None:
        if self._browser is not None and self._browser.is_connected():
            return
        _use_project_browsers()
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        try:
            self._browser = await self._pw.chromium.launch(args=["--disable-gpu", "--hide-scrollbars"])
        except Exception:
            await self._pw.stop()
            self._pw = None
            raise
        self._context = await self._browser.new_context(
            viewport={"width": self.width, "height": self.height}, device_scale_factor=1,
            color_scheme="light", reduced_motion="reduce")
        await self._context.route("**/*", self._handle)
        log.info("chromium started (%s)", self._browser.version)

    async def close(self) -> None:
        for obj, meth in ((self._context, "close"), (self._browser, "close"), (self._pw, "stop")):
            if obj is not None:
                try:
                    await getattr(obj, meth)()
                except Exception:  # pragma: no cover
                    log.debug("error closing %s", obj, exc_info=True)
        self._context = self._browser = self._pw = None

    async def _handle(self, route) -> None:
        url = route.request.url
        if not url.startswith(RENDER_ORIGIN + "/"):
            await route.abort()
            return
        path = url[len(RENDER_ORIGIN) + 1:].split("?", 1)[0].split("#", 1)[0]
        if path in ("", "index.html"):
            await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=self._html)
            return
        if path.startswith("assets/"):
            f = (self.assets_dir / path[len("assets/"):]).resolve()
            if self.assets_dir in f.parents and f.is_file():
                ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
                if f.suffix == ".woff2":
                    ctype = "font/woff2"
                await route.fulfill(status=200, content_type=ctype, body=f.read_bytes())
                return
        await route.fulfill(status=404, body="not found")

    async def screenshot(self, html: str) -> bytes:
        """Render one page (HTML referencing /assets/...) to PNG bytes at width × height."""
        async with self._lock:
            await self.start()
            t0 = time.perf_counter()
            self._html = html
            page = await self._context.new_page()
            try:
                await page.goto(RENDER_ORIGIN + "/", wait_until="load")
                await page.evaluate("document.fonts.ready.then(() => true)")
                png = await page.screenshot(
                    type="png", clip={"x": 0, "y": 0, "width": self.width, "height": self.height},
                    animations="disabled", caret="hide")
            finally:
                await page.close()
            self.last_ms = (time.perf_counter() - t0) * 1000
            return png


async def can_launch() -> bool:
    """True when a Playwright Chromium is installed and starts (used to skip render tests)."""
    r = Renderer()
    try:
        await r.start()
        return True
    except Exception as e:  # pragma: no cover - environment dependent
        log.warning("chromium unavailable: %s", e)
        return False
    finally:
        await r.close()
