"""Storm motion from recent radar frames → the radar panel's arrival lines, motion arrow and snow lines (SPEC *Radar*).

Inputs are reflectivity grids on one regular lon/lat lattice (MRMS files of the same product share it), cropped to
the same box: the latest frame plus one or two older ones (~15 and ~30 min earlier). Everything else is derived:

- **Motion**: one translation for the whole echo field, from the FFT cross-correlation of the echo fields
  (dBZ above 15, clipped, mean removed) with a parabolic sub-cell peak. Two older frames must agree; one older
  frame needs a clearer correlation peak. No trustworthy motion → no overlays (the callout keeps forecast wording).
- **Arrival lines** (fall board): dashed lines perpendicular to the motion where the storms' leading edge will be at
  the next whole hours ("3 PM / 4 PM / 5 PM"). The edge is the one in a band around home when storms are heading for
  it, else the field's leading edge. Plus the motion arrow with its speed.
- **Snow line** (winter board): the boundary between snow and other precipitation in the latest frame (bold), and
  where it will be later ("SNOW 6 PM"), moved by the boundary's own displacement between frames (a snow line doesn't
  travel with the echoes).
- **Arrival at home**: when the storms' edge (or the snow line) reaches home, for the radar callout
  ("Storms ~4 PM", "22 mi west · ENE 15 mph"; "Snow by 6 PM").

Coordinates are map units (`app.mapgeo`: x east, y south, home near 0, 0); times are absolute. The result is plain
JSON so it travels inside the radar frame (and the cache's last-good copy):

    {"motion": {"vx", "vy", "mph", "heading"},
     "arrival": {"t", "kind": "storm"|"rain"|"snow", "dist_mi", "from", "heading", "mph"} | None,
     "arrivals": [{"t", "x1", "y1", "x2", "y2", "tag": [x, y]}], "arrow": {...} | None,
     "snow_line": [[x, y], ...] | None, "snow_label": [x, y] | None,
     "snow_lines": [{"t", "points": [[x, y], ...], "tag": [x, y]}]}
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, tzinfo

import numpy as np

from . import radar
from .mapgeo import UNITS_PER_MILE, X_MAX, X_MIN, Y_MAX, Y_MIN, Extent, inside

ECHO_DBZ = 15.0  # correlation field: dBZ above this, clipped at +35
CLIP_DB = 35.0
MIN_ECHO_CELLS = 60  # echoes (≥ 20 dBZ) needed in the latest frame for any overlay (≈ 20 mi²)
MAX_MPH = 80.0
MIN_MPH = 3.0
PEAK_ONE = 0.5  # normalized correlation peak needed with a single older frame
PEAK_TWO = 0.35  # … with two frames that must also agree
AGREE_MPH = 8.0
AGREE_DEG = 35.0

STRONG_DBZ, RAIN_DBZ, STORM_DBZ = 30.0, 20.0, 45.0
MIN_STRONG_CELLS = 40
BAND_MI = 8.0  # storms within this distance across the track are "heading for home"
AT_HOME_MI = 2.0
STORM_HORIZON_H = 3.0
SNOW_HORIZON_H = 12.0
MAX_LINES = 3
LINE_GAP = 45.0  # map units between arrival lines (tags must not touch)
FAST_LINE_GAP = 180.0  # map units per hour (≈ 32 mph) above which the lines come every 30 min
TAG_Y = Y_MIN + 53  # tags of near-vertical lines sit on this row (fall board), clear of the legend
TAG_X = X_MIN + 42  # tags of near-horizontal lines sit on this column (winter board)
ARROW_LEN = 70.0
TAG_CLEAR_X, TAG_CLEAR_Y = 40.0, 22.0  # the arrow and its label keep this far from time tags

SNOW_MIN_CELLS = 15  # boundary cells for a snow line
SNOW_BIN = 20.0  # map units along the line per polyline vertex
SNOW_MIN_MPH = 2.0
SNOW_AGREE_MPH = 4.5  # the short (~15 min) baseline is noisy for a slow boundary
SNOW_LINES_HORIZON_H = 6.0
SNOW_MAX_LINES = 2


@dataclass
class Frame:
    """One radar time: dBZ (NaN = no data) and optional type codes (`radar.RAIN` …) on the shared lattice."""

    t: datetime
    dbz: np.ndarray  # nj × ni
    types: np.ndarray | None = None


@dataclass
class Lattice:
    """Cell centers of the shared grid in map units."""

    x: np.ndarray  # nj × ni
    y: np.ndarray
    cell_w: float  # map units per column / row
    cell_h: float

    @classmethod
    def build(cls, ext: Extent, lon1: float, lat1: float, di: float, dj: float, ni: int, nj: int) -> Lattice:
        x0, y0 = ext.project(lon1, lat1)
        x1, y1 = ext.project(lon1 + di, lat1 - dj)
        cw, ch = x1 - x0, y1 - y0
        xs = x0 + cw * np.arange(ni)
        ys = y0 + ch * np.arange(nj)
        x, y = np.meshgrid(xs, ys)
        return cls(x, y, cw, ch)


def dbz_array(raw: list[int], ni: int, nj: int, ref: float, e: int, d: int) -> np.ndarray:
    """Packed GRIB2 values → dBZ; MRMS's −99 (no echo) and −999 (no coverage) become NaN."""
    v = (ref + np.asarray(raw, dtype=np.float64).reshape(nj, ni) * 2.0 ** e) / 10.0 ** d
    v[v < -30] = np.nan
    return v


