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
| **MRMS GRIB2** (`mrms.ncep.noaa.gov/data/2D/`): `ReflectivityAtLowestAltitude` or `MergedBaseReflectivityQC`, plus **`PrecipFlag`** | Raw dBZ + **precipitation type** on a 1 km grid, every 2 min | Raw numbers, so we draw our own grays and **rain/ice/snow patterns** exactly as designed; official NOAA data | CONUS files, so we crop to our box; needs a GRIB2 decoder (eccodes) in the image |
| **IEM tiles / WMS** (`mesonet.agron.iastate.edu`): `ridge::ILX-N0B-0` (KILX single site) or `nexrad-n0q` mosaic; WMS GetMap with a bounding box | Colored PNG | Very simple; one request for our exact box | Colored image → must be mapped back to dBZ via the known color ramp; no precip type (IEM's HRRR `REFP` layer gives *modeled* type) |
| NWS ArcGIS `radar_base_reflectivity` MapServer export | Colored PNG for a bounding box | Official, one request | Same color-decoding issue; no type |

**Storm motion and arrival lines** ("Storms ~4 PM", the dashed 3/4/5 PM lines) need motion estimated from
two or three recent frames (cross-correlation of the echo field). That's a **phase 2** feature. Phase 1 shows
the current frame, legend and callout text from the forecast (e.g. "Storms likely 4–6 PM").

## Lightning (map overlay, approved 2026-10-24)

Public options checked 2026-10-24:

| Option | Verdict |
|---|---|
| **NOAA GOES GLM** (Geostationary Lightning Mapper, GOES-East = GOES-19), Level 2 flashes, NetCDF every ~20 s on NOAA's open-data AWS bucket | **Recommended.** Free NOAA data, no key, covers all of Illinois. Total lightning (in-cloud + ground), roughly 8–10 km location accuracy, so good enough for "storms with lightning are here" but not exact strike points. GOES-19 GLM has been in Beta/provisional maturity, so expect occasional data gaps. |
| Blitzortung.org | Not recommended. The site allows viewing for private use, but its raw data is limited to network participants or with explicit permission. |
| Vaisala NLDN, Xweather, AccuWeather Lightning API | Commercial / paid. |

Because GLM arrives as many small files, the container runs a **light background poller** that keeps a rolling
30-minute list of flashes inside the map extent. The render-on-request path then just reads that list, so this is
the one place where data is gathered ahead of time.

Display (approved): small **lightning-bolt symbols** on the radar map, about 12 px tall. Flashes in the last 10 min
are solid black bolts with a white halo; flashes 10–30 min old are outlined bolts (white fill, black outline), so
both read on any radar fill. A legend entry "Lightning · 30 min" appears only when there are flashes. Nearby
flashes are merged so bolts don't overlap (one bolt per ~8 px cell, newest wins). Icons:
`design/icons/markers/lightning-recent.svg`, `lightning-older.svg`.

**Base map**: generated once at build time from public-domain US Census TIGER/Line cartographic files
(state and county lines, primary roads, places) clipped to our ~110 × 120 mile box, saved as GeoJSON and
drawn in the template's SVG.

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
| `GET /display.png?batt=&mv=&reason=` | Render now and return the 8-gray PNG (Inkplate). |
| `GET /preview` | Same page as live HTML in a browser (debugging/design tweaks). |
| `GET /status` | Per-field winner, ages, errors; provider health. |
| `GET /health` | Container health check. |
| `GET /fixtures/<name>.png` | Render a saved data fixture (fall, winter, alert, outage) to compare with `design/reference/`. |

## Container

- Base image: `mcr.microsoft.com/playwright/python` (arm64) or `python:3.12-slim` + `playwright install --with-deps chromium`.
  Adds eccodes (GRIB2), Pillow, FastAPI/uvicorn, httpx, Jinja2, astral, PyYAML.
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
- Server address and Wi-Fi are set in a small config header (or captive-portal setup, later).

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
