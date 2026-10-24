"""Station history: recent outdoor temperature and pressure, recorded after each live resolve.

Feeds the `history` provider (24 h temperature chart, 3 h pressure trend). Stored as one small JSON file in the
cache dir, `history.json`:

    {"version": 1, "series": {"outdoor.temperature": [[<unix seconds>, 58.4], ...], "pressure.sea_level": [...]}}

Only winning readings with status ok are recorded, at their own `observed_at`, at most one point per
`MIN_SPACING` per series, and points older than `KEEP` are dropped. A missing or corrupt file starts empty.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .readings import Status
from .resolver import Resolved

log = logging.getLogger(__name__)

RECORDED = ("outdoor.temperature", "pressure.sea_level")
KEEP = timedelta(hours=48)
MIN_SPACING = timedelta(minutes=5)
FILE_NAME = "history.json"
FILE_VERSION = 1

# Coverage rules for the 24 h temperature chart.
CHART_WINDOW = timedelta(hours=24)
CHART_MIN_SPAN = timedelta(hours=20)
CHART_MAX_GAP = timedelta(hours=2)
CHART_STEP = timedelta(minutes=10)  # at most one chart point per 10 min
# Pressure trend: change versus the point nearest to 3 h before the latest one, within the tolerance.
TREND_SPAN = timedelta(hours=3)
TREND_TOLERANCE = timedelta(minutes=30)


@dataclass(frozen=True)
class Point:
    t: datetime  # timezone-aware (UTC)
    v: float


class HistoryStore:
    def __init__(self, path: Path | None = None, keep: timedelta = KEEP):
        self.path = Path(path) if path is not None else None
        self.keep = keep
        self._series: dict[str, list[Point]] = {f: [] for f in RECORDED}
        self._load()

    # ------------------------------------------------------------ recording

    def record(self, resolved: Mapping[str, Resolved], now: datetime | None = None) -> int:
        """Record the winning ok values of the recorded fields. Returns how many points were added."""
        added = 0
        for name in RECORDED:
            r = resolved.get(name)
            if r is None or r.reading.status is not Status.ok or r.reading.observed_at is None:
                continue
            try:
                v = float(r.reading.value)
            except (TypeError, ValueError):
                continue
            added += self.add(name, r.reading.observed_at, v)
        if added:
            self._prune(now or datetime.now(UTC))
            self.save()
        return added

    def add(self, name: str, t: datetime, v: float) -> bool:
        """Append a point if it is newer than the last one by at least MIN_SPACING (no save)."""
        if t.tzinfo is None:
            raise ValueError("history points need a timezone-aware time")
        pts = self._series.setdefault(name, [])
        t = t.astimezone(UTC)
        if pts and t - pts[-1].t < MIN_SPACING:
            return False
        pts.append(Point(t, round(v, 3)))
        return True

    def _prune(self, now: datetime) -> None:
        cutoff = now - self.keep
        for name, pts in self._series.items():
            self._series[name] = [p for p in pts if p.t >= cutoff]

    # ------------------------------------------------------------ queries

    def series(self, name: str, since: datetime | None = None, until: datetime | None = None) -> list[Point]:
        return [p for p in self._series.get(name, []) if (since is None or p.t >= since)
                and (until is None or p.t <= until)]

    def chart(self, now: datetime) -> tuple[list[Point], str | None]:
        """Temperature points for the last 24 h (oldest first, ≥ 10 min apart), or ([], reason) if coverage is thin.

        Adequate coverage: the points span at least CHART_MIN_SPAN with no gap longer than CHART_MAX_GAP.
        """
        pts = self.series("outdoor.temperature", since=now - CHART_WINDOW, until=now)
        if len(pts) < 2:
            return [], "insufficient_history"
        if pts[-1].t - pts[0].t < CHART_MIN_SPAN:
            return [], "insufficient_history"
        if any(b.t - a.t > CHART_MAX_GAP for a, b in zip(pts, pts[1:], strict=False)):
            return [], "insufficient_history"
        out = [pts[0]]
        for p in pts[1:]:
            if p.t - out[-1].t >= CHART_STEP:
                out.append(p)
        out[-1] = pts[-1]  # end on the newest point (it is < 10 min after the last kept one, so spacing holds)
        return out, None

    def pressure_trend(self, now: datetime) -> tuple[float | None, datetime | None, str | None]:
        """(change in inHg over ~3 h, time of the latest point, reason when unavailable)."""
        pts = self.series("pressure.sea_level", until=now)
        if not pts:
            return None, None, "insufficient_history"
        latest = pts[-1]
        target = latest.t - TREND_SPAN
        best = min(pts[:-1], key=lambda p: abs(p.t - target), default=None)
        if best is None or abs(best.t - target) > TREND_TOLERANCE:
            return None, latest.t, "insufficient_history"
        return round(latest.v - best.v, 3), latest.t, None

    def summary(self) -> dict[str, dict]:
        """Per series: point count and first/last times (for /status)."""
        out = {}
        for name, pts in self._series.items():
            out[name] = {"points": len(pts), "first": pts[0].t.isoformat() if pts else None,
                         "last": pts[-1].t.isoformat() if pts else None,
                         "last_value": pts[-1].v if pts else None}
        return out

    # ------------------------------------------------------------ persistence

    def save(self) -> None:
        if self.path is None:
            return
        data = {"version": FILE_VERSION,
                "series": {n: [[int(p.t.timestamp()), p.v] for p in pts] for n, pts in self._series.items()}}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as e:
            log.warning("history: could not save %s: %s", self.path, e)

    def _load(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("version") != FILE_VERSION:
                raise ValueError(f"unknown version {data.get('version')!r}")
            series: dict[str, list[Point]] = {}
            for name, rows in data["series"].items():
                pts = sorted((Point(datetime.fromtimestamp(float(t), UTC), float(v)) for t, v in rows),
                             key=lambda p: p.t)
                series[name] = pts
        except Exception as e:
            log.warning("history: ignoring unreadable %s: %s", self.path, e)
            return
        self._series.update(series)


_STORES: dict[str, HistoryStore] = {}


def get_store(directory: Path) -> HistoryStore:
    """The shared store for a cache dir (the app and the `history` provider use the same instance)."""
    path = (Path(directory) / FILE_NAME).resolve()
    store = _STORES.get(str(path))
    if store is None:
        store = _STORES[str(path)] = HistoryStore(path)
    return store
