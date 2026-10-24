"""Readability pass on the reflowed boards (requested 2026-10-24): margin −3 px per side, small text larger.

Applied to source/dashboard.html and dashboard-winter.html after reflow_margin.py. Kept for the record; running it
again on already-updated boards fails its own checks.

  margin   69/71 → 66/68 px, header→main gap 12 → 11; the 6 px gained go to the radar column (530 → 536) and the main area height
  radar    536 × 570 at the same map scale (viewBox −214.4 −228 428.8 456)
  HTML     11 → 13 px: readings labels (tracking 0.1 → 0.08 em, cell padding 12 → 8, no wrapping), gauge, sun, moon, key;
           12 → 13 px; 12.5 → 13.5 px (readings' secondary values stay 13 px: 14 px wraps them)
  charts   effective sizes ≈ 12–12.5 px: trend axis/day labels 11/11.5 → 13 units, point labels 12 → 13.5;
           precip % labels 10.5 → 12 (winter 9.5 → 11.5), hours 11 → 12.5; sun day length 11 → 12.5
  gauge    winter labels "Snow depth" / "Season snow" (the heading credits NWS); note "in · snow via NWS"
  moon     next-phase strip as a 2 × 2 grid so "Full 10/26" / "First 11/17" no longer wrap (gaps 6 → 3, rows 0)
  spacing  right-column section gaps 8 → 7 (winter's 7-day rows stay ≥ 24 px)

Usage: python design/tools/readability_v2.py
"""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def sub(s: str, old: str, new: str, count: int | None = None) -> str:
    n = s.count(old)
    if n == 0 or (count is not None and n != count):
        raise SystemExit(f"expected {count or '≥1'} × {old[:80]!r}, found {n}")
    return s.replace(old, new)


def in_svg(s: str, opening: str, pairs: list[tuple[str, str]]) -> str:
    start = s.index(opening)
    end = s.index("</svg>", start)
    block = s[start:end]
    for old, new in pairs:
        if old not in block:
            raise SystemExit(f"{old!r} not in svg {opening[:40]!r}")
        block = block.replace(old, new)
    return s[:start] + block + s[end:]


def update(s: str, winter: bool) -> str:
    # Margin and the space it frees.
    s = sub(s, "padding: 69px 71px;", "padding: 66px 68px;", 1)
    s = sub(s, "flex-direction: column; gap: 12px; overflow: hidden", "flex-direction: column; gap: 11px; overflow: hidden", 1)
    s = sub(s, "grid-template-columns: 530px 504px", "grid-template-columns: 536px 504px", 1)
    s = sub(s, "position: relative; width: 530px; height: 567px", "position: relative; width: 536px; height: 570px", 1)
    s = sub(s, 'width="530" height="567" viewBox="-212 -226.8 424 453.6"',
            'width="536" height="570" viewBox="-214.4 -228 428.8 456"', 1)
    s = sub(s, '<rect x="-212" y="-226.8" width="424" height="453.6" fill="#FFFFFF">',
            '<rect x="-214.4" y="-228" width="428.8" height="456" fill="#FFFFFF">', 1)
    # Readings strip: labels 11 → 12 px; tighter cell padding so the larger labels fit.
    s = sub(s, "font-size: 11px; font-weight: 800; letter-spacing: 0.1em; text-transform: uppercase",
            "font-size: 13px; font-weight: 800; letter-spacing: 0.08em; text-transform: uppercase", 5)
    s = sub(s, "gap: 1px; padding-right: 12px", "gap: 1px; padding-right: 8px; white-space: nowrap", 1)
    s = sub(s, "gap: 1px; padding: 0 12px; border-left", "gap: 1px; padding: 0 8px; white-space: nowrap; border-left", 3)
    s = sub(s, "gap: 1px; padding-left: 12px; border-left", "gap: 1px; padding-left: 8px; white-space: nowrap; border-left", 1)
    # Other small HTML text.
    s = sub(s, "font-size: 11px; color: #494949", "font-size: 13px; color: #494949")  # gauge labels, precip key
    s = sub(s, "font-size: 11px; font-weight: 400; color: #494949", "font-size: 13px; font-weight: 400; color: #494949", 1)
    s = sub(s, "grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 4px; font-size: 11px",
            "grid-template-columns: repeat(2, max-content); gap: 0 16px; font-size: 13px", 1)
    s = sub(s, "flex-direction: column; gap: 6px; padding-left: 14px; border-left", "flex-direction: column; gap: 3px; padding-left: 14px; border-left", 1)
    s = sub(s, '<section style="display: flex; flex-direction: column; gap: 8px; min-height: 0">',
            '<section style="display: flex; flex-direction: column; gap: 7px; min-height: 0">', 1)
    s = sub(s, "font-size: 12px", "font-size: 13px")  # legend, "next 12 h", moon rise/set, alert tag, amounts
    s = sub(s, "font-size: 12.5px", "font-size: 13.5px", 2)  # callout detail, next chip
    # Charts (SVG units; effective size = units × the svg's scale).
    s = in_svg(s, '<svg width="504" height="75" viewBox="0 0 544 81"', [   # scale 0.926
        ('font-size="11" fill="#494949" text-anchor="end"', 'font-size="13" fill="#494949" text-anchor="end"'),
        ('font-size="12" font-weight="700"', 'font-size="13.5" font-weight="700"'),
        ('font-size="11.5" font-weight="700" fill="#494949"', 'font-size="13" font-weight="700" fill="#494949"'),
        ('font-size="11.5" font-weight="800"', 'font-size="13" font-weight="800"'),
    ])
    if winter:
        s = in_svg(s, '<svg width="300" height="56" viewBox="0 0 300 56"', [
            ('font-size="9.5" font-weight="700"', 'font-size="11.5" font-weight="700"'),
            ('font-size="11" fill="#494949"', 'font-size="12.5" fill="#494949"'),
        ])
    else:
        s = in_svg(s, '<svg width="300" height="66" viewBox="0 0 300 66"', [
            ('font-size="10.5" font-weight="700"', 'font-size="12" font-weight="700"'),
            ('font-size="11" fill="#494949"', 'font-size="12.5" fill="#494949"'),
        ])
    s = in_svg(s, '<svg width="222" height="52" viewBox="0 0 230 54"', [  # scale 0.965
        ('font-size="11" font-weight="600"', 'font-size="12.5" font-weight="600"'),
    ])
    if winter:  # the gauge heading already says "snow via NWS"; one-line labels keep the 7-day rows ≥ 24 px
        s = sub(s, ">Snow depth · NWS<", ">Snow depth<", 1)
        s = sub(s, ">Season snow · NWS<", ">Season snow<", 1)
        s = sub(s, ">liquid in · snow via NWS<", ">in · snow via NWS<", 1)  # fits beside "GAUGE" at 13 px
    return s


def main():
    for name, winter in (("dashboard.html", False), ("dashboard-winter.html", True)):
        p = ROOT / "source" / name
        p.write_text(update(p.read_text(encoding="utf-8"), winter), encoding="utf-8", newline="\n")
        print(f"updated {p}")


if __name__ == "__main__":
    main()
