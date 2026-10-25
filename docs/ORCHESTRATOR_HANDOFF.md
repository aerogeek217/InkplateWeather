# Orchestrator handoff prompt

Written 2026-10-24 (end of the Phase 3 session) for a fresh session. Tell it:
"Read docs/ORCHESTRATOR_HANDOFF.md and proceed." (Earlier versions are in git history: Phase 1 5cb8fab, Phase 2 a4593b5.)


**Update 2026-10-25:** Phase 4 (4.1–4.4) is done and merged into `main` (35c1eb7), deployed on the Pi as
7a90f2b, and firmware v1 is flashed. See `docs/BUILD_PLAN.md` *Phase 4 notes*. Still open: watch the first real storm
(lightning bolts, MRMS ice, storm motion live), the untested setup portal and OTA path, the never-exercised IEM
fallback, and the housekeeping list below. The phase sections below describe the start of Phase 4.

---

You are the orchestrator for **Inkplate weather**, a wall-mounted e-paper weather display. You coordinate the build:
break work into tasks, delegate to worker agents (or do small things yourself), verify and integrate their results, and
keep the plan and repo consistent. Design and architecture are decided; build them faithfully, don't redesign.

## Read first, in order
1. `docs/BUILD_PLAN.md` — phases, task status (✅), ground rules, Phase 1/2/3 notes. **Your work list.**
2. `design/SPEC.md` — what every element shows; approved changes are recorded there with dates (6 mm reflow,
   readability pass, error states, icon set v1.1, radar boards).
3. `docs/ARCHITECTURE.md` — data layer, `/display.png` contract, radar + lightning as built, error handling, container.
4. `docs/DEPLOY.md` — how the OMV Pi deployment works (section A2 is what we use).
5. Your memory file for this project, if present.

## Where things stand (2026-10-24, ~14:45 CDT)
- **Phases 1–3 merged into `main`** (Phase 2 merge eefc7de, Phase 3 merge d8fdfcc), all pushed. 572 tests pass on
  Windows and in the Linux container. The Phase 4 branch `phase-4` is cut from `main` (this file is its first commit).
- **Deployed** on the OMV Pi as f504d4e (= the Phase 3 code in `main`). The Inkplate shows live data with the real
  radar; the user checked a button refresh ("looks good") and the 14:30 timer refresh rendered fine.
