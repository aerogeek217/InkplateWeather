"""Filesystem locations, resolved relative to the package (never the working directory)."""
from __future__ import annotations

from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1]  # .../server
REPO_DIR = SERVER_DIR.parent
ASSETS_DIR = SERVER_DIR / "assets"
ICONS_DIR = ASSETS_DIR / "icons"
FONTS_DIR = ASSETS_DIR / "fonts"
TEMPLATES_DIR = SERVER_DIR / "templates"
FIXTURES_DIR = SERVER_DIR / "fixtures"
