# Architecture

Status: **draft for review** (2026-10-24). Design is fixed by [`design/SPEC.md`](../design/SPEC.md).

## Decisions so far

| Topic | Decision |
|---|---|
| Where it runs | One container on the **OpenMediaVault Pi**, managed with the OMV compose plugin. Home Assistant stays untouched (it can be a *data source*). |
| Language / renderer | **Python** service; **Playwright + headless Chromium** renders the approved HTML template to a 1200 × 825 image. |
| When to render | **On request**: the Inkplate asks, the server gathers fresh data, renders, and responds. |
| Sources | **Pluggable**: every value on screen can come from a sensor, NWS, or another provider, chosen in config with fallbacks (see *Data layer*). |
| Errors | The screen shows **plain-language text** where data is missing or old (see *Error handling*). |

## System overview

```
                         ┌──────────────── OMV Pi (Docker) ─────────────────┐
 Inkplate 10             │  weather-display container                       │
 ─ wakes every 30 min    │                                                  │
   or on button  ──GET /display.png?batt=78&mv=4020──▶  HTTP API (FastAPI)  │
                         │        │                                         │
 ◀── 8-gray PNG ─────────┤        ▼                                         │
 ─ draws, deep sleep     │  Collector ── per-source cache (TTL) ── Providers ──▶ Ecowitt GW2000B (LAN)
                         │        │                               ├──▶ NWS api.weather.gov
                         │        ▼                               ├──▶ MRMS / IEM radar
                         │  View model (resolved values +         ├──▶ Open-Meteo (AQ, UV, fallback fcst)
                         │  provenance + error notes)             ├──▶ RCC-ACIS (normals, snow)
                         │        ▼                               └──▶ Home Assistant API (optional)
                         │  Jinja2 template ─▶ Chromium (kept warm) ─▶ screenshot
                         │        ▼                                         │
                         │  quantize to 8 grays ─▶ PNG (+ last-good copy)   │
                         └──────────────────────────────────────────────────┘
```

## Request flow (render on request)

1. Inkplate wakes (timer or button), joins Wi-Fi, calls
   `GET /display.png?batt=<pct>&mv=<millivolts>&reason=<timer|button>`.
2. The server gathers all values **concurrently** with a total budget of ~8 s. Each provider has its own
   cache TTL, so most requests only refresh fast-changing data (station, alerts, radar).
3. The resolver picks a value for every field (with fallbacks) and attaches provenance and error notes.
4. The view model fills the Jinja2 version of `design/source/dashboard.html`. Chromium stays running between
   requests, so a render takes about 0.5–1.5 s on a Pi 4/5.
5. The screenshot is quantized to the panel's 8 levels (a mild dither on photographic areas, i.e. radar
   only) and returned as PNG. A copy is saved as *last good*.
6. If rendering fails outright, the server returns the last good image with an error strip burned in.
   If the server is unreachable, the firmware draws its own error screen (see *Firmware*).

Target: response within 10 s; the Inkplate waits up to 20 s.

## Data layer: pluggable sources

The flexibility you asked for, e.g. switching air quality from Open-Meteo to your own sensor later, or
outdoor temperature from Ecowitt to NWS, lives in config, not code.

### Concepts

- **Field**: something the screen shows, with a canonical name and unit.
  Examples: `outdoor.temperature` (°F), `outdoor.humidity` (%), `indoor.temperature`, `garage.temperature`,
  `pressure.sea_level` (inHg), `pressure.trend_3h`, `wind.speed` / `wind.gust` (mph), `wind.direction` (°),
  `rain.today` / `rain.week` / `rain.month` / `rain.year` / `rain.rate`, `air.aqi`, `uv.index`,
  `snow.depth`, `snow.season`, `normals.high` / `normals.low`, `conditions.current` (icon code),
  `forecast.hourly`, `forecast.daily`, `alerts.active`, `radar.frame`.
- **Provider**: a module that knows how to fetch from one system and *offers* some fields.
  Each provider declares its fields, units (converted to canonical units by the provider), and default TTL.
- **Reading**: `{value, unit, observed_at, source, status}` where status is `ok | stale | missing | error`.
- **Resolver**: for each field, tries the configured sources **in order** and takes the first reading that
  is fresh enough (`max_age`). Everything it tried is recorded for the error notes and `/status`.

