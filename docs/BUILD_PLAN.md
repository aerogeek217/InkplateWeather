# Build plan

Status: **Phase 1 merged (2026-10-24). Phase 2 built, approved and deployed** (OMV Pi; first 2396c38 at 12:58 CDT,
then the readability pass 2c22f9c at 13:30 CDT, 2026-10-24); now running on live data for a few days before the `phase-2` PR.
Read first: [`design/SPEC.md`](../design/SPEC.md) (what to build) and [`ARCHITECTURE.md`](ARCHITECTURE.md) (how).
The reference renders in `design/reference/` are the visual source of truth.

## Ground rules

- **Design fidelity.** The live screen must match `design/reference/dashboard.png` and `dashboard-winter.png` when fed the
  matching fixture data. Don't redesign. If something can't fit with real data, note it under *Open decisions*
  rather than improvising.
- **8 grays only** (`#000000 #242424 #494949 #6D6D6D #929292 #B6B6B6 #DBDBDB #FFFFFF`) in final output; fonts (Inter Tight)
  and icons are bundled. The renderer must work with no internet access except for data fetches.
- **No secrets in git.** Wi-Fi credentials and HA tokens live in untracked files or environment
  variables (`config/config.yaml` is gitignored; commit `config/config.example.yaml`).
- **Line endings LF** (enforced by `.gitattributes`). Target platform: Linux arm64 container. Development also happens on
  Windows, so tests must not depend on the host OS.