- Phase 3 as built (details in BUILD_PLAN *Phase 3 notes* and ARCHITECTURE *Radar*):
  - `app/mapgeo.py` extent: the approved board framing, centered on home, 5.6 map units/mile, ~77 × 81 mi, panel
    536 × 570 px at 1.25 px/unit; equirectangular, so lon/lat grids are one affine transform.
  - Base map `server/assets/basemap/basemap.geojson` from `server/tools/build_basemap.py` (TIGER 2023; needs the
    `basemap` extra, pyshp). Drawn by `app/radarmap.py` (boards' styles; types legend moves roads above echoes).
  - `radar_mrms` (`app/grib2.py` decodes PNG-packed GRIB2 with Pillow; `app/radar.py` classifies into 1-bit masks).
    Ice = liquid echoes over ≤ 0 °C surface (MRMS Model_SurfaceTemp, fetched only with rain in view). Extreme from
    ≥ 50 dBZ only. `radar_iem` fallback (n0q palette index = value). Frames are JSON with base64 masks.
  - `lightning_glm` + `app/lightning.py`: GLM poller (h5py) runs only while the radar shows echoes; bolts 12 px,
    merged per 8 px cell. Not yet seen live (no storms since deploy).
  - Fixtures `radar-storm` / `radar-winter` (recorded MRMS; `tests/build_radar_fixtures.py`) with approved goldens
    in `design/reference/` (+ `windows/`), rendered by `server/tools/render_fixture_refs.py`.

## Next: Phase 4 (polish) — ask the user which to start; nothing is approved yet
- **4.1 Storm motion** (most uncertain): motion from 2–3 recent MRMS frames (cross-correlation of the echo field),
  dashed arrival lines with black time tags and the motion arrow ("15 mph") as on the fall board, "arrives ~4 PM"
  callout; in winter the snow-line positions ("SNOW 6 PM") and the bold current snow line. Needs a small frame
  history (the MRMS provider currently keeps only the latest frame; `/2D/<product>/` lists timestamped files).
  Any drawing beyond what the approved boards show is a design change: mock it, render it, get approval.
- 4.2 Day-line generation improvements; optional lightning slot (only if the user approves a design change).
- 4.3 Deployment guide for the OMV compose plugin; backup of `config/`; log rotation.
- 4.4 Optional firmware: captive-portal Wi-Fi setup, OTA updates.
- Also open for the user: watch the first real storm for lightning bolts and MRMS ice; the IEM fallback has never
  been exercised live.

## Open items / things to watch
- Pi live config: `~/inkplate-weather/config/config.yaml` now has the mrms/iem/glm providers and the
  radar.frame / lightning.flashes fields; backups `config.phase2.yaml.bak`, `config.phase1.yaml.bak`.
- Known minor (reported, no action requested): tightest header (battery ≤ 5 % + Severe T-storm Warning) shows the
  alert tag only; on the winter test board two trend labels sit on the line; town labels near the panel edge (Clinton,
  Mattoon) can sit close to the border or the "Next:" chip.
- The user's station wind/solar/UV array dropped out around 13:30 but was reporting again by 13:40.
- Housekeeping the user may ask for: delete `.claude/worktrees/*` and the `worker-a` / `worktree-agent-*` branches;
  `C:\Users\dimoc\AppData\Local\Arduino15\inventory.yaml` and `C:\Users\dimoc\AppData\Local\arduino\` (left by a
  worker); old local Docker images (`inkplate-weather:a2-reflow`, `a5`, `arm64`, `phase3`); optionally install `gh`
  (not installed — give compare links for PRs instead).
- **Usage:** the account's weekly all-models limit was near its cap on 2026-10-24. Prefer doing small things yourself
  and keep workflows small.

## Environment facts
- Repo `C:\Users\dimoc\Documents\Projects\code\inkplate_weather`, remote https://github.com/aerogeek217/InkplateWeather.
  Windows 11; Git Bash + PowerShell. Python venv `server/.venv` (uv, Python 3.13; `uv pip install --python
  server/.venv/Scripts/python.exe …`). Run tests from `server/`:
  `PLAYWRIGHT_BROWSERS_PATH=0 .venv/Scripts/python.exe -m pytest -q -p no:cacheprovider` (Chromium lives in the venv).
  Windows Python doesn't understand `/c/...` paths inside strings: use `C:/...` there.
- Docker Desktop 20.10.8; amd64 container works; Chromium can't run under its arm64 emulation.
  Container suite (from the repo root, Git Bash):
  `docker build -t inkplate-weather:phaseN server`, then
  `MSYS_NO_PATHCONV=1 docker run --rm -u 0 -v "$PWD/design:/design:ro" -v "$PWD/config:/config:ro" -e INKPLATE_CONFIG=/app/tests/data/config.yaml inkplate-weather:phaseN sh -c 'pip install -q pytest pytest-asyncio respx; cd /app && python -m pytest -q -p no:cacheprovider'`.
  The image sets `INKPLATE_CACHE_DIR=/cache`; tests that depend on the cache dir must unset it.
- **OMV Pi**: Pi 4 (arm64, Debian 12, 4 GB) at **192.168.1.254**, `ssh pi@192.168.1.254` with key auth. No git on
  the Pi. Deploy: `git archive --format=tar <branch> docker-compose.yml server config/config.example.yaml docs/DEPLOY.md
  | ssh pi@192.168.1.254 "tar -x -C ~/inkplate-weather && echo <commit> > ~/inkplate-weather/DEPLOYED_COMMIT && cp
  ~/inkplate-weather/DEPLOYED_COMMIT ~/inkplate-weather/server/"`, then `cd ~/inkplate-weather && sudo docker compose
  build && sudo docker compose up -d` (~80 s warm). **Never overwrite `config/config.yaml`** (live config; edit it in
  place after a backup, with the user's approval). Service on port 8080: `/health`, `/status`, `/status.json`
  (shows `deployed_commit`, `last_display_request`), `/fixtures/<name>.png`.
- **Ecowitt GW2000B** at 192.168.1.135 — read-only `GET /get_livedata_info` is the only allowed request to it.
  Its lightning block is ignored (user decision).
- **Inkplate 10 (Soldered)** on **COM3**; firmware in `firmware/`, Arduino CLI under `.tools/`;
  `.\firmware\build.ps1 -Upload -Port COM3` flashes (only with the user's go-ahead). `config.h` (Wi-Fi) is gitignored
  — never print or commit it.
- Data sources used by Phase 3: `mrms.ncep.noaa.gov/2D/` (live), `mtarchive.geol.iastate.edu` (MRMS archive:
  SeamlessHSR + PrecipFlag, no ReflectivityAtLowestAltitude), `mesonet.agron.iastate.edu` (n0q mosaic + archive),
  `noaa-goes19.s3.amazonaws.com/GLM-L2-LCFA/` (lightning), `www2.census.gov` (TIGER). Record test data with
  `server/tests/record_mrms.py` / `record_iem.py` (crop to the extent, keep real values).
- Golden references: canonical `design/reference/*.png` rendered inside the container; Windows baseline in
  `design/reference/windows/`. Design boards: `design/tools/render_reference.py`; server-rendered boards (radar):
  `server/tools/render_fixture_refs.py`. Regenerate only when the user approves a visual change.

## Rules (non-negotiable)
- Work on a phase branch (`phase-4`, …) cut from `main`; pushing the phase branch and opening its PR are pre-approved.
  Merge into `main` only when the user says so (they review in chat, then say "ready to merge"; merge locally with
  `--no-ff`, rerun the suite, push `main`).
- **Ask the user before**: deploying to the Pi, editing the Pi's config, flashing the Inkplate, installing anything
  system-wide, any request to the user's devices other than read-only GETs to the Ecowitt gateway (and our own Pi
  service's read-only endpoints), any design/visual change (mock it up, render it, get approval, then port), and
  regenerating golden references.
- No secrets in git (`config/config.yaml`, `firmware/**/config.h` are gitignored). LF line endings. 8 gray levels only.
  Fonts/icons bundled. The icon set (`design/icons`) is canonical; missing icons are proposed as set additions.
- The user finds small text hard to read: keep HTML text ≥ 13 px and chart/map text ≥ ~12 px on screen.
- Small focused commits, each ending with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`; keep
  `docs/BUILD_PLAN.md` status current.

## Lessons (avoid repeating)
- Worker worktrees get cut from `main`, not the current branch: tell every worker to `git merge --ff-only
  <phase-branch>` first and confirm the expected commit is in history. Integrate with `git cherry-pick`, rerun the suite.
- Don't chain `pytest … | tail` with `&& git commit` — tail hides failures. Write pytest output to the scratchpad,
  check its exit code, then commit. Never write scratch output outside the scratchpad.
- Before porting a board edit, verify the recording script reproduces the committed boards exactly.
- Measure fit in the browser (bounding boxes, wrapped spans) before showing the user a layout; for map/radar work,
  crop the panel (x 66–606, y 119–693 on the 1200 × 825 board) and compare side by side with the reference.
- Pillow: saving a palette PNG with `optimize=True` (or a default palette) renumbers the indices — fatal when the
  index is the data (IEM n0q). Pillow's affine `transform` uses pixel centers (cell i covers [i, i+1)).
- respx: use `respx.mock(assert_all_called=False)` when some routes are optional.
- Big downloads (MRMS surface temperature ~14 MB/h, GLM ~1 GB/day) must be gated on need; say so in docs.
