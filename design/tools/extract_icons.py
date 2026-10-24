"""Split the approved icon sheet (design/source/icons.html) into one SVG file per icon.

Output goes to design/icons/<group>/<name>.svg. Each file keeps the icon's
original viewBox and drops the fixed width/height so the renderer can size it.

Usage: python design/tools/extract_icons.py
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
SHEET = ROOT / "source" / "icons.html"
OUT = ROOT / "icons"

# Markers and fills are small one-offs on the sheet; they are listed here
# explicitly (same geometry as the sheet) rather than scraped.
MARKERS = {
    "precip-rain": ("0 0 9 12", '<path d="M4.5 0 C4.5 0 0 5.5 0 8 A4.5 4.5 0 0 0 9 8 C9 5.5 4.5 0 4.5 0 Z" fill="#000000"/>'),
    "precip-snow": ("0 0 12 12", '<path d="M6 1 V11 M1.7 3.5 L10.3 8.5 M1.7 8.5 L10.3 3.5" stroke="#000000" stroke-width="1.7" stroke-linecap="round"/>'),
    "precip-ice": ("0 0 10 10", '<circle cx="5" cy="5" r="3.5" fill="#000000"/>'),
    "pressure-rising": ("0 0 14 14", '<path d="M7 12 V2 M3 6 L7 2 L11 6" fill="none" stroke="#000000" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>'),
    "pressure-steady": ("0 0 14 14", '<path d="M2 7 H12 M8 3 L12 7 L8 11" fill="none" stroke="#000000" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>'),
    "pressure-falling": ("0 0 14 14", '<path d="M7 2 V12 M3 8 L7 12 L11 8" fill="none" stroke="#000000" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>'),
    "sun-on-arc": ("-17 -17 34 34", '<path d="M0,-8.5 V-12.5 M0,8.5 V12.5 M-8.5,0 H-12.5 M8.5,0 H12.5 M-6,-6 L-8.8,-8.8 M6,-6 L8.8,-8.8 M-6,6 L-8.8,8.8 M6,6 L8.8,8.8" stroke="#000000" stroke-width="1.8" stroke-linecap="round"/><circle r="5.5" fill="#000000"/>'),
    "map-home": ("0 0 14 14", '<rect x="1" y="1" width="12" height="12" fill="#000000"/>'),
    "map-city": ("0 0 10 10", '<circle cx="5" cy="5" r="4" fill="#000000"/>'),
    "alert-triangle": ("0 0 12 11", '<path d="M6 0 L12 11 H0 Z" fill="#000000"/><path d="M6 4 V7.5" stroke="#FFFFFF" stroke-width="1.6"/>'),
    # Map lightning (added after icon set v1, approved 2026-10-24): same bolt as the storm icon.
    "lightning-recent": ("16 18.5 22 31", '<path d="M26 21 H35 L28 31 H34 L22 47 L26 35 H19 Z" fill="#000000" stroke="#FFFFFF" stroke-width="2.5" stroke-linejoin="round" paint-order="stroke"/>'),
    "lightning-older": ("16 18.5 22 31", '<path d="M26 21 H35 L28 31 H34 L22 47 L26 35 H19 Z" fill="#FFFFFF" stroke="#000000" stroke-width="2" stroke-linejoin="round"/>'),
}

FILLS = {
    "rain-light": '<rect width="48" height="32" fill="#DBDBDB"/>',
    "rain-moderate": '<rect width="48" height="32" fill="#929292"/>',
    "rain-heavy": '<rect width="48" height="32" fill="#494949"/>',
    "rain-extreme": '<rect width="48" height="32" fill="#000000"/>',
    "ice": '<defs><pattern id="p" width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><line x1="0" y1="0" x2="0" y2="5" stroke="#000000" stroke-width="1.6"/></pattern></defs><rect width="48" height="32" fill="#FFFFFF"/><rect width="48" height="32" fill="url(#p)"/>',
    "snow": '<defs><pattern id="p" width="6" height="6" patternUnits="userSpaceOnUse"><circle cx="3" cy="3" r="1.2" fill="#000000"/></pattern></defs><rect width="48" height="32" fill="#FFFFFF"/><rect width="48" height="32" fill="url(#p)"/>',
    "snow-heavy": '<defs><pattern id="p" width="3.6" height="3.6" patternUnits="userSpaceOnUse"><rect width="3.6" height="3.6" fill="#B6B6B6"/><circle cx="1.8" cy="1.8" r="1.2" fill="#000000"/></pattern></defs><rect width="48" height="32" fill="url(#p)"/>',
}


def slug(text: str) -> str:
    text = text.replace("­", "").replace("&amp;", "and").replace("+", " ")
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def to_xml(inner: str) -> str:
    # HTML-style <x></x> pairs for empty SVG elements are valid XML already;
    # only make sure the namespace is declared.
    return inner.strip()


def write(group: str, name: str, viewbox: str, inner: str) -> None:
    path = OUT / group / f"{name}.svg"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{viewbox}">{to_xml(inner)}</svg>\n',
        encoding="utf-8",
        newline="\n",
    )


def section(html: str, start: str, end: str | None) -> str:
    a = html.index(start)
    b = html.index(end, a) if end else len(html)
    return html[a:b]


def main() -> None:
    html = SHEET.read_text(encoding="utf-8")
    count = 0

    cond = section(html, ">Conditions</span>", ">Modifiers")
    for inner, label in re.findall(
        r'<svg width="64" height="64" viewBox="0 0 48 48" aria-hidden="true">(.*?)</svg>.*?line-height: 1.2">(.*?)</span>',
        cond, re.S):
        write("conditions", slug(label), "0 0 48 48", inner)
        count += 1

    mods = section(html, ">Modifiers", ">Moon phases")
    for inner, label in re.findall(
        r'<svg width="64" height="64" viewBox="0 0 48 48" aria-hidden="true">(.*?)</svg>\s*<span[^>]*>(.*?)</span>',
        mods, re.S):
        write("modifiers", slug(label), "0 0 48 48", inner)
        count += 1

    moon = section(html, ">Moon phases", ">Wind direction")
    for inner, label in re.findall(
        r'<svg width="36" height="36" viewBox="-14 -14 28 28" aria-hidden="true">(.*?)</svg><span[^>]*>(.*?)</span>',
        moon, re.S):
        write("moon", slug(label), "-14 -14 28 28", inner)
        count += 1

    wind = section(html, ">Wind direction", ">Small markers")
    for inner, label in re.findall(
        r'<svg width="22" height="22" viewBox="0 0 14 14" aria-hidden="true">(.*?)</svg><span[^>]*>(.*?)</span>',
        wind, re.S):
        write("wind", f"from-{slug(label)}", "0 0 14 14", inner)
        count += 1

    for name, (vb, inner) in MARKERS.items():
        write("markers", name, vb, inner)
        count += 1
    for name, inner in FILLS.items():
        write("fills", name, "0 0 48 32", inner)
        count += 1

    print(f"wrote {count} icons to {OUT}")


if __name__ == "__main__":
    main()
