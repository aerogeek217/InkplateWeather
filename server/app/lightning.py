"""GOES-East GLM lightning: a background poller keeping a rolling 30-minute list of flashes in the radar extent.

GLM Level 2 flash files (`GLM-L2-LCFA`, NetCDF4, ~220 KB every 20 s) are public on NOAA's open-data bucket:
`https://noaa-goes19.s3.amazonaws.com/GLM-L2-LCFA/<year>/<day-of-year>/<hour>/OR_GLM-L2-LCFA_G19_s<start>_e…_c….nc`.

Polling all day would download ~1 GB, so the poller only runs while the radar shows echoes in view (and for an hour
after the last render that did): `note_radar()` is called after each render's radar frame. When it wakes up it
backfills the last 30 minutes, then keeps up once a minute, listing only keys newer than the last one seen.

Flashes (good quality only, inside the extent plus a margin) are kept as {"t": iso UTC, "lat", "lon"} and persisted
to `<cache>/lightning.json` so a restart keeps the last half hour.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

BUCKET = "https://noaa-goes19.s3.amazonaws.com"
PRODUCT = "GLM-L2-LCFA"
WINDOW = timedelta(minutes=30)
ACTIVE_AFTER_ECHOES = timedelta(minutes=60)
POLL_S = 60
MAX_PARALLEL = 4
_START = re.compile(r"_s(\d{4})(\d{3})(\d{2})(\d{2})(\d{2})\d_")
_S3 = "{http://s3.amazonaws.com/doc/2006-03-01/}"


def key_start(key: str) -> datetime | None:
    """Scan start time from a GLM file name (…_s20262971800000_…: year, day of year, h, m, s, tenths)."""
    m = _START.search(key)
    if not m:
        return None
    y, doy, h, mi, s = (int(x) for x in m.groups())
    return datetime(y, 1, 1, h, mi, s, tzinfo=UTC) + timedelta(days=doy - 1)


def hour_prefix(t: datetime) -> str:
    t = t.astimezone(UTC)
    return f"{PRODUCT}/{t.year}/{t.timetuple().tm_yday:03d}/{t.hour:02d}/"


def parse(data: bytes, bbox: tuple[float, float, float, float]) -> list[dict]:
    """Good-quality flashes inside bbox (west, south, east, north) from one LCFA file."""
    import h5py  # imported here: only the poller needs it

    w, s, e, n = bbox
    out = []
    with h5py.File(io.BytesIO(data), "r") as f:
        start = datetime.fromisoformat(f.attrs["time_coverage_start"].decode().replace("Z", "+00:00"))
        lat, lon = f["flash_lat"][:], f["flash_lon"][:]
        off = f["flash_time_offset_of_first_event"]
        raw = off[:].astype("uint16") if off.attrs.get("_Unsigned", b"") == b"true" else off[:]
        secs = raw * float(off.attrs.get("scale_factor", [1.0])[0]) + float(off.attrs.get("add_offset", [0.0])[0])
        q = f["flash_quality_flag"][:] if "flash_quality_flag" in f else None
        for i in range(len(lat)):
            if q is not None and q[i] != 0:
                continue
            la, lo = float(lat[i]), float(lon[i])
            if s <= la <= n and w <= lo <= e:
                t = start + timedelta(seconds=float(secs[i]))
                out.append({"t": t.isoformat(timespec="seconds"), "lat": round(la, 4), "lon": round(lo, 4)})
    return out


class Store:
    def __init__(self, path: Path | None = None):
        self.path = path
        self.flashes: list[dict] = []
        self.last_key = ""  # newest file processed
        self.polled_at: datetime | None = None
        self.load()

    def load(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            self.flashes = [f for f in d.get("flashes", []) if isinstance(f, dict)]
            self.last_key = str(d.get("last_key", ""))
            self.polled_at = datetime.fromisoformat(d["polled_at"]) if d.get("polled_at") else None
        except (OSError, ValueError, KeyError, TypeError) as e:
            log.warning("lightning: ignoring unreadable %s (%s)", self.path, e)

    def save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({"flashes": self.flashes, "last_key": self.last_key,
                                       "polled_at": self.polled_at.isoformat() if self.polled_at else None},
                                      separators=(",", ":")), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as e:
            log.warning("lightning: could not save %s (%s)", self.path, e)

    def prune(self, now: datetime) -> None:
        cut = now - WINDOW
        self.flashes = [f for f in self.flashes if datetime.fromisoformat(f["t"]) >= cut]

    def recent(self, now: datetime) -> list[dict]:
        cut = now - WINDOW
        return [f for f in self.flashes if cut <= datetime.fromisoformat(f["t"]) <= now + timedelta(minutes=1)]


class Poller:
    def __init__(self, store: Store, bbox: tuple[float, float, float, float], user_agent: str,
                 bucket: str = BUCKET, timeout: float = 15.0, always: bool = False):
        self.store = store
        self.bbox = bbox
        self.user_agent = user_agent
        self.bucket = bucket.rstrip("/")
        self.timeout = timeout
        self.always = always
        self.active_until: datetime | None = None
        self.task: asyncio.Task | None = None

    def note_radar(self, frame: dict | None, now: datetime) -> None:
        """Called after each render: echoes in view keep the poller running for the next hour."""
        if isinstance(frame, dict) and frame.get("echo_px"):
            self.active_until = now + ACTIVE_AFTER_ECHOES

    def active(self, now: datetime) -> bool:
        return self.always or (self.active_until is not None and now < self.active_until)

    async def list_keys(self, client: httpx.AsyncClient, now: datetime) -> list[str]:
        """Keys of files that started in the last 30 minutes and are newer than the last one processed."""
        since = now - WINDOW
        keys: list[str] = []
        hours = sorted({hour_prefix(since), hour_prefix(now)})
        for prefix in hours:
            params = {"list-type": "2", "prefix": prefix}
            if self.store.last_key.startswith(prefix):
                params["start-after"] = self.store.last_key
            while True:
                r = await client.get(f"{self.bucket}/", params=params)
                r.raise_for_status()
                root = ET.fromstring(r.content)
                for c in root.iter(f"{_S3}Contents"):
                    k = c.findtext(f"{_S3}Key") or ""
                    t = key_start(k)
                    if t is not None and t >= since and k > self.store.last_key:
                        keys.append(k)
                token = root.findtext(f"{_S3}NextContinuationToken")
                if not token:
                    break
                params = {"list-type": "2", "prefix": prefix, "continuation-token": token}
        return sorted(keys)

    async def poll(self, now: datetime | None = None) -> int:
        """Fetch new files once; returns the number of files processed."""
        now = now or datetime.now(UTC)
        sem = asyncio.Semaphore(MAX_PARALLEL)
        async with httpx.AsyncClient(timeout=self.timeout, headers={"User-Agent": self.user_agent}) as client:
            keys = await self.list_keys(client, now)

            async def one(k: str) -> list[dict]:
                async with sem:
                    r = await client.get(f"{self.bucket}/{k}")
                    r.raise_for_status()
                    return await asyncio.to_thread(parse, r.content, self.bbox)

            results = await asyncio.gather(*(one(k) for k in keys), return_exceptions=True)
        done = 0
        for k, res in zip(keys, results, strict=True):
            if isinstance(res, BaseException):
                log.warning("lightning: %s failed (%s)", k.rsplit("/", 1)[-1], res)
                break  # keep last_key before the gap; the next poll retries from there
            self.store.flashes.extend(res)
            self.store.last_key = k
            done += 1
        self.store.polled_at = now
        self.store.prune(now)
        self.store.save()
        return done

    async def run(self) -> None:
        while True:
            now = datetime.now(UTC)
            if self.active(now):
                try:
                    await self.poll(now)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # keep polling; a gap only loses bolts
                    log.warning("lightning poll failed: %s", e)
            await asyncio.sleep(POLL_S)

    def start(self) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.get_running_loop().create_task(self.run(), name="lightning-poller")

    def stop(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
        self.task = None
