# Inkplate weather display — design spec

Status: **approved 2026-10-24** ("B3b · Tall radar", winter state, icon set v1).
Design canvas (visual history and comments): https://claude.ai/artifact/L59NG2bMU2NXkNgvtAjB3W

All numbers on the reference boards are mock data. This document describes what each
element shows, where its data comes from, and how it behaves. The reference renders
in `reference/` are the source of truth for exact visual appearance.

## Files

| Path | What it is |
|---|---|
| `source/dashboard.html` | Approved board, normal state (fall afternoon, storms approaching). Plain static HTML. |
| `source/dashboard-winter.html` | Same layout in a winter-storm state (alert, sleet → snow). |
| `source/icons.html` | Icon sheet v1. |
| `source/*.dc.html` | The same boards exactly as exported from the design canvas (need the canvas runtime to render). |
| `icons/<group>/<name>.svg` | Every icon as its own SVG (extracted from the icon sheet). |
| `reference/*.png` | Browser renders of the boards; `*-eink.png` are quantized to the panel's 8 gray levels. |
| `tools/` | `dc2html.py` (canvas → plain HTML), `extract_icons.py`, `render_reference.py`. |

## Display

- Inkplate 10, landscape, **1200 × 825 px**, 3-bit grayscale.
- Use only these 8 levels: `#000000 #242424 #494949 #6D6D6D #929292 #B6B6B6 #DBDBDB #FFFFFF`.
  The design uses black, `#494949` (secondary text), `#6D6D6D` (lows, tertiary),
  `#929292` / `#B6B6B6` / `#DBDBDB` (fills, rules), and white.
- Patterns (hatching, dots) carry meaning where color would on a normal screen.
- Full refresh every 30 min plus a button-triggered refresh; no partial-refresh regions are assumed.

## Typography

- Single family: **Inter Tight** (weights 300, 400, 600, 700, 800). Bundle the font files with
  the renderer; do not fetch Google Fonts at runtime.
- Key sizes: current temperature 180 px / 800, letter-spacing −0.065em · H/L 38 px (H 700, L 300) ·
  header city 22 px / 800 · section labels 13 px / 800, uppercase, 0.12em tracking ·
  forecast rows 14.5 px, temps 23 px · readings values 24 px / 700.
- Numbers never wrap (H/L, temps are `nowrap`).

## Layout (px)

```
padding 34 top/bottom, 36 left/right
┌ header (44, 3px black rule under) ─────────────────────────────────────────────┐
├ 16 gap ────────────────────────────────────────────────────────────────────────┤
│ LEFT 600                              │24│ RIGHT 504                            │
│ radar 600 × 627                       │  │ current temp block                   │
│                                       │  │ temperature trend (72 h)             │
│                                       │  │ chance of precip (300) │ gauge       │
│                                       │  │ sun (236) │ moon                     │
│ readings strip (5 cells)              │  │ 7-day list (stretches to bottom)     │
└───────────────────────────────────────┴──┴──────────────────────────────────────┘
```

The bottom of the 7-day list aligns with the bottom of the readings strip: the list's rows
share the remaining height (flex), with a 24 px minimum per row.

## Components

### Header
- Left: **current-conditions icon** (34 px, no label) → city ("Savoy, IL", 22/800) → date (22/300, gray).
- Right: **day line** (17/600), a one-sentence summary such as "Storms around 4 PM, then colder and windy."
  When an **NWS alert** is active, it replaces the day line: black tag with ⚠ + event name, then
  "until <time> · <key impact>".
- Then a 1 px divider, "Next update h:mm" (when no alert), battery icon + %, time (22/700).

### Radar (600 × 627, left column)
- Base map: county lines `#DBDBDB`, state line dashed `#929292`, interstates `#B6B6B6` 3.5 px,
  city dots + labels with a white halo, home marked by a black square + "SAVOY".
- Echo fills (same coding as the icon sheet `fills/`): light `#DBDBDB`, moderate `#929292`,
  heavy `#494949`, extreme/hail `#000`; **ice/sleet** diagonal hatch; **snow** dots; heavy snow dots on gray.
- Overlays:
  - 25 mi range ring.
  - Storm-motion arrow with speed.
  - Dashed **arrival lines** labeled with black time tags ("3 PM / 4 PM / 5 PM"); in winter the same
    device shows **snow-line** positions ("SNOW 6 PM").
  - A bold rule marks the current snow line.
- **Lightning** (NOAA GLM, last 30 min): bolt symbols ~12 px tall: solid black with a white halo for ≤ 10 min,
  outlined (white fill, black outline) for 10–30 min (`icons/markers/lightning-recent.svg`, `lightning-older.svg`).
  Nearby flashes merge into one bolt. Added after the reference boards, so they don't show it.
- Top-left legend: "Radar · <site> <time>" + swatches (+ "Lightning · 30 min" when flashes are shown) (rain intensities; or Rain / Ice / Snow
  when frozen precip is present).
- Bottom-right callout: black card with the headline event ("Storms ~4 PM" / "Snow by 6 PM") +
  one-line detail, and below it a white "Next:" chip for the next notable hazard.

