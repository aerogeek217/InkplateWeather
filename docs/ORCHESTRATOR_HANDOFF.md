# Orchestrator handoff prompt

Written 2026-10-24 (end of the Phase 1 + Phase 2 session) for a fresh session. Tell it:
"Read docs/ORCHESTRATOR_HANDOFF.md and proceed." (The Phase 1 version of this file is in git history, commit 5cb8fab.)

---

You are the orchestrator for **Inkplate weather**, a wall-mounted e-paper weather display. You coordinate the build:
break work into tasks, delegate to worker agents (or do small things yourself), verify and integrate their results, and
keep the plan and repo consistent. Design and architecture are decided; build them faithfully, don't redesign.

## Read first, in order
1. `docs/BUILD_PLAN.md` — phases, task status (✅/🔄), ground rules, Phase 1/2 notes. **Your work list.**
2. `design/SPEC.md` — what every element shows; approved changes are recorded there with dates (6 mm reflow, readability
   pass, dry-period precip line, alert-name abbreviation, error states, icon set v1.1).
3. `docs/ARCHITECTURE.md` — data layer, `/display.png` contract, error handling, container, firmware.
4. `docs/DEPLOY.md` — how the OMV Pi deployment works (section A2 is what we actually use).
5. Your memory file for this project, if present.

## Where things stand (2026-10-24, evening)
- **Phase 1** merged into `main` (merge commit 4ce8fda). The Inkplate showed the board served from the Pi.
- **Phase 2** (live data) built on branch **`phase-2`** (pushed; tip 3cf35d5), all tasks 2.1–2.7 done and approved:
  providers ecowitt_local, nws_observations/forecast/alerts, open_meteo, acis, astro, home_assistant, device, history;
  TTL cache + last-good; `/status`, `/status.json`, `/admin/reload`; view-model rules + generated day line/callout/next
  chip (`app/summary.py`, fitted with `app/textwidth.py`); error states (all wording in `app/messages.py`).
  Plus two approved design passes (6 mm margin reflow, then −3 px margin and ≥ 13 px small text).
  531 tests pass on Windows and in the Linux container; goldens fall 0.056 %, winter 0.058 % (threshold 0.5 %).
- **Deployed** to the OMV Pi (live config) as commit 2c22f9c at 13:30 CDT. The Inkplate shows live data.
- **Milestone in progress:** Phase 2 runs on live data "for a few days"; the user checks the screen and wording.
  Then open the `phase-2` PR into `main`; the user reviews; the user has previously told you to merge after approval.
- **Next after that:** Phase 3 (radar: 3.1 base map — confirm the map extent with the user first, the radar panel is now
  536 × 570 at 1.25 px per map unit; 3.2 MRMS; 3.3 IEM fallback; 3.4 radar partial; 3.5 GLM lightning bolts).
  The user also accepted starting Phase 3 on its own branch while Phase 2 soaks, if they ask.

## Open items / things to watch
- The user's station wind/solar/UV array stopped reporting around 13:30 (gateway sends `--.-`); the server correctly
  falls back to NWS/Open-Meteo. The user was asked to check the sensor. Not a code issue.
- History (`cache/history.json` on the Pi) needs ~20 h before the 24 h chart switches from KCMI to the station.
- Known minor: in the tightest header (battery ≤ 5 % + Severe T-storm Warning) the alert shows the tag only; on the
  winter test board two trend labels sit on the line. Both reported to the user; no action requested.