# ------------------------------------------------------------------ motion


def _field(dbz: np.ndarray) -> np.ndarray:
    f = np.clip(np.nan_to_num(dbz, nan=0.0) - ECHO_DBZ, 0.0, CLIP_DB)
    return f - f.mean()


def shift(a: np.ndarray, b: np.ndarray, max_cells: tuple[int, int]) -> tuple[float, float, float]:
    """(rows, cols, peak): the displacement that best maps field `a` onto `b` (b(p) ≈ a(p − d)), with the
    normalized correlation at the peak (1 = identical shapes)."""
    nj, ni = a.shape
    my, mx = max_cells
    shape = (nj + my, ni + mx)  # zero padding: no wrap-around within the search window
    c = np.fft.irfft2(np.fft.rfft2(b, shape) * np.conj(np.fft.rfft2(a, shape)), shape)
    dys = np.arange(-my, my + 1)
    dxs = np.arange(-mx, mx + 1)
    win = c[np.ix_(dys % shape[0], dxs % shape[1])]
    iy, ix = np.unravel_index(int(np.argmax(win)), win.shape)
    norm = math.sqrt(float((a * a).sum() * (b * b).sum())) or 1.0

    def sub(m1: float, m0: float, p1: float) -> float:
        den = m1 - 2 * m0 + p1
        return 0.0 if den >= 0 else 0.5 * (m1 - p1) / den

    fy = sub(win[iy - 1, ix], win[iy, ix], win[iy + 1, ix]) if 0 < iy < len(dys) - 1 else 0.0
    fx = sub(win[iy, ix - 1], win[iy, ix], win[iy, ix + 1]) if 0 < ix < len(dxs) - 1 else 0.0
    return float(dys[iy] + fy), float(dxs[ix] + fx), float(win[iy, ix]) / norm


@dataclass
class Motion:
    vx: float  # map units per hour
    vy: float

    @property
    def speed(self) -> float:
        return math.hypot(self.vx, self.vy)

    @property
    def mph(self) -> float:
        return self.speed / UNITS_PER_MILE

    @property
    def heading(self) -> str:
        """Compass point the storms move toward ("ENE")."""
        return compass16(math.degrees(math.atan2(self.vx, -self.vy)))


def compass16(bearing: float) -> str:
    names = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return names[round((bearing % 360) / 22.5) % 16]


def compass8_word(bearing: float) -> str:
    names = ["north", "northeast", "east", "southeast", "south", "southwest", "west", "northwest"]
    return names[round((bearing % 360) / 45) % 8]


def _pair_motion(old: Frame, new: Frame, lat: Lattice) -> tuple[Motion, float] | None:
    hours = (new.t - old.t).total_seconds() / 3600
    if hours <= 0:
        return None
    reach = MAX_MPH * UNITS_PER_MILE * hours
    max_cells = (int(reach / abs(lat.cell_h)) + 1, int(reach / abs(lat.cell_w)) + 1)
    a, b = _field(old.dbz), _field(new.dbz)
    if not a.any() or not b.any():
        return None
    dy, dx, peak = shift(a, b, max_cells)
    return Motion(dx * lat.cell_w / hours, dy * lat.cell_h / hours), peak