### Providers (initial)

| Provider | Offers | Notes |
|---|---|---|
| `ecowitt_local` | outdoor temp/RH/dew point/feels-like, **wind speed/gust/direction**, **UV & solar radiation**, indoor temp/RH + pressure (WH25 block), garage (channel 1), rain event/rate/day/week/month/year (the gateway's `lightning` block is ignored) | Polls `http://192.168.1.135/get_livedata_info`. Sample response saved in `server/fixtures/ecowitt_get_livedata_info.sample.json`. Values come as strings with units (e.g. `"2.24 mph"`, `"72%"`); `common_list` uses hex IDs (0x02 outdoor temp, 0x07 RH, 0x03 dew point, 0x0A wind dir, 0x0B wind, 0x0C gust, 0x15 solar, 0x17 UV; rain 0x0D event, 0x0E rate, 0x10 day, 0x11 week, 0x12 month, 0x13 year). Channel → field mapping lives in config, so a newly paired sensor is a config change. |
| `ecowitt_push` (later) | same | Alternative: the gateway's "customized upload" POSTs to the container. More robust; no polling. |
| `nws_observations` | wind, pressure, present weather, visibility | Station `KCMI` (Willard Airport), `/stations/KCMI/observations/latest`. |
| `nws_forecast` | hourly + daily forecast, precip probability/type/amounts, sky | `/points/{lat},{lon}` → `forecastHourly` + `forecastGridData` (snow and ice amounts, weather type). |
| `nws_alerts` | active alerts | `/alerts/active` filtered to our zone/county. |
| `open_meteo` | AQI (US), UV, fallback forecast | No key. Also a fallback when NWS is down. |
| `acis` | daily normals, season snowfall, snow depth | RCC-ACIS for the Champaign-Urbana co-op station; cached for 12 h. |
| `home_assistant` | **any HA entity** | Token + entity IDs in config. A generic escape hatch for your future AQ sensor or anything HA already knows. |
| `radar_mrms` / `radar_iem` | radar frame | See *Radar*. |
| `lightning_glm` | lightning flashes in the map extent (last 30 min) | NOAA GOES-East GLM Level 2 (`GLM-L2-LCFA`) NetCDF files on the public NOAA AWS bucket, ~20 s cadence. See *Lightning*. The station's own lightning sensor is **not** used (user decision). |
| `astro` | sun/moon times, phase, next phases | Computed locally (`astral`/`skyfield`); never fails. |
| `device` | battery %, voltage, last wake reason | From the Inkplate's own request. |

### Config sketch (`config/config.yaml`)

```yaml
location: {name: "Savoy, IL", lat: 40.0617, lon: -88.2517, tz: America/Chicago}
nws: {user_agent: "(InkplateWeather, https://github.com/aerogeek217/InkplateWeather)", station: KCMI}

providers:
  ecowitt: {type: ecowitt_local, host: 192.168.1.50, ttl: 0s}
  ha:      {type: home_assistant, url: http://192.168.1.10:8123, token_env: HA_TOKEN, ttl: 60s}
  nws_obs: {type: nws_observations, ttl: 10m}
  nws_fc:  {type: nws_forecast, ttl: 30m}
  om:      {type: open_meteo, ttl: 30m}

fields:
  outdoor.temperature:  [{from: ecowitt, key: outdoor.temp, max_age: 10m}, {from: nws_obs, max_age: 90m}]
  garage.temperature:   [{from: ecowitt, key: ch2.temp, max_age: 15m}]
  wind.speed:           [{from: ecowitt, key: "0x0B", max_age: 10m}, {from: nws_obs, max_age: 90m}]
  uv.index:             [{from: ecowitt, key: "0x17", max_age: 15m}, {from: om, key: uv_index}]
  air.aqi:              [{from: om, key: us_aqi}]          # public data for now
  # later, when the PMS5003 works (e.g. via ESPHome → Home Assistant):
  # air.aqi:            [{from: ha, entity: sensor.pms5003_aqi, max_age: 20m}, {from: om, key: us_aqi}]
```

Swapping a source is a config edit plus a container restart (or a reload at `/admin/reload`).
The `/status` page shows, per field, which source won and why the others were skipped.

## Radar

Recommendation: **MRMS as primary, IEM as fallback.** Avoid RainViewer: its free tier was cut in 2026
(zoom ≤ 7, one color scheme, no nowcast).

| Option | What you get | Pros | Cons |
|---|---|---|---|
| **MRMS GRIB2** (`mrms.ncep.noaa.gov/data/2D/`): `ReflectivityAtLowestAltitude` or `MergedBaseReflectivityQC`, plus **`PrecipFlag`** | Raw dBZ + **precipitation type** on a 1 km grid, every 2 min | Raw numbers, so we draw our own grays and **rain/ice/snow patterns** exactly as designed; official NOAA data | CONUS files, so we crop to our box; GRIB2 (PNG-packed, so Pillow decodes it; no eccodes needed) |
| **IEM tiles / WMS** (`mesonet.agron.iastate.edu`): `ridge::ILX-N0B-0` (KILX single site) or `nexrad-n0q` mosaic; WMS GetMap with a bounding box | Colored PNG | Very simple; one request for our exact box | Colored image → must be mapped back to dBZ via the known color ramp; no precip type (IEM's HRRR `REFP` layer gives *modeled* type) |
| NWS ArcGIS `radar_base_reflectivity` MapServer export | Colored PNG for a bounding box | Official, one request | Same color-decoding issue; no type |

**Storm motion and arrival lines** ("Storms ~4 PM", the dashed 3/4/5 PM lines) are estimated from three recent
frames (cross-correlation of the echo field); built in phase 4, see *Storm motion* below. Without a trustworthy
motion the panel shows the current frame and the callout uses forecast wording (e.g. "Storms ~4 PM · 3–8 PM · 80%").

As built (Phase 3, 2026-10-24):
- `radar_mrms` fetches `ReflectivityAtLowestAltitude` + `PrecipFlag` (`mrms.ncep.noaa.gov/2D/…latest.grib2.gz`),
  decodes them with `app/grib2.py` (template 3.0 + 5.41 PNG packing only) and crops to the extent.
  `app/radar.py` reprojects onto the 536 × 570 px panel (one affine transform: the map is equirectangular around
  home), dBZ bilinearly and type by nearest cell, then classifies into 1-bit masks the partial fills with the
  design's grays/patterns: light ≥ 15, moderate ≥ 30, heavy ≥ 40, extreme ≥ 50 dBZ; with ≥ 100 px of snow/ice in
  view the legend switches to Rain / Ice / Snow (snow from 10 dBZ, heavy snow ≥ 25).
- PrecipFlag has no ice class. Liquid echoes over a ≤ 0 °C surface (MRMS `Model_SurfaceTemp`, ~14 MB hourly,
  fetched only with rain in view, at most hourly, every 6 h while the grid is warm) are drawn as ice/sleet.
- `radar_iem` (fallback) reads the IEM `n0q` CONUS mosaic: a palette PNG whose index is the value
  (dBZ = index / 2 − 32.5), plus its world file and JSON time. Intensity only. It shows ~30 % more echo than MRMS
  (strongest return of overlapping radars).
- Frames are plain JSON (masks as base64 PNG), so the cache keeps a last-good frame.

### Storm motion (phase 4.1, as built 2026-10-25)

`app/motion.py`, fed by `radar_mrms` (the IEM fallback has none):
- With ≥ 400 echo pixels in view, the provider reads the product's directory listing (`/2D/<product>/`, ~190 kB,
  cached 90 s) and fetches the files nearest 30 and 15 min before the latest (~0.7 MB each, decoded grids kept in
  memory). A snow line also needs the matching PrecipFlag files. All motion work uses a window 80 map units
  (~14 mi) wider than the panel. Any failure only drops the overlays.
- **Motion**: one translation for the whole echo field, the peak of the FFT cross-correlation of dBZ − 15 (clipped
  at 35, mean removed) with a parabolic sub-cell fit. The 30- and 15-min pairs must agree (±8 mph or 35 %, ±35°)
  with normalized peaks ≥ 0.35, speed 3–80 mph; otherwise nothing is drawn. Fast-growing lines fail this on
  purpose (the recorded 2023-04-01 squall line: 35 vs 54 mph).
- **Arrival lines**: perpendicular to the motion through the 20 dBZ leading edge (the edge in an 8 mi band around
  home when storms are heading for it, else the field's 95th percentile) at the next whole hours up to 3 h ahead,
  ≥ 45 units apart (every 30 min above ~32 mph). Tags on the fall board's row (y = −175) or column (x = −172).
  The arrow (70 units) sits on the storms just behind the edge, clear of home, legend, callout and tags.
- **Arrival at home**: from that band's edge; "storm" when ≥ 45 dBZ is in the band, else "rain". The callout says
  "Storms ~3 PM" (nearest half hour) · "7 mi west · ENE 15 mph" · forecast amount, when within 3 h; a winter storm
  in the forecast keeps forecast wording.
- **Snow line** (types legend): snow cells next to other precipitation, principal direction → a polyline (20-unit
  bins, median, smoothed). Its own displacement across the line between frames (median, both pairs agreeing within
  4.5 mph / 60 %) gives future positions every 1–3 h on local-clock multiples ("SNOW 6 PM", "SNOW 9 PM", ≤ 6 h) and
  the time it reaches home ("Snow by 5 PM", rounded up; within 12 h).
- The frame carries `motion`, `arrival` and `overlays` (JSON); `radarmap` draws only lines still in the future.
  Cost on the dev PC: ~50 ms per analysis plus the decodes.

## Lightning (map overlay, approved 2026-10-24)

Public options checked 2026-10-24:

| Option | Verdict |
|---|---|
| **NOAA GOES GLM** (Geostationary Lightning Mapper, GOES-East = GOES-19), Level 2 flashes, NetCDF every ~20 s on NOAA's open-data AWS bucket | **Recommended.** Free NOAA data, no key, covers all of Illinois. Total lightning (in-cloud + ground), roughly 8–10 km location accuracy, so good enough for "storms with lightning are here" but not exact strike points. GOES-19 GLM has been in Beta/provisional maturity, so expect occasional data gaps. |
| Blitzortung.org | Not recommended. The site allows viewing for private use, but its raw data is limited to network participants or with explicit permission. |
| Vaisala NLDN, Xweather, AccuWeather Lightning API | Commercial / paid. |

Because GLM arrives as many small files, the container runs a **light background poller** that keeps a rolling
30-minute list of flashes inside the map extent. The render-on-request path then just reads that list, so this is
the one place where data is gathered ahead of time. As built (`app/lightning.py`): files are ~220 KB every 20 s
(~1 GB/day), so the poller only runs while the radar shows echoes in view (for an hour after each render that did)
and backfills the last 30 minutes when it wakes; files are read with h5py; the list is persisted in
`cache/lightning.json`.

Display (approved): small **lightning-bolt symbols** on the radar map, about 12 px tall. Flashes in the last 10 min
are solid black bolts with a white halo; flashes 10–30 min old are outlined bolts (white fill, black outline), so
both read on any radar fill. A legend entry "Lightning · 30 min" appears only when there are flashes. Nearby
flashes are merged so bolts don't overlap (one bolt per ~8 px cell, newest wins). Icons:
`design/icons/markers/lightning-recent.svg`, `lightning-older.svg`.

**Base map**: generated once at build time from public-domain US Census TIGER/Line cartographic files
(`server/tools/build_basemap.py`: county lines, the home state line, interstates, lakes ≥ 2 km², the board's towns)
clipped to the approved board's framing (centered on home, 5.6 map units per mile, ~77 × 81 mi; `app/mapgeo.py`),
saved as GeoJSON (`server/assets/basemap/`, committed) and drawn in the radar panel's SVG (`app/radarmap.py`).

## Error handling (plain language)

Every field resolves to `ok`, `stale` or `missing`; the template shows each state consistently:

- **Stale** (older than its `max_age` but usable): value shown in gray with a small "as of 1:12 PM".
- **Missing**: the slot shows a short sentence instead of a number, e.g.
  - "Garage sensor hasn't reported since 1:12 PM."
  - "Radar unavailable: NOAA's radar feed didn't respond. Retrying next update."
  - "Forecast unavailable from NWS; showing Open-Meteo instead." (fallback used: small note)
- **Header**: if two or more sections are missing, the day line is replaced by one summary sentence
  ("Some data is unavailable: radar, forecast.") so a glance from across the room tells you something's off.
- **Device**: battery ≤ 15 % shows "Battery low: charge soon" in the header; ≤ 5 % adds a reminder strip.
- Messages are written once in a single messages file, so wording is consistent and easy to edit.

## HTTP API

| Endpoint | Purpose |
|---|---|
| `GET /display.png?batt=&mv=&reason=&fw=` | Render now and return the 8-gray PNG (Inkplate). |
| `GET /firmware/inkplate_weather-<n>.bin` | A published firmware build for over-the-air updates (with `x-MD5`). |
| `GET /preview` | Same page as live HTML in a browser (debugging/design tweaks). |
| `GET /status` | Per-field winner, ages, errors; provider health. |
| `GET /health` | Container health check. |
| `GET /fixtures/<name>.png` | Render a saved data fixture (fall, winter, alert, outage) to compare with `design/reference/`. |

### `/display.png` contract (frozen 2026-10-24; firmware builds against this)

Request: `GET /display.png?batt=<0–100>&mv=<millivolts>&reason=<timer|button|retry|boot>&fw=<build>`. All parameters
are optional; unknown values are ignored. The server passes the first three to the `device` provider.
`fw` (added 2026-10-25, additive) is the firmware build number, a positive integer; the server uses it only to decide
whether to announce an update (below) and to show it on `/status`.

Response `200`:
- `Content-Type: image/png`, exactly **1200 × 825**, 8-bit grayscale (`L`) or palette PNG whose pixels use only the
  8 levels `0, 36, 73, 109, 146, 182, 219, 255` (multiples of 255/7, rounded). Typically 60–150 kB.
- `X-Sleep-Seconds: <int>`: seconds until the next scheduled refresh (the next :00 or :30 in the display
  timezone, plus ~20 s so the server sees the new half hour). The firmware sleeps this long; if the header is
  missing or out of range (< 60 or > 3600) it falls back to its own RTC alignment or 1800 s.
- `Date`: standard HTTP date (the firmware may use it to set its RTC).
- `X-Firmware-Update: <n>` (added 2026-10-25, additive; only when present): a firmware build number **greater than**
  the request's `fw` is available. Absent when `fw` is missing or invalid, when it is current, or when no build is
  published. Firmware without OTA support ignores it. The device then fetches
  `GET /firmware/inkplate_weather-<n>.bin` (`200`, `application/octet-stream`, `Content-Length`, and
  `x-MD5: <32 hex>`, which the ESP32 updater verifies; `404` if that build isn't published).

Builds are files in `<cache dir>/firmware/` (`INKPLATE_CACHE_DIR`, else `server.cache_dir`; on the Pi `~/inkplate-weather/cache/firmware/`):
`inkplate_weather-<n>.bin`, optionally with a `manifest.json` (`version`, `file`, `md5`, ...) written by
`firmware/build.ps1 -Export`. Without a manifest the highest-numbered file is offered. Code: `server/app/firmware.py`.

Any non-200 status, a timeout (20 s), or a body that doesn't decode is a failure: the firmware draws its local
error screen and retries in 5 min, up to 3 times, before returning to the normal schedule.
If rendering fails inside the server, it still returns `200` with the last good image plus an error strip.

## Container

- Base image: `mcr.microsoft.com/playwright/python` (arm64) or `python:3.12-slim` + `playwright install --with-deps chromium`.
  Adds Pillow (also decodes MRMS GRIB2), h5py (GLM lightning), FastAPI/uvicorn, httpx, Jinja2, astral, PyYAML.
- Fonts (Inter Tight) and icons are **baked into the image**; no internet needed to render.
- `docker-compose.yml` for the OMV compose plugin:
  - port `8080`
  - volumes: `./config` (config.yaml, secrets via env), `./cache` (provider cache, last-good image, radar frames)
  - `restart: unless-stopped`, healthcheck on `/health`
  - environment: `HA_TOKEN`, `TZ=America/Chicago`
- Resource expectations: about 300–500 MB RAM while rendering (Chromium), idle near 150 MB, image about 1 GB.

## Firmware (Inkplate 10)

- Arduino framework with the official Inkplate library (`drawImage` from a URL handles PNG).
- Wake on timer (aligned to :00 and :30) **or** the wake button; read battery; connect to Wi-Fi; `GET /display.png?...`;
  full refresh; deep sleep.
- If Wi-Fi or the server fails: draw a local text screen ("Can't reach the weather server at 192.168.1.20.
  Will retry at 3:30 PM."), keep the previous image otherwise, retry sooner (5 min) up to 3 times.
- Wi-Fi and server address: stored in NVS (ESP32 `Preferences`), with `config.h` values compiled in as the defaults
  used while NVS is empty. A **captive-portal setup** (SoftAP `Inkplate-Weather-XXXX`, WPA2 with a password printed on
  the e-paper, DNS catch-all, one-page form with a network scan) opens when no SSID is configured, when the wake
  button is held about 5 s, and is suggested on the error screen after 3 consecutive failed retry cycles with Wi-Fi as
  the cause (the radio is never left on for that). It closes after 10 minutes. Built-in ESP32 libraries only.
- **OTA updates are pulled** (the device sleeps, so push-OTA doesn't fit): the firmware reports `fw=<FW_VERSION>`
  (`version.h`) on every `/display.png` request; when the reply has `X-Firmware-Update: <n>` and the battery is at
  least 40 % (or no battery is detected), it draws the picture first, then downloads
  `/firmware/inkplate_weather-<n>.bin` with `HTTPUpdate` (MD5-checked) and restarts. A failed attempt is retried no
  sooner than 6 hours later (timestamp in NVS). No automatic rollback.
- Partition table: `no_fs` (two 2 MB app slots + `otadata`); the board's default `huge_app` has no OTA slot, so the
  first flash with this firmware must be over USB. See `firmware/README.md`.

## Repository layout (planned)

```
design/        approved design (done)
docs/          this document
server/
  app/         FastAPI app, collector, resolver, providers/, render/
  templates/   dashboard.html.j2 (from design/source/dashboard.html), partials
  assets/      fonts, icons (from design/icons), base map GeoJSON
  fixtures/    saved data sets for fall / winter / alert / outage
  tests/
  Dockerfile
docker-compose.yml
firmware/      Inkplate sketch
```

## Build phases

1. **Skeleton:** container, `/display.png` rendering the template from a **fixture** (no live data), quantization, firmware fetch + sleep loop. You'd see the reference board on the device end to end.
2. **Live data:** Ecowitt, NWS obs/forecast/alerts, Open-Meteo, ACIS, astro; resolver, config and error states.
3. **Radar:** MRMS frame + precip type, base map, IEM fallback.
4. **Polish:** storm motion and arrival lines, day-line generation, winter behaviors, `/status` page, HA provider.

## Open questions

Answered 2026-10-24: gateway at `192.168.1.135` (garage = channel 1; the station also reports wind, UV/solar
and lightning); air quality from public data for now (PMS5003 later, likely via Home Assistant); Inkplate
battery does **not** need to go to Home Assistant. NWS `User-Agent` contact: the project's GitHub URL,
`(InkplateWeather, https://github.com/aerogeek217/InkplateWeather)` (public, so it can live in the example config).
GLM lightning overlay approved with bolt symbols (see *Lightning*); the station's own lightning sensor is not used.

No open questions at the moment.

## Sources consulted

- IEM OGC/tile services: https://mesonet.agron.iastate.edu/ogc/
- MRMS data directory: https://mrms.ncep.noaa.gov/data/2D/
- NWS API documentation: https://www.weather.gov/documentation/services-web-api
- RainViewer API transition (2026 free-tier limits): https://rainviewer.com/api/transition-faq.html
- NOAA GOES-R GLM Level 2 dataset: https://catalog.data.gov/dataset/noaa-goes-r-series-geostationary-lightning-mapper-glm-level-2-lightning-detection-events-g
- GOES-19 GLM Beta readme: https://noaasis.noaa.gov/pdf/ps-pvr/goes19/GLM/Beta/GOES-19_GLM_Beta_ReadMe.pdf
- Blitzortung data terms: https://www.Blitzortung.org/contact.php
- Ecowitt local API (community documentation; no official spec found): https://www.cumuluswiki.org/ and the `wittiot` Python library (https://pypi.org/project/wittiot)