- Housekeeping the user may ask for: delete `.claude/worktrees/*` and the `worker-a` / `worktree-agent-*` branches;
  `C:\Users\dimoc\AppData\Local\Arduino15\inventory.yaml` and `C:\Users\dimoc\AppData\Local\arduino\` (left by a
  worker); optionally install `gh` (not installed — give compare links for PRs instead).
- **Usage:** the account's weekly all-models limit was at 100 % (resets ~9 PM CDT 2026-10-24). Parallel workers are
  expensive; prefer doing small things yourself and keep workflows small.

## Environment facts
- Repo `C:\Users\dimoc\Documents\Projects\code\inkplate_weather`, remote https://github.com/aerogeek217/InkplateWeather.
  Windows 11; Git Bash + PowerShell. Python venv `server/.venv` (uv, Python 3.13); run tests with
  `PLAYWRIGHT_BROWSERS_PATH=0 server/.venv/Scripts/python.exe -m pytest -q` from `server/` (Chromium lives in the venv).
- Docker Desktop 20.10.8 with buildx; amd64 container works; Chromium can't run under its arm64 emulation.
- **OMV Pi**: Pi 4 (arm64, Debian 12) at **192.168.1.254**, `ssh pi@192.168.1.254` with key auth from this PC. No git on
  the Pi; deploy with `git archive <branch> docker-compose.yml server config/config.example.yaml docs/DEPLOY.md | ssh … tar -x`
  into `~/inkplate-weather`, copy `DEPLOYED_COMMIT` into `server/`, then `sudo docker compose build && sudo docker compose up -d`
  (pi is not in the docker group; passwordless sudo works). **Never overwrite `config/config.yaml`** on the Pi (it is
  the live config; `config.phase1.yaml.bak` is the old fixture config). Service on port 8080; check `/health`, `/status`.
- **Ecowitt GW2000B** at 192.168.1.135 — read-only `GET /get_livedata_info` is the only allowed request to it.
  The station's lightning block is ignored (user decision). The user calibrated the relative-pressure offset.
- **Inkplate 10 (Soldered)** on **COM3**; firmware in `firmware/`, project-local Arduino CLI under `.tools/`;
  `.\firmware\build.ps1 -Upload -Port COM3` flashes (only with the user's go-ahead). `config.h` (Wi-Fi) is gitignored
  and was filled in by the user — never print or commit it.
- Golden references: canonical `design/reference/*.png` rendered inside the container; Windows baseline in
  `design/reference/windows/` (Edge). Regenerate with `design/tools/render_reference.py` (docstring has the exact
  commands) only when the user approves a design change. Board HTML changes are recorded as scripts in `design/tools/`
  (`reflow_margin.py`, `readability_v2.py`).

## Rules (non-negotiable)
- Work on a phase branch (`phase-3`, …) cut from the right base; pushing the phase branch and opening its PR are
  pre-approved. Merge into `main` only when the user says so.
- **Ask the user before**: deploying to the Pi, flashing the Inkplate, installing anything system-wide, any request to
  the user's devices other than read-only GETs to the Ecowitt gateway, any design/visual change (mock it up on the
  board HTML, render it, get approval, then port), and regenerating golden references.
- No secrets in git (`config/config.yaml`, `firmware/**/config.h` are gitignored). LF line endings. 8 gray levels only.
  Fonts/icons bundled. The icon set (`design/icons`) is canonical; missing icons are proposed as set additions.
- The user finds small text hard to read: keep HTML text ≥ 13 px and chart text ≥ ~12 px on screen.
- Small focused commits, each ending with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`; keep
  `docs/BUILD_PLAN.md` status current.

## Lessons from this session (avoid repeating)
- Worker worktrees get cut from `main`, not the current branch: tell every worker to run
  `git merge --ff-only <phase-branch>` first and confirm the expected commit is in history.
- Integrate worker branches with `git cherry-pick` onto the phase branch, then rerun the full suite yourself.
- Don't chain `pytest … | tail` with `&& git commit` — tail's exit status hides failures. Run pytest, check its exit code,
  then commit.
- Golden tests in the container: mount `design/` and `config/` read-only, set `-e INKPLATE_CONFIG=/app/tests/data/config.yaml`,
  install pytest ad hoc, run as root so `tests/_out` is writable.
- Before porting a board edit, verify the recording script reproduces the committed boards exactly from their prior
  version; a transform once silently shifted an SVG pattern tile (now `<defs>` are skipped).
- Measure fit in the browser (bounding boxes, scrollWidth, wrapped spans) before showing the user a layout.
