"""Build source/icons-proposed.html: icon-set additions and fixes awaiting approval.

Each entry is shown large and at the size it is drawn on the screen, on its intended background.
Usage: python design/tools/icons_proposed.py
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
ICONS = ROOT / "icons"

# (icon, title, note, white-on-black, large size, screen size, screen sample text)
ITEMS = [
    ("modifiers/sunny-wind", "Sunny + wind (new)",
     "The approved wind modifier on Sunny. Forecast rows, header icon.", False, (64, 64), (28, 28),
     "Windy · frost at night"),
    ("markers/callout-storm", "Callout · storm (new)", "White bolt in the black radar callout card.", True,
     (49, 64), (22, 30), "Storms ~4 PM"),
    ("markers/callout-snow", "Callout · snow (new)", "White snowflake in the black radar callout card.", True,
     (64, 64), (28, 28), "Snow by 6 PM"),
    ("markers/alert-triangle-inverse", "Alert · inverse (new)",
     "White on black for the header alert tag. Exclamation now has its dot.", True, (64, 59), (12, 11),
     "WINTER STORM WARNING"),
    ("markers/alert-triangle", "Alert (fix to v1)",
     "v1 drew the exclamation without its dot; stroke shortened and dot added.", False, (64, 59), (12, 11),
     "NWS alert"),
]


def svg(name, w, h):
    text = (ICONS / f"{name}.svg").read_text(encoding="utf-8")
    vb = re.search(r'viewBox="([^"]+)"', text).group(1)
    inner = re.search(r"<svg[^>]*>(.*)</svg>", text, re.S).group(1)
    return f'<svg width="{w}" height="{h}" viewBox="{vb}" aria-hidden="true">{inner}</svg>'


def cell(name, title, note, dark, big, small, sample):
    bg, fg = ("#000000", "#FFFFFF") if dark else ("#FFFFFF", "#000000")
    tag = ("font-size: 12px; font-weight: 800; letter-spacing: 0.08em; text-transform: uppercase"
           if name.startswith("markers/alert") else "font-size: 15px; font-weight: 700")
    return f'''
    <div style="display: flex; flex-direction: column; gap: 10px; padding-top: 12px; border-top: 1px solid #B6B6B6">
      <div style="display: flex; gap: 18px; align-items: center">
        <div style="width: 96px; height: 96px; display: flex; align-items: center; justify-content: center; background: {bg}; border: 1px solid #000000">{svg(name, *big)}</div>
        <div style="display: flex; gap: 8px; align-items: center; background: {bg}; color: {fg}; padding: 6px 10px; border: 1px solid #000000">{svg(name, *small)}<span style="{tag}">{sample}</span></div>
      </div>
      <span style="font-size: 15px; font-weight: 700">{title}</span>
      <span style="font-size: 12px; color: #494949">icons/{name}.svg · {note}</span>
    </div>'''


def main():
    cells = "".join(cell(*item) for item in ITEMS)
    html = f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Icon set additions (proposed)</title>
<link href="https://fonts.googleapis.com/css2?family=Inter+Tight:wght@300;400;600;700;800&amp;display=swap" rel="stylesheet">
<style>body{{margin:0}}</style>
</head>
<body>
<div style="width: 1200px; box-sizing: border-box; padding: 36px 40px; background: #FFFFFF; color: #000000; font-family: 'Inter Tight', 'Helvetica Neue', Arial, sans-serif; display: flex; flex-direction: column; gap: 18px">
  <header style="display: flex; justify-content: space-between; align-items: baseline; border-bottom: 3px solid #000000; padding-bottom: 10px">
    <span style="font-size: 28px; font-weight: 800; letter-spacing: -0.01em">Icon set · v1.1 (proposed)</span>
    <span style="font-size: 13px; color: #494949">Additions and fixes awaiting approval · large and at screen size</span>
  </header>
  <div style="display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); column-gap: 40px; row-gap: 18px">{cells}
  </div>
</div>
</body>
</html>
'''
    out = ROOT / "source" / "icons-proposed.html"
    out.write_text(html, encoding="utf-8", newline="\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
