"""Render the approved boards to reference PNGs with a headless Chromium browser.

For each board it writes:
  design/reference/<name>.png       straight browser render
  design/reference/<name>-eink.png  quantized to the Inkplate 10's 8 gray levels

Usage: python design/tools/render_reference.py [--browser PATH]
Needs a Chromium-based browser (Edge or Chrome) and Pillow.
"""
import argparse
import pathlib
import shutil
import subprocess
import tempfile
import time

from PIL import Image

ROOT = pathlib.Path(__file__).resolve().parents[1]
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
    args = ap.parse_args()
    browser = find_browser(args.browser)
    out_dir = ROOT / "reference"
    out_dir.mkdir(exist_ok=True)
    for name, (rel, w, h) in BOARDS.items():
        # A fresh throwaway profile per render keeps the headless browser from
        # attaching to one that is still open (which silently skips the shot).
        profile = tempfile.mkdtemp(prefix="render-profile-")
        src = (ROOT / rel).resolve()
        png = out_dir / f"{name}.png"
        png.unlink(missing_ok=True)  # a stale file must not count as success
        subprocess.run([
            browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
            f"--user-data-dir={profile}",
            f"--window-size={w},{h}", "--virtual-time-budget=8000",
            f"--screenshot={png}", src.as_uri(),
        ], check=True, capture_output=True)
        # On Windows the msedge.exe launcher returns before the headless
        # browser has written the file, so wait for it to appear and settle.
        wait_for_file(png)
        quantize(png, out_dir / f"{name}-eink.png")
        print(f"rendered {name} ({w}x{h})")


if __name__ == "__main__":
    main()
