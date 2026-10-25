"""Layout v3 on the approved boards (requested 2026-10-25): smaller top/right margin, wider radar view.

Applied to source/dashboard.html and dashboard-winter.html after readability_v2.py. Kept for the record; running it
again on already-updated boards fails its own checks.

  margin   66/68 px → 51 top, 53 right, 66 bottom, 68 left (15 px off the top and right); the 15 px gained go to
           the radar column (536 → 551) and the main area height (the radar 570 → 585)
  radar    551 × 585 px, viewBox −270.4 −234 440.8 468: 4.48 map units per mile (20 % farther out than 5.6), home
           60 mi from the west edge and 38 mi from the east (storms moving east show earlier), ±52 mi north–south
  art      the hand-drawn radar is replaced by the generated one: the fall board shows fixtures/radar-motion.json,
           the winter board fixtures/radar-snowline.json (server/tests/build_radar_fixtures.py writes the same art to
           server/fixtures/radar/*.svg), so each board's radar panel is the server's own render of its fixture,
           callout included

Usage (from the repo root): server/.venv/Scripts/python.exe design/tools/layout_v3.py
"""
import asyncio
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVER = ROOT.parent / "server"
sys.path.insert(0, str(SERVER))
from app import config, main  # noqa: E402
from app.render import render_html  # noqa: E402

BOARDS = {"source/dashboard.html": "fall", "source/dashboard-winter.html": "winter"}
PANEL = '<div style="position: relative; width: '


def sub(s: str, old: str, new: str) -> str:
    if s.count(old) != 1:
        raise SystemExit(f"expected 1 × {old[:80]!r}, found {s.count(old)}")
    return s.replace(old, new)


def panel(html: str) -> tuple[int, int]:
    """(start, end) of the radar panel: the first absolutely positioned container div, to its matching </div>."""
    start = html.index(PANEL)
    depth, i = 0, start
    while True:
        o, c = html.find("<div", i), html.find("</div>", i)
        if o != -1 and o < c:
            depth, i = depth + 1, o + 4
        else:
            depth, i = depth - 1, c + 6
            if depth == 0:
                return start, i


async def server_panels() -> dict[str, str]:
    cfg = config.load(SERVER / "tests" / "data" / "config.yaml")
    out = {}
    for name in BOARDS.values():
        html = render_html(await main.build_context(cfg, name))
        a, b = panel(html)
        out[name] = html[a:b]
    return out


def main_() -> None:
    panels = asyncio.run(server_panels())
    for rel, fixture in BOARDS.items():
        path = ROOT / rel
        s = path.read_text(encoding="utf-8")
        s = sub(s, "padding: 66px 68px;", "padding: 51px 53px 66px 68px;")
        s = sub(s, "grid-template-columns: 536px 504px;", "grid-template-columns: 551px 504px;")
        if '<div style="position: relative; width: 536px; height: 570px; flex-shrink: 0">' not in s:
            raise SystemExit(f"{rel}: v2 radar panel not found")
        a, b = panel(s)
        s = s[:a] + panels[fixture] + s[b:]
        path.write_text(s, encoding="utf-8", newline="\n")
        print(path)


if __name__ == "__main__":
    main_()
