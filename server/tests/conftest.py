import asyncio
import os
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
SERVER_DIR = TESTS_DIR.parent
REPO_DIR = SERVER_DIR.parent
OUT_DIR = TESTS_DIR / "_out"
TEST_CONFIG = TESTS_DIR / "data" / "config.yaml"

# The app reads these at startup; tests must not depend on files outside server/.
os.environ.setdefault("INKPLATE_CONFIG", str(TEST_CONFIG))


@pytest.fixture(scope="session")
def chromium_ok() -> bool:
    from app.render.browser import can_launch

    return asyncio.run(can_launch())


@pytest.fixture
def needs_chromium(chromium_ok):
    if not chromium_ok:
        pytest.skip("Playwright Chromium is not installed or does not start")


@pytest.fixture(scope="session")
def out_dir() -> Path:
    OUT_DIR.mkdir(exist_ok=True)
    return OUT_DIR