def estimate(older: list[Frame], latest: Frame, lat: Lattice) -> Motion | None:
    """One motion for the echo field, or None when the frames don't give a trustworthy one."""
    pairs = []
    for f in sorted(older, key=lambda f: f.t):  # oldest first: the longest baseline is the most precise
        p = _pair_motion(f, latest, lat)
        if p is not None:
            pairs.append(p)
    if not pairs:
        return None
    m, peak = pairs[0]
    if len(pairs) == 1:
        ok = peak >= PEAK_ONE
    else:
        m2, peak2 = pairs[1]
        ok = min(peak, peak2) >= PEAK_TWO and _agree(m, m2)
    if not ok or not (MIN_MPH <= m.mph <= MAX_MPH):
        return None
    return m


def _agree(a: Motion, b: Motion) -> bool:
    if abs(a.mph - b.mph) > max(AGREE_MPH, 0.35 * a.mph):
        return False
    da = math.degrees(math.atan2(a.vx, -a.vy) - math.atan2(b.vx, -b.vy))
    return abs((da + 180) % 360 - 180) <= AGREE_DEG


# ------------------------------------------------------------------ geometry helpers


def clip_segment(x1: float, y1: float, x2: float, y2: float, inset: float = 0.0):
    """Liang–Barsky clip to the panel (minus `inset`); None when the segment misses it."""
    xmin, xmax, ymin, ymax = X_MIN + inset, X_MAX - inset, Y_MIN + inset, Y_MAX - inset
    dx, dy = x2 - x1, y2 - y1
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x1 - xmin), (dx, xmax - x1), (-dy, y1 - ymin), (dy, ymax - y1)):
        if p == 0:
            if q < 0:
                return None
            continue
        r = q / p
        if p < 0:
            t0 = max(t0, r)
        else:
            t1 = min(t1, r)
        if t0 > t1:
            return None
    return x1 + t0 * dx, y1 + t0 * dy, x1 + t1 * dx, y1 + t1 * dy


def _in_view(lat: Lattice):
    return (lat.x >= X_MIN) & (lat.x <= X_MAX) & (lat.y >= Y_MIN) & (lat.y <= Y_MAX)


def _r(v: float) -> float:
    return round(float(v), 1)


def _iso(t: datetime) -> str:
    return t.isoformat()


def _times_after(t0: datetime, step_min: int, horizon_h: float, min_lead_h: float):
    """Clock times after t0 (+ min lead) on multiples of `step_min` minutes of the local day, up to the horizon
    (t0 carries the display time zone, so 3-hour steps land on 6 PM / 9 PM)."""
    t = (t0 + timedelta(hours=min_lead_h)).replace(second=0, microsecond=0)
    mins = t.hour * 60 + t.minute
    t += timedelta(minutes=step_min - mins % step_min)
    while (t - t0).total_seconds() / 3600 <= horizon_h:
        yield t
        t += timedelta(minutes=step_min)


def _step(speed: float, steps: tuple[int, ...] = (60, 120, 180)) -> int | None:
    """Minutes between lines so they sit at least LINE_GAP apart (speed in map units per hour)."""
    for step in steps:
        if speed * step / 60 >= LINE_GAP:
            return step
    return None


# ------------------------------------------------------------------ storms (intensity legend)


