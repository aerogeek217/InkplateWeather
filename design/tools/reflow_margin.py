"""One-off: reflow the approved boards for a 6 mm (35 px) wider margin without changing any font size.

Applied 2026-10-24 to source/dashboard.html and dashboard-winter.html (see design/SPEC.md, Layout). Kept for the
record; running it again on already-reflowed boards fails its own checks.

  margin   34/36 px → 69/71 px (top-bottom / left-right); header→main gap 16 → 12
  columns  600 + 24 + 504 → 530 + 24 + 504; radar 600×627 → 530×567, same map scale (cropped around home)
  readings strip columns sized to content (auto) instead of equal fifths
  right column gaps/paddings 10 → 8; trend chart plot 70 → 60 px tall; precip bars baseline −8 px
  forecast icons 28 → 24 px (the icon sheet's forecast-list size)
  header: "Next update h:mm" removed (more room for the day line)

Usage: python design/tools/reflow_margin.py
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
NUM = r"-?\d+(?:\.\d+)?"


def fmt(v: float) -> str:
    return f"{round(v, 2):g}"


def sub_once(s: str, old: str, new: str) -> str:
    if s.count(old) < 1:
        raise SystemExit(f"pattern not found: {old[:70]}")
    return s.replace(old, new)


def transform_svg(block: str, ty) -> str:
    """Apply ty() to every y coordinate in an SVG fragment (paths, polylines, y/cy attributes, rect heights)."""

    def path(m):
        out, cmd = [], None
        for tok in re.findall(rf"[MLHVZmlhvz]|{NUM},{NUM}|{NUM}", m.group(2)):
            if tok.isalpha():
                cmd = tok
                out.append(tok)
            elif "," in tok:
                x, y = tok.split(",")
                out.append(f"{x},{fmt(ty(float(y)))}")
            elif cmd == "V":
                out.append(fmt(ty(float(tok))))
            else:
                out.append(tok)
        text = " ".join(out)
        text = re.sub(r"([MLHVZ]) ", r"\1", text)
        return f'{m.group(1)}"{text}"'

    block = re.sub(r'(\sd=)"([^"]*)"', path, block)
    block = re.sub(r'(\spoints=)"([^"]*)"',
                   lambda m: m.group(1) + '"' + " ".join(
                       f"{p.split(',')[0]},{fmt(ty(float(p.split(',')[1])))}" for p in m.group(2).split()) + '"', block)

    def rect(m):
        tag = m.group(0)
        y = re.search(rf'\sy="({NUM})"', tag)
        h = re.search(rf'\sheight="({NUM})"', tag)
        if y and h:
            top, bottom = float(y.group(1)), float(y.group(1)) + float(h.group(1))
            tag = tag.replace(y.group(0), f' y="{fmt(ty(top))}"', 1)
            tag = tag.replace(h.group(0), f' height="{fmt(ty(bottom) - ty(top))}"', 1)
        return tag

    block = re.sub(r"<rect\b[^>]*>", rect, block)
    block = re.sub(rf'(<(?:text|circle)\b[^>]*?\s(?:y|cy)=)"({NUM})"',
                   lambda m: f'{m.group(1)}"{fmt(ty(float(m.group(2))))}"', block)
    return block


def replace_svg(s: str, opening: str, new_opening: str, ty) -> str:
    start = s.index(opening)
    end = s.index("</svg>", start) + len("</svg>")
    body = s[start + len(opening):end]
    # Pattern tiles in <defs> are in their own coordinates; leave them alone.
    parts = re.split(r"(<defs>.*?</defs>)", body, flags=re.S)
    body = "".join(p if p.startswith("<defs>") else transform_svg(p, ty) for p in parts)
    return s[:start] + new_opening + body + s[end:]


def reflow(s: str, winter: bool) -> str:
    s = sub_once(s, "padding: 34px 36px;", "padding: 69px 71px;")
    s = sub_once(s, "flex-direction: column; gap: 16px; overflow: hidden", "flex-direction: column; gap: 12px; overflow: hidden")
    s = sub_once(s, "grid-template-columns: 600px 504px", "grid-template-columns: 530px 504px")
    s = sub_once(s, "position: relative; width: 600px; height: 627px", "position: relative; width: 530px; height: 567px")
    s = sub_once(s, 'width="600" height="627" viewBox="-240 -250.8 480 501.6"', 'width="530" height="567" viewBox="-212 -226.8 424 453.6"')
    s = sub_once(s, '<rect x="-240" y="-250.8" width="480" height="501.6" fill="#FFFFFF">',
                 '<rect x="-212" y="-226.8" width="424" height="453.6" fill="#FFFFFF">')
    s = sub_once(s, "grid-template-columns: repeat(5, minmax(0, 1fr)); column-gap: 0; border-top: 2px",
                 "grid-template-columns: repeat(5, auto); column-gap: 0; border-top: 2px")
    # Header: "Next update" is dropped (user decision) to give the day line more room.
    if not winter:
        s = sub_once(s, '      <span style="font-size: 14px; color: #494949">Next update 3:00</span>\n', "")
    # Right column: tighter gaps and section paddings.
    s = sub_once(s, '<section style="display: flex; flex-direction: column; gap: 10px; min-height: 0">',
                 '<section style="display: flex; flex-direction: column; gap: 8px; min-height: 0">')
    s = s.replace("border-top: 2px solid #000000; padding-top: 10px", "border-top: 2px solid #000000; padding-top: 8px")
    # Trend chart: plot area 70 → 60 tall; everything below the axis moves up 10.
    s = replace_svg(s, '<svg width="504" height="85" viewBox="0 0 544 92"',
                    '<svg width="504" height="75" viewBox="0 0 544 81"',
                    lambda y: y * 60 / 70 if y <= 70.01 else y - 10)
    # Precip chart: baseline 8 px higher, bars scaled to fit; labels below the baseline move up 8.
    if winter:
        s = replace_svg(s, '<svg width="300" height="64" viewBox="0 0 300 64"', '<svg width="300" height="56" viewBox="0 0 300 56"',
                        lambda y: y * 42 / 50 if y <= 50.01 else y - 8)
    else:
        s = replace_svg(s, '<svg width="300" height="74" viewBox="0 0 300 74"', '<svg width="300" height="66" viewBox="0 0 300 66"',
                        lambda y: y * 48 / 56 if y <= 56.01 else y - 8)
    # Forecast icons at the icon sheet's list size.
    s = s.replace('<svg width="28" height="28" viewBox="0 0 48 48" aria-hidden="true">',
                  '<svg width="24" height="24" viewBox="0 0 48 48" aria-hidden="true">')
    # Placeholder map artwork (phase 3 generates the real map for the extent): drop Decatur, now outside the
    # crop, and nudge the Clinton dot inside the edge.
    s = sub_once(s, '<circle cx="-235" cy="73" r="3.6"></circle><text x="-229" y="92">Decatur</text>', "")
    s = sub_once(s, '<circle cx="-210" cy="-34" r="3.2"></circle><text x="-205" y="-60">Clinton</text>',
                 '<circle cx="-204" cy="-34" r="3.2"></circle><text x="-199" y="-60">Clinton</text>')
    if winter:  # keep the snow-line tags inside the cropped map
        s = sub_once(s, 'translate(-190,58)', 'translate(-172,58)')
        s = sub_once(s, 'translate(-190,118)', 'translate(-172,118)')
    else:  # arrival-time tags slide down their dashed lines, clear of the legend
        s = sub_once(s, 'translate(-44.4,-205)', 'translate(-50.6,-175)')
        s = sub_once(s, 'translate(27,-205)', 'translate(20.8,-175)')
        s = sub_once(s, 'translate(97.3,-205)', 'translate(91.1,-175)')
    return s


def main():
    for name, winter in (("dashboard.html", False), ("dashboard-winter.html", True)):
        p = ROOT / "source" / name
        p.write_text(reflow(p.read_text(encoding="utf-8"), winter), encoding="utf-8", newline="\n")
        print(f"reflowed {p}")


if __name__ == "__main__":
    main()