- **Branches and PRs.** Each phase is built on its own branch (`phase-1`, `phase-2`, …) cut from `main` and delivered as a
  pull request into `main` (remote: https://github.com/aerogeek217/InkplateWeather). Pushing the phase branch and opening
  its PR are allowed; pushing to, merging into or rewriting `main` is not. The user reviews and merges.
- **Ask before** any other push, deploying to the Pis, flashing the Inkplate, or contacting the user's devices
  beyond read-only GETs to the Ecowitt gateway.
- Keep commits small and focused; one phase task per commit.

## Target layout

```
config/
  config.example.yaml       documented example (committed)
  config.yaml               real config (gitignored)
server/
  pyproject.toml            deps: fastapi, uvicorn, httpx, jinja2, pydantic, pyyaml, pillow, playwright,
                            astral (or skyfield), eccodes/cfgrib (phase 3), pytest, pytest-asyncio, respx
  Dockerfile
  app/
    main.py                 FastAPI app + endpoints
    config.py               load/validate YAML (pydantic models)
    fields.py               canonical field registry (name → unit, kind)
    readings.py             Reading model, status enum (ok/stale/missing/error)
    cache.py                per-provider TTL cache, last-good persistence (./cache volume)
    resolver.py             config-driven source selection + fallbacks + provenance
    messages.py             all plain-language error/notice strings (one place)
    providers/
      base.py               Provider protocol
      fixture.py            serves canned readings from fixtures/*.json (phase 1)
      ecowitt_local.py      GW2000B /get_livedata_info
      nws_observations.py   KCMI latest observation
      nws_forecast.py       points → forecastHourly + forecastGridData + daily
      nws_alerts.py         active alerts for zone/county
      open_meteo.py         AQI, UV, fallback forecast
      acis.py               normals, season snowfall, snow depth
      home_assistant.py     generic entity reader
      astro.py              sun/moon (local computation)
      device.py             battery/voltage/reason from request
      radar_mrms.py         phase 3
      radar_iem.py          phase 3 fallback
    viewmodel.py            builds the template context from resolved readings (formatting, icon choice, text)
    icons.py                weather code → icon name (+ modifiers) per SPEC
    render/
      browser.py            persistent Playwright Chromium; render(html) → PNG bytes
      quantize.py           8-level grayscale LUT (shared with design/tools)
  templates/
    dashboard.html.j2       Jinja2 port of design/source/dashboard.html (both states via data)
    partials/*.j2           header, radar, readings, current, trend, precip, sun_moon, forecast
  assets/
    fonts/                  Inter Tight woff2/ttf (OFL)
    icons/                  copy of design/icons
    basemap/                GeoJSON (phase 3)
  fixtures/
    fall.json               view-model inputs reproducing design/reference/dashboard.png
    winter.json             … dashboard-winter.png
    outage.json             several sources missing (error-text states)
    ecowitt_get_livedata_info.sample.json   (exists)
  tests/
docker-compose.yml
firmware/
  inkplate_weather/inkplate_weather.ino
  inkplate_weather/config.example.h      (config.h gitignored)
  README.md
```

## Key interfaces (contracts the workstreams share)

```python
# readings.py
class Status(str, Enum): ok = "ok"; stale = "stale"; missing = "missing"; error = "error"

class Reading(BaseModel):
    value: Any                 # canonical unit (°F, %, mph, inHg, in, deg, AQI, …) or structured (forecast lists)
    unit: str | None
    observed_at: datetime | None
    source: str                # provider instance name from config, e.g. "ecowitt"
    status: Status = Status.ok
    note: str | None = None    # machine-friendly reason; messages.py turns it into plain language

# providers/base.py
class Provider(Protocol):
    name: str                  # instance name from config
    ttl: timedelta
    async def fetch(self, ctx: RequestContext) -> dict[str, Reading]:   # keys = provider-native keys
        ...

# resolver.py
class Resolved(BaseModel):
    field: str
    reading: Reading           # winner, or a missing/error Reading if nothing qualified
    tried: list[Attempt]       # each source tried, with why it was rejected (stale, missing, error)

def resolve(fields_cfg, provider_results, now) -> dict[str, Resolved]
```

`viewmodel.build(resolved, now, device) -> DashboardContext` is the only thing templates see. Templates never
call providers or format raw numbers.

## Phases, tasks and acceptance criteria

Parallel workstreams are marked **[A] server/render**, **[B] data providers**, **[C] radar**, **[D] firmware**,
**[E] deployment**. Dependencies are noted per task.

### Phase 1: end-to-end skeleton with fixture data

| # | WS | Task | Depends | Done when |
|---|---|---|---|---|
| 1.1 ✅ | A | Python project scaffold (`server/pyproject.toml`, app package, pytest, ruff), `config.example.yaml`, gitignore entries | – | `pytest` runs green on Windows and in the container |
| 1.2 ✅ | A | `readings.py`, `fields.py`, `config.py`, `resolver.py`, `messages.py` with unit tests (priority, max_age → stale, all-missing → missing with plain text) | 1.1 | tests cover fallback order, staleness, provenance |
| 1.3 ✅ | A | `fixture` provider + `fixtures/fall.json` and `winter.json` reproducing the reference boards' values | 1.2 | fixtures load and resolve every field the template needs |
| 1.4 ✅ | A | Jinja2 template port of `design/source/dashboard.html` driven by `DashboardContext` (both states, alert/no-alert, snow/rain precip, winter gauge), icons inlined from `assets/icons` | 1.3 | rendered HTML for `fall` is visually identical to `design/source/dashboard.html` |
| 1.5 ✅ | A | `render/browser.py` (persistent Chromium, 1200×825, fonts local) + `quantize.py`; endpoints `/display.png`, `/preview`, `/health`, `/fixtures/{name}.png` | 1.4 | **golden test**: `fall` and `winter` renders differ from `design/reference/*-eink.png` by < 0.5 % of pixels (radar region may be masked until phase 3); render < 3 s on the dev machine |
| 1.6 ✅ | E | Dockerfile (arm64-capable), `docker-compose.yml` (port 8080, `./config` + `./cache` volumes, healthcheck, `TZ`) | 1.5 | `docker compose up` serves `/display.png` from fixtures locally; image builds for `linux/arm64` (buildx) |
| 1.7 ✅ | D | Inkplate firmware: Wi-Fi, read battery, `GET /display.png?batt=&mv=&reason=`, draw, deep sleep aligned to :00/:30, wake button, error screen + 5-min retry ×3 | 1.5 (endpoint contract only) | compiles with Arduino CLI for Inkplate 10; documented flashing steps; **user flashes it** |

Milestone: the Inkplate shows the fall reference board served from the Pi (after the user approves deployment).
**Reached 2026-10-24.**

Phase 1 notes (2026-10-24):
- Golden diffs on Windows (Playwright Chromium): fall 0.050 %, winter 0.095 % (sub-pixel chart/arc geometry only).
- In the Linux container against the old Edge references: fall 3.86 %, winter 4.04 % (text anti-aliasing only).
  With the user's approval the references were regenerated inside the container (canonical) plus a Windows
  baseline in `design/reference/windows/`; both platforms now measure fall 0.050 %, winter 0.095 %.
- 1.6: arm64 image builds (buildx) but Chromium can't run under QEMU emulation. Deployed on the OMV Pi 4
  (192.168.1.254, `~/inkplate-weather`, commit 7e83cbc, built natively in 284 s): healthy; fixtures on the Pi match
  the canonical references (fall 0.050 %, winter 0.095 %); `/display.png` 3.6 s cold, ~1.0 s warm.
- 1.7: compiles for both Inkplate 10 variants; flashed to the user's Soldered Inkplate 10 (COM3) with the user's approval.
- `/display.png` also returns `X-Sleep-Seconds` (seconds to the next :00/:30) so the firmware can align its sleep.

### Phase 2: live data, fallbacks, error text

| # | WS | Task | Depends | Done when |
|---|---|---|---|---|
| 2.1 ✅ | B | `ecowitt_local`: parse `get_livedata_info` (strings with units; hex IDs; `wh25`, `rain`, `lightning`, `ch_aisle`), map via config | 1.2 | parses the saved sample exactly; tolerant to missing blocks; unit tests |
| 2.2 ✅ | B | `nws_observations` (KCMI), `nws_forecast` (points → hourly/daily/grid incl. snow & ice amounts, weather type), `nws_alerts` (zone/county from points) with User-Agent from config/env and grid cache | 1.2 | recorded-response tests (respx); handles 429/5xx → error Reading |
| 2.3 ✅ | B | `open_meteo` (us_aqi, uv_index, fallback hourly/daily), `acis` (normals, season snow, snow depth), `astro` (sunrise/set, day length delta, moon phase %, rise/set, next 4 principal phases) | 1.2 | tests against recorded responses; astro verified against a known date |
| 2.4 ✅ | B | `home_assistant` generic entity provider (token via env) and `device` provider | 1.2 | tests with recorded HA state JSON |
| 2.5 ✅ | A | `viewmodel.py` real logic: icon mapping + modifiers (SPEC table), dominant precip type, snow amounts, day line (template-based sentence from forecast; NWS short forecast fallback), alert takeover, gauge winter swap, wind bold ≥ 20 mph, frost modifier, chart series + label placement without collisions | 2.1–2.3 | fixture-driven tests for each conditional behavior in SPEC |
| 2.6 ✅ | A | Error states per ARCHITECTURE *Error handling*: stale styling + "as of", missing-slot sentences, header summary when ≥ 2 sections missing, battery low notice; `fixtures/outage.json` | 2.5 | outage fixture renders readable plain-language messages; reviewed by user |
| 2.7 ✅ | A | `/status` page (per-field winner, ages, attempts) and `/admin/reload` | 1.2 | shows provenance for all fields |

Milestone: the live screen runs on real data for a few days; the user reviews error wording.

Phase 2 contracts (frozen 2026-10-24, commit 77dfe6f): `app/shapes.py` (structured values), `app/conditions.py`
(NWS/WMO codes → icon set), live `config/config.example.yaml` (sources per field, provider key names). Added to the
plan: a `history` provider that records station readings in the cache (24 h temperature chart, 3 h pressure trend),
with NWS observations as the fallback.

Layout reflow (approved 2026-10-24): 6 mm more margin, no font changes, "Next update" removed; references
regenerated (fall 0.052 %, winter 0.051 % in the container). First live render on the dev PC (2026-10-24): 33/35
fields ok; gaps for 2.5: generated day line / callout / next chip, trend scale ignores the forecast range (labels
clipped), "gusts" shown when equal to the wind speed.

### Phase 3: radar

| # | WS | Task | Depends | Done when |
|---|---|---|---|---|
| 3.1 | C | Base map build script: TIGER/Line cartographic (states, counties, primary roads, places) clipped to the display extent around Savoy (≈ 110 × 120 mi, same framing as the design), simplified, saved as GeoJSON; label list | – | map layer renders in the template matching the reference framing |
| 3.2 | C | `radar_mrms`: fetch latest `ReflectivityAtLowestAltitude` (or `MergedBaseReflectivityQC`) + `PrecipFlag` GRIB2 (gz) from `mrms.ncep.noaa.gov`, crop to extent, reproject to display pixels, classify into the 4 intensity grays and rain/ice/snow pattern masks | – | correct output on recorded files; < 5 s fetch+process on Pi 5 |
| 3.3 | C | `radar_iem` fallback: KILX single site (`ridge::ILX-N0B-0`) or `nexrad-n0q` WMS GetMap for the extent; decode color ramp → intensity classes (no type) | – | works when MRMS is blocked in tests |
| 3.4 | A | Radar partial: draw classified raster + patterns + base map + overlays (range ring, legend switching rain-only vs rain/ice/snow, callout) | 3.1–3.3, 2.5 | golden test against reference with recorded radar fixture |
| 3.5 | C | `lightning_glm` background poller (GOES-East GLM L2 LCFA NetCDF from NOAA's open-data AWS bucket), rolling 30-min flash list clipped to the extent; radar partial draws **bolt symbols** (`markers/lightning-recent.svg` for ≤ 10 min, `lightning-older.svg` for 10–30 min), merges flashes per ~8 px cell, and shows a legend entry only when flashes exist | 3.4 | recorded GLM files produce the expected bolts; poller survives gaps and restarts |

### Phase 4: polish

| # | WS | Task |
|---|---|---|
| 4.1 | C | Storm motion from 2–3 recent frames (cross-correlation), arrival-time lines and "arrives ~4 PM" callout; snow-line motion in winter |
| 4.2 | A | Day-line generation improvements; optional lightning slot (if the user approves a design change) |
| 4.3 | E | Deployment guide for OMV compose plugin; backup of `config/`; log rotation |
| 4.4 | D | Optional: captive-portal Wi-Fi setup, OTA updates |

## Testing strategy

- **Unit tests** for parsers, resolver, view-model rules (pytest, no network; recorded responses with respx).
- **Golden image tests**: render fixtures and compare against `design/reference/*-eink.png` with a pixel-diff threshold;
  failures write a diff image to `server/tests/_out/`.
- **Cross-platform baseline**: the references come from Edge on Windows, while the container uses Chromium on Linux, so text
  anti-aliasing may differ. If a golden test fails only on text edges (layout, positions and sizes match), don't loosen the
  threshold or touch the design. Report it with the diff image and propose regenerating the references inside the container;
  regenerate only after the user approves, then commit the new references with a note in `design/SPEC.md`.
- **Contract test** for the firmware endpoint: `/display.png` returns `image/png`, 1200×825, only the 8 allowed levels.
- **Live smoke test** (manual, opt-in flag): hits the real gateway and NWS once and prints `/status`.

## Open decisions (need the user)

1. ~~NWS contact~~ Resolved: `User-Agent: (InkplateWeather, https://github.com/aerogeek217/InkplateWeather)`.
2. ~~Lightning overlay~~ Approved: NOAA GLM, drawn as bolt symbols (task 3.5). The station's own lightning sensor must
   **not** be used.
3. **Storm motion** (phase 4.1) is the most uncertain piece; until then the callout uses forecast wording.
4. Exact display extent / base map framing: default to the design's framing centered on Savoy.

## Environment facts

- Ecowitt GW2000B: `http://192.168.1.135/get_livedata_info` (garage = channel 1; also wind, UV/solar; its lightning
  block is ignored by decision).
- Location: Savoy, IL 61874 (≈ 40.06 N, 88.25 W), NWS station KCMI, radar KILX, tz America/Chicago.
- Display: **Soldered** Inkplate 10 (FQBN `soldered-inkplate-boards:esp32:Inkplate10V2`, the firmware default).
- Deployment target: OpenMediaVault Pi (Pi 4/5, compose plugin), fixed IP; Home Assistant Pi is a data source only.
- Air quality: Open-Meteo now; PMS5003 later (probably via ESPHome → Home Assistant).