def storm_overlays(latest: Frame, lat: Lattice, m: Motion, home: tuple[float, float]) -> dict:
    z = np.nan_to_num(latest.dbz, nan=-99.0)
    echo = z >= RAIN_DBZ  # the edge people see: the light-gray fill starts at 15 dBZ
    ux, uy = m.vx / m.speed, m.vy / m.speed
    nx, ny = -uy, ux
    hx, hy = home
    rx, ry = lat.x - hx, lat.y - hy
    s = rx * ux + ry * uy  # along the track (home = 0, positive = downstream)
    c = rx * nx + ry * ny  # across the track
    band = np.abs(c) <= BAND_MI * UNITS_PER_MILE
    dist = np.hypot(rx, ry)
    at_home = bool((echo & (dist <= AT_HOME_MI * UNITS_PER_MILE)).any())
    upstream = echo & band & (s < 0)
    out: dict = {"arrivals": [], "arrow": None, "arrival": None}
    t0 = latest.t
    if upstream.any() and not at_home:
        s_edge = float(s[upstream].max())
        hours = -s_edge / m.speed
        if hours <= STORM_HORIZON_H:
            convective = bool((z[band & (s < 0)] >= STORM_DBZ).any())
            bearing = math.degrees(math.atan2(-ux, uy))  # from home toward the oncoming storms
            out["arrival"] = {"t": _iso(t0 + timedelta(hours=hours)), "kind": "storm" if convective else "rain",
                              "dist_mi": round(-s_edge / UNITS_PER_MILE), "from": compass8_word(bearing),
                              "heading": m.heading, "mph": round(m.mph)}
    else:
        s_edge = None
    if s_edge is None:
        s_edge = float(np.percentile(s[echo], 95))
    step = _step(m.speed)
    if m.speed > FAST_LINE_GAP:  # hourly lines would mostly fall off the panel: every half hour instead
        step = 30
    if step is not None:
        for h in _times_after(t0, step, STORM_HORIZON_H, 10 / 60):
            if len(out["arrivals"]) >= MAX_LINES:
                break
            sh = s_edge + m.speed * (h - t0).total_seconds() / 3600
            px, py = hx + ux * sh, hy + uy * sh
            seg = clip_segment(px - nx * 1000, py - ny * 1000, px + nx * 1000, py + ny * 1000)
            if seg is None:
                continue
            tag = _line_tag(seg, ux, uy)
            if tag is None:
                continue
            out["arrivals"].append({"t": _iso(h), "x1": _r(seg[0]), "y1": _r(seg[1]), "x2": _r(seg[2]),
                                    "y2": _r(seg[3]), "tag": [_r(tag[0]), _r(tag[1])]})
    tags = [tuple(a["tag"]) for a in out["arrivals"]]
    out["arrow"] = _arrow(z, lat, m, home, tags, ux, uy)
    return out


def _line_tag(seg, ux: float, uy: float) -> tuple[float, float] | None:
    x1, y1, x2, y2 = seg
    if abs(ux) >= abs(uy):  # the line runs mostly north–south: tags on a row near the top
        if (y1 - TAG_Y) * (y2 - TAG_Y) <= 0 and y1 != y2:
            t = (TAG_Y - y1) / (y2 - y1)
            x = x1 + t * (x2 - x1)
            if inside(x, 0, 24):
                return x, TAG_Y
    elif (x1 - TAG_X) * (x2 - TAG_X) <= 0 and x1 != x2:
        t = (TAG_X - x1) / (x2 - x1)
        y = y1 + t * (y2 - y1)
        if inside(0, y, 12):
            return TAG_X, y
    # otherwise a point 15 % along the visible part, kept inside the panel
    x, y = x1 + 0.15 * (x2 - x1), y1 + 0.15 * (y2 - y1)
    return (x, y) if inside(x, y, 20) else None


def _blocked(x: float, y: float) -> bool:
    """Places the arrow must avoid: the legend (top left), the callout (bottom right), the panel edge."""
    return (not inside(x, y, 20)) or (x < X_MIN + 274 and y < Y_MIN + 58) or (x > X_MAX - 224 and y > Y_MAX - 118)


def _arrow(z, lat: Lattice, m: Motion, home, tags: list[tuple[float, float]], ux, uy) -> dict | None:
    """The motion arrow on the storms themselves, a little behind their leading edge (fall board), clear of home,
    the legend, the callout, the time tags and the panel edge."""
    hx, hy = home
    core = (z >= STRONG_DBZ) & _in_view(lat)
    if core.sum() < MIN_STRONG_CELLS:
        core = (z >= RAIN_DBZ) & _in_view(lat)
    if not core.any():
        return None
    cx, cy = lat.x[core], lat.y[core]
    s = (cx - hx) * ux + (cy - hy) * uy
    behind = s <= np.percentile(s, 85)
    order = np.argsort(-np.where(behind, s, -1e9))[: 4000: 7]  # nearest the edge first, a sample of cells
    for k in order:
        mx, my = float(cx[k]), float(cy[k])
        tx, ty = mx - ux * ARROW_LEN / 2, my - uy * ARROW_LEN / 2
        ex, ey = mx + ux * ARROW_LEN / 2, my + uy * ARROW_LEN / 2
        if _blocked(tx, ty) or _blocked(ex, ey) or _blocked(tx, ty - 14):
            continue
        if min(math.hypot(tx - hx, ty - hy), math.hypot(ex - hx, ey - hy), math.hypot(mx - hx, my - hy)) < 50:
            continue
        if abs(ux) >= abs(uy):  # label above the tail, like the fall board's "15 mph"
            lx, ly = tx + 2, min(ty, ey) - 10
        else:
            lx, ly = max(tx, ex) + 10, (ty + ey) / 2 + 4
        spots = ((tx, ty), (ex, ey), (mx, my), (lx + 20, ly - 4))
        if any(abs(px - gx) < TAG_CLEAR_X and abs(py - gy) < TAG_CLEAR_Y for px, py in spots for gx, gy in tags):
            continue
        return {"x1": _r(tx), "y1": _r(ty), "x2": _r(ex), "y2": _r(ey), "label": f"{round(m.mph)} mph",
                "lx": _r(lx), "ly": _r(ly), "anchor": "start"}
    return None


