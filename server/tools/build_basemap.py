"""Build the radar base map (`server/assets/basemap/basemap.geojson`) from US Census TIGER/Line files.

Run once (or when the extent changes); the output is committed, so the server never downloads map data:

    server/.venv/Scripts/python.exe -m pip install pyshp      # or: pip install ".[basemap]"
    server/.venv/Scripts/python.exe server/tools/build_basemap.py [--lat 40.0617 --lon -88.2517]

Sources (public domain, 2023 vintage), cached in `server/tools/.cache/`:
- cartographic boundaries 1:500k: states (home state outline → dashed state line), counties (light lines)
- TIGER primary/secondary roads per state: interstates only (`RTTYP = I`)
- TIGER area water per county in the extent: lakes ≥ 2 km² (gray fills)
- cartographic places 1:500k: the labeled towns in PLACES (dot at the polygon's centroid)

Everything is clipped to the display extent (`app.mapgeo`) plus a margin, simplified to ~0.4 map units
(½ px), and stored as lon/lat GeoJSON with a `layer` property per feature.
"""
from __future__ import annotations

import argparse
import io
import json
import math
import sys
import urllib.request
import zipfile
from pathlib import Path

import shapefile  # pyshp

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
from app.mapgeo import X_MAX, X_MIN, Y_MAX, Y_MIN, Extent  # noqa: E402

CACHE = SERVER / "tools" / ".cache"
OUT = SERVER / "assets" / "basemap" / "basemap.geojson"
YEAR = 2023
GENZ = f"https://www2.census.gov/geo/tiger/GENZ{YEAR}/shp"
TIGER = f"https://www2.census.gov/geo/tiger/TIGER{YEAR}"

MARGIN = 12.0  # map units beyond the viewBox, so strokes run off the edge
TOLERANCE = 0.4  # Douglas–Peucker, map units
MIN_LAKE_M2 = 2e6
HOME_STATE = "17"  # Illinois; its outline is the dashed state line

# Labeled towns (the approved board's, plus the v3 additions): name, state FIPS, dot radius (0 = label only),
# label dx, dy, anchor.
PLACES = [
    ("Champaign", "17", 0, 4, -6, "start"),
    ("Rantoul", "17", 3.2, 6, 4, "start"),
    ("Paxton", "17", 3.2, 6, 4, "start"),
    ("Danville", "17", 3.6, -6, -7, "end"),
    ("Monticello", "17", 3.2, 0, -8, "middle"),
    ("Clinton", "17", 3.2, 5, -8, "start"),
    ("Bloomington", "17", 3.6, 7, 4, "start"),
    ("Tuscola", "17", 3.2, 7, 4, "start"),
    ("Mattoon", "17", 3.6, -7, 4, "end"),  # left of the dot, clear of Charleston
    # layout v3 (wider, extending west)
    ("Decatur", "17", 3.6, 7, 4, "start"),
    ("Taylorville", "17", 3.2, 6, 4, "start"),
    ("Shelbyville", "17", 3.2, 6, 4, "start"),
    ("Charleston", "17", 3.2, 6, 4, "start"),
    ("Paris", "17", 3.2, -6, 4, "end"),
]


def fetch(url: str) -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / url.rsplit("/", 1)[1]
    if not path.exists():
        print("download", url, file=sys.stderr)
        req = urllib.request.Request(url, headers={"User-Agent": "InkplateWeather basemap build"})
        with urllib.request.urlopen(req, timeout=120) as r:
            path.write_bytes(r.read())
    return path


def reader(url: str) -> shapefile.Reader:
    z = zipfile.ZipFile(fetch(url))
    stem = next(n[:-4] for n in z.namelist() if n.endswith(".shp"))
    return shapefile.Reader(shp=io.BytesIO(z.read(stem + ".shp")), shx=io.BytesIO(z.read(stem + ".shx")),
                            dbf=io.BytesIO(z.read(stem + ".dbf")))


# --- geometry in map units ------------------------------------------------------------------------------------

Pt = tuple[float, float]
X0, Y0, X1, Y1 = X_MIN - MARGIN, Y_MIN - MARGIN, X_MAX + MARGIN, Y_MAX + MARGIN


def parts(shape) -> list[list[tuple[float, float]]]:
    idx = list(shape.parts) + [len(shape.points)]
    return [shape.points[idx[i]:idx[i + 1]] for i in range(len(idx) - 1)]


