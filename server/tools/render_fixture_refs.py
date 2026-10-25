"""Render server fixtures to golden reference PNGs (`<name>-eink.png`) through the real pipeline.

For boards that have no design HTML (the radar boards come from recorded data), the approved server render is the
reference. Run only after the user approves the look.

Windows baseline (from server/):
    PLAYWRIGHT_BROWSERS_PATH=0 .venv/Scripts/python.exe tools/render_fixture_refs.py --out ../design/reference/windows
Canonical, inside the server container (from the repo root; Git Bash: prefix MSYS_NO_PATHCONV=1):
    docker run --rm -u 0 -v "$PWD:/repo" -w /repo/server -e INKPLATE_CONFIG=/repo/server/tests/data/config.yaml \
      inkplate-weather:latest python tools/render_fixture_refs.py --out ../design/reference
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
from app import config, main  # noqa: E402
from app.render import render_html  # noqa: E402
from app.render.browser import Renderer  # noqa: E402
from app.render.quantize import quantize_png  # noqa: E402

FIXTURES = ["radar-storm", "radar-winter", "radar-motion", "radar-snowline"]


async def run(out: Path, names: list[str]) -> None:
    cfg = config.load(SERVER / "tests" / "data" / "config.yaml")
    r = Renderer()
    await r.start()
    try:
        for n in names:
            png = quantize_png(await r.screenshot(render_html(await main.build_context(cfg, n))))
            (out / f"{n}-eink.png").write_bytes(png)
            print(out / f"{n}-eink.png")
    finally:
        await r.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("names", nargs="*", default=FIXTURES)
    args = ap.parse_args()
    asyncio.run(run(args.out, args.names))