### Readings strip (under radar, 5 cells with light dividers)
Inside temp + RH · Garage temp + RH · Pressure + trend arrow · Air quality (US AQI + category) · UV index.

### Current temperature block (top of right column)
- Big temperature, left-aligned. The detail block is pushed to the right edge, with the spare width between them.
- Detail block: **H xx° L xx°** (one line) · "Feels xx° · Humidity xx%" · wind arrow + "Wind <dir> <speed> · gusts <g>" ·
  "Average xx° / xx°" (climate normal high/low for the date, small gray).

### Temperature trend (504 × 85)
- One continuous timeline: **past 24 h** (solid line, gray area) → **NOW** marker → **next 48 h** (dashed).
- Gridlines every 10°, labels at left. Dotted midnight lines with day names below. Black bars under
  the axis mark night (sunset → sunrise).
- Labels only on notable points: past low, each upcoming low/high, the end value.
- No header row (removed for space).

### Chance of precip (300 wide) + gauge
- 12 hourly bars, heading on top. Title follows the dominant type ("Chance of rain" / "Chance of snow").
  The right side of the heading gives the expected amount when meaningful ("3–5 in · ice 0.05 in").
- Bar fill by type: rain solid (darker = higher chance), snow dots, ice/sleet hatch. A small key appears
  **only when more than one type** is in the window.
- Percent labels above bars ≥ 20 %; hour labels every 3 h.
- **Gauge** (right, behind a divider): Ecowitt totals: Today, This week, October (month), 2026 (year).
  In winter, since the gauge is unheated and undercounts snow, the cells switch to liquid totals plus
  NWS **snow depth** and **season snowfall**; the heading notes "liquid in · snow via NWS".

### Sun & moon
- Sun: horizon with hatched ground, dashed full-day arc, solid arc up to the sun's current position,
  sun marker with rays; day length centered under the arc; "↑ rise · ±m/day · ↓ set".
- Moon (behind a divider): today's phase drawn at 36 px with % lit and phase name, rise/set times;
  a strip of the next four principal phases with dates (Full / Last / New / First).

### 7-day forecast list
- Columns: day (TODAY in a black tag) · icon 28 px · short description · wind (direction arrow + dir + speed,
  **bold at ≥ 20 mph**) · precip (drop + % for rain, snowflake + amount for snow days, pellet + % for ice)
  · **high** (23/800, 16 px gap before it) · **low** (23/300 gray). Highs and lows each form their own
  left-aligned column.
- Rows separated by 1 px `#B6B6B6` rules.

## Conditional behavior (summary)

| Situation | What changes |
|---|---|
| NWS alert active | Header day line → alert tag + "until … · impact"; storm icon gets the alert badge modifier. |
| Frozen precip in the next 12 h | Precip chart titled by dominant type, patterned bars, key if mixed; amounts shown. |
| Snow day in the forecast | Precip column shows snowflake + amount instead of %. |
| Winter (snow on ground / season) | Gauge cells switch to liquid totals + NWS snow depth / season total. |
| Gusts ≥ 20 mph | Wind modifier on the header icon; bold wind in the forecast list. |
| Clear night ≤ 34° | Frost modifier on the header icon. |

## Data sources (intended)

| Field | Source |
|---|---|
| Outdoor / indoor / garage temp & RH, rain gauge, pressure, **wind**, **UV** | Ecowitt GW2000B local API (LAN) |
| Present weather; wind/UV fallback | NWS observations, KCMI (Willard Airport); Open-Meteo |
| Hourly + daily forecast, precip chance/type/amounts | NWS gridpoint forecast (Open-Meteo as fallback) |
| Alerts | NWS alerts API for the county/zone |
| Radar | NOAA MRMS (reflectivity + precip type); IEM NEXRAD (KILX) fallback |
| Lightning (map) | NOAA GOES GLM, last 30 min (not the station sensor) |
| Air quality, UV | Open-Meteo air-quality / forecast APIs |
| Snow depth, season snowfall | NWS (climate / observations) |
| Climate normals (average high/low) | NOAA normals for Champaign area |
| Sun, moon | Computed locally (no API) |
| Day line | Generated from forecast + station data (NWS wording as a fallback) |

## Icons

`icons/` contains 59 SVGs: `conditions/` (20), `modifiers/` (4 composite examples),
`moon/` (8 phases), `wind/` (8 arrows named by the direction the wind comes **from**),
`markers/` (precip type, pressure trend, sun marker, map symbols, alert triangle, map lightning bolts) and
`fills/` (radar / precip-bar fills). The renderer should map forecast weather codes
(NWS icon codes or WMO codes) onto `conditions/` and add modifiers by rule (table above).

## Known gaps to resolve during the build

- Radar map geometry on the boards is hand-drawn. The real build needs a base map
  (simplified county/road vectors) plus radar tiles reprojected to the same extent.
- Chart label placement (lows/highs) must avoid collisions with real data.
- A test fixture set (fall, winter storm, alert, missing data) should reproduce the reference boards.
- Behavior when a source is stale or unavailable (dim value + "as of" time) is not yet designed.
