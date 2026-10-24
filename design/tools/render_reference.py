"""Render the approved boards to reference PNGs with a headless Chromium browser.

For each board it writes:
  <out>/<name>.png       straight browser render
  <out>/<name>-eink.png  quantized to the Inkplate 10's 8 gray levels

Fonts come from the bundled server/assets/fonts (the same files Google Fonts serves), so renders need no
internet and match what the server draws.

Baselines (see design/SPEC.md, Reference renders):
  design/reference/          canonical: Linux Chromium, rendered inside the server container
  design/reference/windows/  Windows dev baseline (Edge or Playwright Chromium), used by tests on Windows

Usage: python design/tools/render_reference.py [--browser PATH] [--out DIR] [--only NAME ...] [--no-sandbox]

Canonical baseline, from the repo root (Docker Desktop on Windows: prefix with MSYS_NO_PATHCONV=1 in Git Bash):
  docker run --rm -v "$PWD:/repo" -w /repo inkplate-weather:latest sh -c \
    'python design/tools/render_reference.py --no-sandbox \
       --browser $(ls -d /ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell)'
Needs a Chromium-based browser (Edge, Chrome, Chromium or Playwright's headless shell) and Pillow.
"""
import argparse
import pathlib
import re
import shutil
import subprocess
import tempfile
import time

from PIL import Image

ROOT = pathlib.Path(__file__).resolve().parents[1]
FONT_CSS = ROOT.parent / "server" / "assets" / "fonts" / "inter-tight.css"
GOOGLE_FONTS = re.compile(r'<link href="https://fonts\.googleapis\.com/[^"]*" rel="stylesheet">')
BOARDS = {  # name: (source html, width, height)
    "dashboard": ("source/dashboard.html", 1200, 825),
    "dashboard-winter": ("source/dashboard-winter.html", 1200, 825),
    "icons": ("source/icons.html", 1200, 1000),
}
# Inkplate 10 grayscale mode: 3 bits, 8 evenly spaced levels.
LEVELS = [round(i * 255 / 7) for i in range(8)]
CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "chromium", "chromium-browser", "google-chrome", "microsoft-edge",
]


def find_browser(explicit):
    for c in [explicit] + CANDIDATES if explicit else CANDIDATES:
        if c and (pathlib.Path(c).exists() or shutil.which(c)):
            return c
    raise SystemExit("No Chromium-based browser found; pass --browser")


def quantize(src: pathlib.Path, dst: pathlib.Path) -> None:
    img = Image.open(src).convert("L")
    lut = [min(LEVELS, key=lambda v: abs(v - i)) for i in range(256)]
    img.point(lut).save(dst)


def local_fonts(src: pathlib.Path, tmp: pathlib.Path) -> pathlib.Path:
    """Copy of the board with the Google Fonts link pointed at the bundled font files."""
    html = src.read_text(encoding="utf-8")
    html, n = GOOGLE_FONTS.subn(f'<link href="{FONT_CSS.as_uri()}" rel="stylesheet">', html)
    if n != 1:
        raise SystemExit(f"{src.name}: expected one Google Fonts link, found {n}")
    out = tmp / src.name
    out.write_text(html, encoding="utf-8")
    return out


def wait_for_file(path: pathlib.Path, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    last = -1
    while time.monotonic() < deadline:
        if path.exists():
            size = path.stat().st_size
            if size > 0 and size == last:
                return
            last = size
        time.sleep(0.5)
    raise SystemExit(f"browser produced no screenshot: {path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--browser")
    ap.add_argument("--out", default=str(ROOT / "reference"))
    ap.add_argument("--only", nargs="*", choices=list(BOARDS))
    ap.add_argument("--no-sandbox", action="store_true", help="needed inside a container (as Playwright does)")
    args = ap.parse_args()
    browser = find_browser(args.browser)
    out_dir = pathlib.Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, (rel, w, h) in BOARDS.items():
        if args.only and name not in args.only:
            continue
        # A fresh throwaway profile per render keeps the headless browser from
        # attaching to one that is still open (which silently skips the shot).
        profile = tempfile.mkdtemp(prefix="render-profile-")
        src = local_fonts((ROOT / rel).resolve(), pathlib.Path(tempfile.mkdtemp(prefix="render-src-")))
        png = out_dir / f"{name}.png"
        png.unlink(missing_ok=True)  # a stale file must not count as success
        subprocess.run([
            browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
            *(["--no-sandbox"] if args.no_sandbox else []),
            f"--user-data-dir={profile}",
            f"--window-size={w},{h}", "--virtual-time-budget=8000",
            f"--screenshot={png}", src.as_uri(),
        ], check=True, capture_output=True)
        # On Windows the msedge.exe launcher returns before the headless
        # browser has written the file, so wait for it to appear and settle.
        wait_for_file(png)
        quantize(png, out_dir / f"{name}-eink.png")
        print(f"rendered {name} ({w}x{h}) -> {out_dir}")


if __name__ == "__main__":
    main()