# ------------------------------------------------------------------ snow line (types legend)


def _dilate(mask: np.ndarray, r: int) -> np.ndarray:
    out = mask.copy()
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            out |= np.roll(np.roll(mask, dy, 0), dx, 1)
    return out


def _boundary(f: Frame) -> np.ndarray | None:
    """Snow cells next to other precipitation."""
    if f.types is None:
        return None
    z = np.nan_to_num(f.dbz, nan=-99.0)
    snow = (f.types == radar.SNOW) & (z >= radar.SNOW_MIN)
    other = np.isin(f.types, (radar.RAIN, radar.ICE, radar.HAIL)) & (z >= radar.LIGHT)
    b = snow & _dilate(other, 2)
    b[0, :] = b[-1, :] = False
    b[:, 0] = b[:, -1] = False  # np.roll wraps around
    return b


def snow_overlays(latest: Frame, older: list[Frame], lat: Lattice, home: tuple[float, float]) -> dict:
    out: dict = {"snow_line": None, "snow_label": None, "snow_lines": [], "arrival": None}
    b = _boundary(latest)
    if b is None:
        return out
    view = _in_view(lat)
    inview = b & view
    if inview.sum() < SNOW_MIN_CELLS:
        return out
    px, py = lat.x[inview], lat.y[inview]
    mx, my = float(px.mean()), float(py.mean())
    cov = np.cov(np.vstack([px - mx, py - my]))
    w, v = np.linalg.eigh(cov)
    dx, dy = v[:, int(np.argmax(w))]
    if dx < 0:
        dx, dy = -dx, -dy
    nx, ny = -dy, dx
    z = np.nan_to_num(latest.dbz, nan=-99.0)
    snow = (latest.types == radar.SNOW) & (z >= radar.SNOW_MIN)
    near = np.hypot(lat.x - mx, lat.y - my) <= 120
    u_all = (lat.x - mx) * nx + (lat.y - my) * ny
    if snow[near].any() and (~snow & (z >= radar.LIGHT))[near].any():
        if u_all[near & snow].mean() > u_all[near & ~snow & (z >= radar.LIGHT)].mean():
            nx, ny = -nx, -ny  # n̂ points from the snow toward the other precipitation
    t = (px - mx) * dx + (py - my) * dy
    u = (px - mx) * nx + (py - my) * ny
    verts = []
    for lo in np.arange(math.floor(t.min() / SNOW_BIN) * SNOW_BIN, t.max() + SNOW_BIN, SNOW_BIN):
        sel = (t >= lo) & (t < lo + SNOW_BIN)
        if sel.sum() >= 2:
            verts.append((float(t[sel].mean()), float(np.median(u[sel]))))
    if len(verts) < 3:
        return out
    us = [uv for _, uv in verts]
    smooth = [float(np.mean(us[max(0, i - 1):i + 2])) for i in range(len(us))]  # 3-vertex moving average
    line = [(mx + dx * tv + nx * uv, my + dy * tv + ny * uv) for (tv, _), uv in zip(verts, smooth, strict=True)]

    def poly(points) -> list[list[float]]:
        return [[_r(x), _r(y)] for x, y in points if inside(x, y)]

    cur = poly(line)
    if len(cur) < 2:
        return out
    out["snow_line"] = cur
    i = int(len(cur) * 0.8) if len(cur) > 2 else len(cur) - 1
    out["snow_label"] = [_r(cur[i][0]), _r(cur[i][1] - 8)]
    # the line's own motion along n̂ (positive: snow advancing over the other precipitation)
    speeds = []
    for f in sorted(older, key=lambda f: f.t):
        ob = _boundary(f)
        hours = (latest.t - f.t).total_seconds() / 3600
        if ob is None or hours <= 0:
            continue
        ob = ob & view  # the same window as the latest line, or the medians compare different stretches of it
        ox, oy = lat.x[ob], lat.y[ob]
        ot = (ox - mx) * dx + (oy - my) * dy
        keep = (ot >= t.min()) & (ot <= t.max())
        if keep.sum() < SNOW_MIN_CELLS:
            continue
        ou = (ox[keep] - mx) * nx + (oy[keep] - my) * ny
        speeds.append((float(np.median(u)) - float(np.median(ou))) / hours)
    if not speeds:
        return out
    ws = speeds[0]
    if len(speeds) > 1:
        tol = max(SNOW_AGREE_MPH * UNITS_PER_MILE, 0.6 * abs(ws))
        if ws * speeds[1] < 0 or abs(ws - speeds[1]) > tol:
            return out
    if abs(ws) < SNOW_MIN_MPH * UNITS_PER_MILE:
        return out
    step = _step(abs(ws)) or 180
    t0 = latest.t
    for h in _times_after(t0, step, SNOW_LINES_HORIZON_H, 0.5):
        if len(out["snow_lines"]) >= SNOW_MAX_LINES:
            break
        k = ws * (h - t0).total_seconds() / 3600
        pts = poly((x + nx * k, y + ny * k) for x, y in line)
        if len(pts) < 2:
            continue
        # the whole tag (≤ 80 units wide) inside the panel, off the legend and the callout
        clear = [p for p in pts if inside(p[0], 0, 40) and not _blocked(*p)] or pts
        tag = min(clear, key=lambda p: abs(p[0] - TAG_X)) if abs(dx) >= abs(dy) else min(clear, key=lambda p: p[1])
        if not inside(tag[0], tag[1], 16):
            tag = clear[len(clear) // 2]
        out["snow_lines"].append({"t": _iso(h), "points": pts, "tag": [tag[0], tag[1]]})
    # arrival at home: home on the far side of the line, the line advancing toward it
    hx, hy = home
    home_snow = bool((snow & (np.hypot(lat.x - hx, lat.y - hy) <= AT_HOME_MI * UNITS_PER_MILE)).any())
    th = (hx - mx) * dx + (hy - my) * dy
    uh = (hx - mx) * nx + (hy - my) * ny
    tv = [p[0] for p in verts]
    if not home_snow and ws > 0 and tv[0] - SNOW_BIN <= th <= tv[-1] + SNOW_BIN:
        gap = uh - float(np.interp(th, tv, smooth))
        hours = gap / ws
        if 0 < hours <= SNOW_HORIZON_H:
            bearing = math.degrees(math.atan2(-nx, ny))
            out["arrival"] = {"t": _iso(t0 + timedelta(hours=hours)), "kind": "snow",
                              "dist_mi": round(gap / UNITS_PER_MILE), "from": compass8_word(bearing),
                              "heading": compass16(math.degrees(math.atan2(nx, -ny))),
                              "mph": round(ws / UNITS_PER_MILE)}
    return out


# ------------------------------------------------------------------ entry point


def analyze(latest: Frame, older: list[Frame], lat: Lattice, home: tuple[float, float], legend: str,
            tz: tzinfo | None = None) -> dict | None:
    """Overlays + arrival for the radar frame, or None when there's nothing trustworthy to draw. Line times
    follow the clock of `tz` (the display time zone)."""
    if tz is not None:
        latest = replace(latest, t=latest.t.astimezone(tz))
        older = [replace(f, t=f.t.astimezone(tz)) for f in older]
    z = np.nan_to_num(latest.dbz, nan=-99.0)
    if (z >= RAIN_DBZ).sum() < MIN_ECHO_CELLS or not older:
        return None
    if legend == "types":  # winter board: snow lines only
        res = snow_overlays(latest, older, lat, home)
        if res["snow_line"] is None:
            return None
        return {"motion": None, **res, "arrivals": [], "arrow": None}
    m = estimate(older, latest, lat)
    if m is None:
        return None
    res = storm_overlays(latest, lat, m, home)
    return {"motion": {"vx": _r(m.vx), "vy": _r(m.vy), "mph": _r(m.mph), "heading": m.heading}, **res,
            "snow_line": None, "snow_label": None, "snow_lines": []}
