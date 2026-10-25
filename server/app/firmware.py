"""Over-the-air firmware hosting for the Inkplate.

The device sleeps, so it pulls: it reports its build number as `fw=<n>` on /display.png, and when this server holds
a newer build the reply carries `X-Firmware-Update: <n>`. The device then fetches `/firmware/inkplate_weather-<n>.bin`
(with an `x-MD5` header, which the ESP32 HTTPUpdate class checks).

Builds live in `<cache_dir>/firmware/` (cache_dir = env INKPLATE_CACHE_DIR, else `server.cache_dir`), published with
`scp` from `firmware/dist/` (written by `firmware/build.ps1 -Export`):

- `manifest.json` (optional): {"version": 2, "file": "inkplate_weather-2.bin", "md5": "...", ...}. When it is valid it
  names the build to offer; a wrong `md5` means a damaged upload, so nothing is offered. A manifest that can't be used
  for any other reason is ignored and the folder is scanned.
- otherwise the highest `inkplate_weather-<n>.bin` in the folder is offered.

A missing folder, a malformed file or an unknown/missing `fw` value simply means "no update": /display.png is never
affected.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

log = logging.getLogger(__name__)

SUBDIR = "firmware"
MANIFEST = "manifest.json"
FILE_RE = re.compile(r"^inkplate_weather-(\d{1,6})\.bin$")
HEADER = "X-Firmware-Update"
MAX_VERSION = 999_999

router = APIRouter()


@dataclass(frozen=True)
class Build:
    version: int
    path: Path
    md5: str
    size: int

    @property
    def name(self) -> str:
        return self.path.name


def firmware_dir(cache: Path) -> Path:
    return Path(cache) / SUBDIR


def parse_version(v: str | int | None) -> int | None:
    """A build number from the `fw` query value: a positive integer, else None."""
    if v is None:
        return None
    try:
        n = int(str(v).strip())
    except ValueError:
        return None
    return n if 0 < n <= MAX_VERSION else None


_md5_cache: dict[tuple[str, int, int], str] = {}


def file_md5(path: Path) -> str:
    """MD5 of a file, remembered until its size or modification time changes."""
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size)
    if key not in _md5_cache:
        h = hashlib.md5()  # noqa: S324 - the ESP32 updater verifies MD5; this is an integrity check, not security
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 16), b""):
                h.update(chunk)
        _md5_cache.clear()  # only the current builds matter; keep the dict from growing
        _md5_cache[key] = h.hexdigest()
    return _md5_cache[key]


def _build(path: Path, version: int) -> Build | None:
    try:
        size = path.stat().st_size
        if size <= 0:
            return None
        return Build(version, path, file_md5(path), size)
    except OSError:
        return None


class _Damaged(Exception):
    """The manifest's md5 doesn't match the file: a damaged upload, so offer nothing at all."""


def _from_manifest(d: Path) -> Build | None:
    """The manifest's build; None when there is no usable manifest; raises _Damaged on an md5 mismatch."""
    try:
        m = json.loads((d / MANIFEST).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        log.warning("firmware manifest unreadable: %s", e)
        return None
    try:
        version, name = parse_version(m["version"]), str(m["file"])
    except (KeyError, TypeError):
        log.warning("firmware manifest needs 'version' and 'file'")
        return None
    fm = FILE_RE.match(name)
    if version is None or not fm or int(fm.group(1)) != version:
        # The device builds the URL from the version, so the file name has to carry the same number.
        log.warning("firmware manifest: file %r doesn't match version %r", name, m.get("version"))
        return None
    b = _build(d / name, version)
    if b is None:
        log.warning("firmware manifest names %s but the file is missing or empty", name)
        return None
    want = str(m.get("md5") or "").lower()
    if want and want != b.md5:
        log.warning("firmware %s has md5 %s but the manifest says %s; not offering it", name, b.md5, want)
        raise _Damaged
    return b


def latest(cache: Path) -> Build | None:
    """The build to offer: the manifest's, else the highest-numbered file. None when there is nothing."""
    d = firmware_dir(cache)
    try:
        if not d.is_dir():
            return None
        if (d / MANIFEST).exists():
            b = _from_manifest(d)
            if b is not None:
                return b
        found = sorted(((int(m.group(1)), p) for p in d.iterdir() if (m := FILE_RE.match(p.name))),
                       key=lambda t: t[0], reverse=True)
        for version, p in found:  # highest first; skip empty files
            b = _build(p, version)
            if b is not None:
                return b
        return None
    except _Damaged:
        return None
    except OSError as e:
        log.warning("firmware folder unreadable: %s", e)
        return None


def update_headers(cache: Path, reported: int | None) -> dict[str, str]:
    """`{"X-Firmware-Update": "<n>"}` when a build newer than the device's `fw` exists, else {}."""
    if reported is None:
        return {}
    try:
        b = latest(cache)
    except Exception:  # never let firmware hosting break the picture
        log.exception("firmware lookup failed")
        return {}
    return {HEADER: str(b.version)} if b is not None and b.version > reported else {}


def status_entry(cache: Path | None, reported: int | None) -> dict:
    """For /status: the build on offer and the `fw` the display last reported."""
    b = latest(cache) if cache is not None else None
    return {
        "reported": reported,
        "available": b.version if b else None,
        "file": b.name if b else None,
        "size": b.size if b else None,
        "update_pending": bool(b and reported is not None and b.version > reported),
    }


@router.get("/firmware/{name}")
async def get_firmware(request: Request, name: str) -> FileResponse:
    cache = getattr(request.app.state, "cache_dir", None)
    m = FILE_RE.match(name)
    if cache is None or not m:
        raise HTTPException(status_code=404, detail="no such firmware")
    path = firmware_dir(cache) / name
    b = _build(path, int(m.group(1)))
    if b is None:
        raise HTTPException(status_code=404, detail="no such firmware")
    return FileResponse(path, media_type="application/octet-stream",
                        headers={"x-MD5": b.md5, "Cache-Control": "no-store"})
