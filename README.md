# Inkplate weather

A wall-mounted weather display on an Inkplate 10 (1200 × 825, 8-level grayscale e-paper) for Savoy, IL.
It shows weather radar, readings from an Ecowitt GW2000B station (outdoor, indoor, garage, rain gauge,
pressure), the NWS forecast and alerts, air quality, and sun and moon information.

## Status

- **Design: approved** (2026-10-24). See [`design/SPEC.md`](design/SPEC.md) and the renders in
  [`design/reference/`](design/reference/).
- Architecture: not decided yet. The working plan is a containerized service on a Raspberry Pi that
  gathers data and renders the screen to an image; the Inkplate wakes every 30 min (or on button press),
  downloads the image, displays it and goes back to sleep.

## Repository layout

```
design/
  SPEC.md          what each element shows, data sources, conditional behavior
  source/          approved boards as static HTML (+ original canvas exports)
  icons/           every icon as an individual SVG
  reference/       reference renders (*.png) and 8-gray previews (*-eink.png)
  tools/           scripts that regenerate the above
```

## Regenerating design assets

Requires Python 3 with Pillow, and Microsoft Edge or Google Chrome for rendering.

```bash
python design/tools/dc2html.py design/source/HeroTall.dc.html design/source/dashboard.html
```

```bash
python design/tools/extract_icons.py
```

```bash
python design/tools/render_reference.py
```
