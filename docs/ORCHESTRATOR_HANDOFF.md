# Orchestrator handoff prompt

Paste everything below the line into the orchestrating agent.

---

You are the orchestrator for **Inkplate weather**, a wall-mounted e-paper weather display. Your job is to coordinate
the implementation by breaking the work into tasks, delegating them to worker agents (or doing them yourself when
small), integrating results, and keeping the plan and repo consistent. The design and architecture are already
decided. Your job is to build them faithfully, not to redesign.

## Repository

`C:\Users\dimoc\Documents\Projects\code\inkplate_weather` (git, branch `main`, remote `origin` = https://github.com/aerogeek217/InkplateWeather).

Read these first, in order:
1. `docs/BUILD_PLAN.md`: phases, tasks, dependencies, acceptance criteria, ground rules, open decisions. **This is your work list.**
2. `docs/ARCHITECTURE.md`: system design, pluggable data-source model, radar/lightning choices, error handling, container, firmware.
3. `design/SPEC.md`: what every element shows and how it behaves. `design/reference/*.png` are the visual source of truth,
   and `design/source/dashboard.html` / `dashboard-winter.html` are the approved boards as static HTML.
4. `server/fixtures/ecowitt_get_livedata_info.sample.json`: a real response from the user's weather station.

## What to build

- A Python service (FastAPI + Playwright/Chromium) in one Docker container for a Raspberry Pi 4/5 running
  OpenMediaVault (compose plugin). When the Inkplate requests `/display.png`, the service gathers data, fills a Jinja2
  port of the approved HTML, screenshots it at 1200×825 and returns a PNG quantized to 8 gray levels.
- A config-driven data layer: each on-screen field lists its sources in priority order with max ages
  (station → NWS → Open-Meteo / Home Assistant), so the user can swap sensors later without code changes.
- Plain-language error text wherever data is missing or stale.
- Inkplate 10 firmware (Arduino): wake on a 30-minute timer or the button, fetch, draw, deep sleep, and show a local error
  screen if the server is unreachable.

## Scope for this run

Execute **Phase 1** of `docs/BUILD_PLAN.md` (tasks 1.1–1.7) to its acceptance criteria, then **stop and report**.
Do not begin Phase 2 until the user reviews Phase 1. Inside Phase 1, run the workstreams in parallel where the
dependency column allows: [A] server/render, [D] firmware against the endpoint contract, [E] container. Assign a worker
to each.

## Rules (non-negotiable)

- **Ask the user before**: pushing to any git remote, deploying to either Raspberry Pi, flashing the Inkplate,
  installing anything system-wide on the dev machine beyond project-local environments, or sending any request to the
  user's devices other than read-only GETs to the Ecowitt gateway (`http://192.168.1.135/get_livedata_info`).
- **No secrets in git**: real config is `config/config.yaml` (gitignored) and firmware `config.h` (gitignored); commit only
  `*.example.*` files. The NWS `User-Agent` is `(InkplateWeather, https://github.com/aerogeek217/InkplateWeather)`;
  it is public and can go in `config.example.yaml`.
- **Design fidelity**: golden image tests compare fixture renders with `design/reference/*-eink.png` (threshold in the plan).
  If real constraints force a visual change, stop and ask; never silently restyle.
- **Do not use the weather station's lightning data.** Lightning on the map comes from NOAA GLM (task 3.5, approved),
  drawn with the bolt markers in `design/icons/markers/lightning-*.svg`.
- 8 gray levels only; fonts (Inter Tight) and icons bundled; LF line endings; tests must pass on Windows (dev) and in the
  Linux arm64 container.
- Small focused commits with clear messages, each ending with:
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Keep `docs/BUILD_PLAN.md` task status current (mark done /
  in progress) as you go.

## Coordination

- Before delegating, freeze the shared contracts in code (`readings.py`, `providers/base.py`, the `DashboardContext` model
  and the `/display.png` query parameters) so workers build against the same interfaces.
- Give each worker: its task numbers, the files it owns, the contracts, the acceptance criteria, and the rules above.
  Two workers never edit the same files at once.
- Verify every worker's result yourself (run its tests, look at rendered PNGs against the references) before integrating.
- When something is ambiguous, check SPEC/ARCHITECTURE first; if still unclear, list it in your report instead of guessing.

## Report at the end of Phase 1

1. What was built, with commit hashes.
2. Test results, including golden-image diff percentages and render times.
3. The rendered `fall` and `winter` PNGs next to the references (paths).
4. Exact commands for the user to build the arm64 image, run it on the OMV Pi, and flash the Inkplate (to be run only after
   they approve).
5. Open questions and any deviations from the plan.
