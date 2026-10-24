"""Renderer, quantizer and HTTP endpoints. Tests marked `render` need a Playwright Chromium."""
import io
import os
import sys
import time
from datetime import datetime

import pytest
from conftest import REPO_DIR
from PIL import Image, ImageChops

from app.providers.fixture import fixture_now
from app.render.quantize import LEVELS, LUT, quantize

GOLDEN = {"fall": "dashboard-eink.png", "winter": "dashboard-winter-eink.png"}
MAX_DIFF_FRACTION = 0.005  # < 0.5 % of 1200 × 825 pixels
TIMINGS: dict[str, float] = {}


def reference_dir():
    """Canonical baseline is Linux Chromium (the container); Windows text rasterizes differently, so Windows
    dev runs compare with design/reference/windows/ (same boards, rendered on Windows)."""
    base = REPO_DIR / "design" / "reference"
    if sys.platform == "win32" and (base / "windows").is_dir():
        return base / "windows"
    return base


# ------------------------------------------------------------------ no browser needed


def levels_used(img):
    return {v for v, n in enumerate(img.histogram()) if n}


def test_levels_and_lut_match_reference_tool():
    assert LEVELS == [0, 36, 73, 109, 146, 182, 219, 255]
    assert LUT[0] == 0 and LUT[18] == 0 and LUT[19] == 36 and LUT[255] == 255
    assert set(LUT) == set(LEVELS)
    img = Image.linear_gradient("L").resize((256, 4))
    assert levels_used(quantize(img)) <= set(LEVELS)


def test_sleep_target():
    from app.main import sleep_seconds

    tz = fixture_now("fall").tzinfo
    assert sleep_seconds(datetime(2026, 10, 24, 14, 30, 0, tzinfo=tz)) == 1800 + 20
    assert sleep_seconds(datetime(2026, 10, 24, 14, 59, 50, tzinfo=tz)) == 60  # 10 + 20 s, clamped to ≥ 60
    assert sleep_seconds(datetime(2026, 10, 24, 14, 10, 0, tzinfo=tz)) == 1200 + 20


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    from fastapi.testclient import TestClient

    os.environ["INKPLATE_CACHE_DIR"] = str(tmp_path_factory.mktemp("cache"))
    from app.main import app

    with TestClient(app) as c:
        yield c


def test_health_is_cheap(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    assert client.app.state.renderer._browser is None  # health never starts Chromium


def test_preview_serves_html_with_local_fonts(client):
    r = client.get("/preview")
    assert r.status_code == 200 and "Savoy, IL" in r.text
    assert "fonts.googleapis" not in r.text and "/assets/fonts/inter-tight.css" in r.text
    assert client.get("/assets/fonts/inter-tight.css").status_code == 200
    assert "Winter Storm Warning" in client.get("/preview?fixture=winter").text


def test_unknown_fixture_is_404(client):
    assert client.get("/fixtures/nope.png").status_code == 404
    assert client.get("/fixtures/..%2Fconfig.png").status_code == 404


# ------------------------------------------------------------------ browser


def _timed_get(client, url):
    t0 = time.perf_counter()
    r = client.get(url)
    return r, (time.perf_counter() - t0) * 1000


@pytest.mark.render
@pytest.mark.parametrize("name", ["fall", "winter"])
def test_golden(name, client, needs_chromium, out_dir):
    ref_path = reference_dir() / GOLDEN[name]
    r, ms = _timed_get(client, f"/fixtures/{name}.png")
    TIMINGS.setdefault("cold_ms", ms)
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    _, warm = _timed_get(client, f"/fixtures/{name}.png")
    TIMINGS[f"{name}_warm_ms"] = warm
    img = Image.open(io.BytesIO(r.content))
    img.save(out_dir / f"{name}.png")
    assert img.size == (1200, 825) and img.mode == "L"
    if not ref_path.is_file():
        pytest.skip("design/reference not present (container image)")
    ref = Image.open(ref_path).convert("L")
    diff = ImageChops.difference(img, ref).point(lambda v: 255 if v else 0)
    changed = diff.histogram()[255]
    fraction = changed / (1200 * 825)
    # Diff image: reference dimmed, differing pixels in red.
    base = ref.point(lambda v: 128 + v // 2).convert("RGB")
    base.paste((255, 0, 0), mask=diff)
    base.save(out_dir / f"{name}-diff.png")
    line = f"{name}: {changed} px differ ({fraction:.3%}) vs {ref_path.parent.name}/; warm render {warm:.0f} ms\n"
    (out_dir / f"{name}-golden.txt").write_text(line, encoding="utf-8")
    print(line.strip())
    assert fraction < MAX_DIFF_FRACTION, line


@pytest.mark.render
def test_display_png_contract(client, needs_chromium, out_dir):
    r, ms = _timed_get(client, "/display.png?batt=55&mv=3900&reason=button")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert 60 <= int(r.headers["x-sleep-seconds"]) <= 3600
    # `Date` is added by uvicorn in the real server (TestClient does not add it).
    img = Image.open(io.BytesIO(r.content))
    assert img.size == (1200, 825) and img.mode in ("L", "P")
    assert levels_used(img.convert("L")) <= set(LEVELS)
    assert len(r.content) < 400_000
    # Garbage parameters are ignored, not errors.
    r2 = client.get("/display.png?batt=lots&mv=&reason=zzz")
    assert r2.status_code == 200 and r2.headers["content-type"] == "image/png"
    TIMINGS["display_ms"] = ms


@pytest.mark.render
def test_render_time(client, needs_chromium, out_dir):
    times = []
    for _ in range(3):
        r, ms = _timed_get(client, "/fixtures/fall.png")
        assert r.status_code == 200
        times.append(ms)
    TIMINGS["fall_warm_best_ms"] = min(times)
    text = " ".join(f"{k}={v:.0f}" for k, v in TIMINGS.items())
    (out_dir / "timing.txt").write_text(text + "\n", encoding="utf-8")
    print("render timings:", text)
    assert min(times) < 3000