def clip_segment(a: Pt, b: Pt) -> tuple[Pt, Pt] | None:
    """Liang–Barsky against the margin rectangle."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, a[0] - X0), (dx, X1 - a[0]), (-dy, a[1] - Y0), (dy, Y1 - a[1])):
        if p == 0:
            if q < 0:
                return None
            continue
        t = q / p
        if p < 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t0 > t1:
            return None
    p0 = a if t0 == 0 else (a[0] + t0 * dx, a[1] + t0 * dy)
    p1 = b if t1 == 1 else (a[0] + t1 * dx, a[1] + t1 * dy)
    return p0, p1


def clip_line(pts: list[Pt]) -> list[list[Pt]]:
    out: list[list[Pt]] = []
    cur: list[Pt] = []
    for a, b in zip(pts, pts[1:], strict=False):
        seg = clip_segment(a, b)
        if seg is None:
            if cur:
                out.append(cur)
                cur = []
            continue
        p, q = seg
        if not cur:
            cur = [p]
        elif cur[-1] != p:
            out.append(cur)
            cur = [p]
        cur.append(q)
        if q != b:  # left the rectangle
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return [line for line in out if len(line) > 1]


def clip_polygon(ring: list[Pt]) -> list[Pt]:
    """Sutherland–Hodgman against the margin rectangle."""
    edges = (
        (lambda p: p[0] >= X0, lambda a, b: (X0, a[1] + (b[1] - a[1]) * (X0 - a[0]) / (b[0] - a[0]))),
        (lambda p: p[0] <= X1, lambda a, b: (X1, a[1] + (b[1] - a[1]) * (X1 - a[0]) / (b[0] - a[0]))),
        (lambda p: p[1] >= Y0, lambda a, b: (a[0] + (b[0] - a[0]) * (Y0 - a[1]) / (b[1] - a[1]), Y0)),
        (lambda p: p[1] <= Y1, lambda a, b: (a[0] + (b[0] - a[0]) * (Y1 - a[1]) / (b[1] - a[1]), Y1)),
    )
    pts = ring
    for inside, cross in edges:
        if not pts:
            break
        src, pts = pts, []
        for i, cur in enumerate(src):
            prev = src[i - 1]
            if inside(cur):
                if not inside(prev):
                    pts.append(cross(prev, cur))
                pts.append(cur)
            elif inside(prev):
                pts.append(cross(prev, cur))
    return pts


def simplify(pts: list[Pt], tol: float = TOLERANCE) -> list[Pt]:
    """Douglas–Peucker (iterative)."""
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        (ax, ay), (bx, by) = pts[i], pts[j]
        dx, dy = bx - ax, by - ay
        norm = math.hypot(dx, dy)
        best, best_d = -1, tol
        for k in range(i + 1, j):
            px, py = pts[k]
            d = abs(dy * (px - ax) - dx * (py - ay)) / norm if norm else math.hypot(px - ax, py - ay)
            if d > best_d:
                best, best_d = k, d
        if best > 0:
            keep[best] = True
            stack += [(i, best), (best, j)]
    return [p for p, k in zip(pts, keep, strict=True) if k]


def centroid(ring: list[Pt]) -> Pt:
    a = cx = cy = 0.0
    for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1], strict=True):
        c = x0 * y1 - x1 * y0
        a += c
        cx += (x0 + x1) * c
        cy += (y0 + y1) * c
    if abs(a) < 1e-12:
        return sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring)
    return cx / (3 * a), cy / (3 * a)


def ring_area(ring: list[Pt]) -> float:
    return abs(sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1], strict=True))) / 2


# --- layers -----------------------------------------------------------------------------------------------------

class Builder:
    def __init__(self, ext: Extent):
        self.ext = ext
        self.features: list[dict] = []
        w, s, e, n = ext.bbox(MARGIN)
        self.bbox = (w, s, e, n)

    def proj(self, pts) -> list[Pt]:
        return [self.ext.project(lon, lat) for lon, lat in pts]

    def lonlat(self, pts: list[Pt]) -> list[list[float]]:
        return [[round(c, 5) for c in self.ext.unproject(x, y)] for x, y in pts]

    def hits(self, shape) -> bool:
        w, s, e, n = self.bbox
        bx0, by0, bx1, by1 = shape.bbox
        return not (bx1 < w or bx0 > e or by1 < s or by0 > n)

    def add_lines(self, layer: str, shapes, props: dict | None = None) -> int:
        """One MultiLineString feature from one shape or a list of them (e.g. every piece of one interstate)."""
        lines = []
        for part in (pt for shape in (shapes if isinstance(shapes, list) else [shapes]) for pt in parts(shape)):
            for line in clip_line(self.proj(part)):
                line = simplify(line)
                if len(line) > 1:
                    lines.append(self.lonlat(line))
        if lines:
            self.features.append({"type": "Feature", "properties": {"layer": layer, **(props or {})},
                                  "geometry": {"type": "MultiLineString", "coordinates": lines}})
        return len(lines)

    def add_polygon(self, layer: str, shape, props: dict | None = None) -> int:
        rings = []
        for part in parts(shape):
            ring = clip_polygon(self.proj(part[:-1] if part[0] == part[-1] else part))
            ring = simplify(ring + ring[:1])
            if len(ring) > 3 and ring_area(ring) > 1.0:
                rings.append(self.lonlat(ring))
        if rings:
            self.features.append({"type": "Feature", "properties": {"layer": layer, **(props or {})},
                                  "geometry": {"type": "MultiPolygon", "coordinates": [[r] for r in rings]}})
        return len(rings)


def build(ext: Extent) -> dict:
    b = Builder(ext)
    counties = reader(f"{GENZ}/cb_{YEAR}_us_county_500k.zip")
    fips = []
    for sr in counties.iterShapeRecords():
        if b.hits(sr.shape) and b.add_lines("county", sr.shape, {"name": sr.record["NAME"]}):
            fips.append(sr.record["GEOID"])
    states = sorted({f[:2] for f in fips})
    print("counties", len(fips), "states", states, file=sys.stderr)

    for sr in reader(f"{GENZ}/cb_{YEAR}_us_state_500k.zip").iterShapeRecords():
        if sr.record["STATEFP"] == HOME_STATE:
            b.add_lines("state", sr.shape, {"name": sr.record["NAME"]})

    for geoid in fips:
        for sr in reader(f"{TIGER}/AREAWATER/tl_{YEAR}_{geoid}_areawater.zip").iterShapeRecords():
            if sr.record["AWATER"] >= MIN_LAKE_M2 and b.hits(sr.shape):
                b.add_polygon("water", sr.shape, {"name": sr.record["FULLNAME"] or ""})

    for st in states:
        roads: dict[str, list] = {}
        for sr in reader(f"{TIGER}/PRISECROADS/tl_{YEAR}_{st}_prisecroads.zip").iterShapeRecords():
            if sr.record["RTTYP"] == "I" and b.hits(sr.shape):
                roads.setdefault(sr.record["FULLNAME"].replace("I- ", "I-"), []).append(sr.shape)
        for ref, shapes in sorted(roads.items()):
            b.add_lines("interstate", shapes, {"ref": ref})

    wanted = {(n, st): (r, dx, dy, anchor) for n, st, r, dx, dy, anchor in PLACES}
    found = set()
    for st in sorted({st for _, st, *_ in PLACES}):
        for sr in reader(f"{GENZ}/cb_{YEAR}_{st}_place_500k.zip").iterShapeRecords():
            key = (sr.record["NAME"], st)
            if key not in wanted or key in found:
                continue
            ring = max((b.proj(p) for p in parts(sr.shape)), key=ring_area)
            x, y = centroid(ring)
            r, dx, dy, anchor = wanted[key]
            found.add(key)
            b.features.append({"type": "Feature",
                               "properties": {"layer": "place", "name": key[0], "dot": r, "dx": dx, "dy": dy,
                                              "anchor": anchor},
                               "geometry": {"type": "Point", "coordinates": b.lonlat([(x, y)])[0]}})
    missing = set(wanted) - found
    if missing:
        raise SystemExit(f"places not found: {sorted(missing)}")

    order = {"water": 0, "county": 1, "state": 2, "interstate": 3, "place": 4}
    b.features.sort(key=lambda f: order[f["properties"]["layer"]])
    return {"type": "FeatureCollection",
            "properties": {"center": [ext.lon, ext.lat], "source": f"US Census TIGER/Line {YEAR} (public domain)",
                           "built_by": "server/tools/build_basemap.py"},
            "features": b.features}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--lat", type=float, default=40.0617)
    ap.add_argument("--lon", type=float, default=-88.2517)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()
    data = build(Extent(args.lat, args.lon))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, separators=(",", ":"))
    args.out.write_text(text + "\n", encoding="utf-8", newline="\n")
    counts: dict[str, int] = {}
    for f in data["features"]:
        counts[f["properties"]["layer"]] = counts.get(f["properties"]["layer"], 0) + 1
    print(f"wrote {args.out} ({len(text) // 1024} KB): {counts}", file=sys.stderr)


if __name__ == "__main__":
    main()
