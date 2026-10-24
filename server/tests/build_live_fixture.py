"""Build fixtures from the recorded provider responses (offline; every request is mocked).

- `live-2026-10-24.json`: the live providers of config/config.example.yaml against tests/data/recorded/ (NWS,
  Open-Meteo, ACIS, the Ecowitt gateway) at 11:45 AM CDT on 2026-10-24, when the NWS/Open-Meteo responses were
  recorded. The radar has no recording yet (phase 3), so it stays missing.
- `live-winter-2027-01-19.json`: the crafted winter-storm NWS responses (forecast, grid, alerts) parsed by the real
  providers at the winter board's time; station readings, radar artwork and the rest come from fixtures/winter.json.
  It carries no day line / callout / next text, so the generated text shows.

    .venv/Scripts/python.exe tests/build_live_fixture.py      (from server/)
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import respx

TESTS = Path(__file__).resolve().parent
SERVER = TESTS.parent
sys.path.insert(0, str(SERVER))
REC = TESTS / "data" / "recorded"
FIXTURES = SERVER / "fixtures"
TZ = ZoneInfo("America/Chicago")
FALL_NOW = datetime(2026, 10, 24, 11, 45, tzinfo=TZ)
WINTER_NOW = datetime(2027, 1, 19, 15, 30, tzinfo=TZ)
API = "https://api.weather.gov"
GRID = f"{API}/gridpoints/ILX/95,70"
DEVICE = {"battery_pct": 78, "voltage_mv": 3980, "wake_reason": "timer"}
GENERATED = ("summary.day_line", "summary.next_hazard", "radar.callout")


def rec(name: str) -> dict:
    return json.loads((REC / name).read_text(encoding="utf-8"))


def mock_nws(router: respx.MockRouter, prefix: str, alerts: str) -> None:
    router.get(f"{API}/points/40.0617,-88.2517").respond(json=rec("nws_points.json"))
    router.get(f"{GRID}/forecast/hourly").respond(json=rec(f"{prefix}forecast_hourly.json"))
    router.get(f"{GRID}/forecast").respond(json=rec(f"{prefix}forecast.json"))
    router.get(GRID).respond(json=rec(f"{prefix}griddata.json"))
    router.get(f"{API}/alerts/active").respond(json=rec(alerts))


def mock_fall(router: respx.MockRouter) -> None:
    from app.providers import acis, open_meteo

    mock_nws(router, "nws_", "nws_alerts_point.json")
    router.get(f"{API}/stations/KCMI/observations").respond(json=rec("nws_obs_history.json"))
    router.get(f"{API}/stations/KCMI/observations/latest").respond(json=rec("nws_obs_latest.json"))
    router.get(open_meteo.FORECAST_URL).respond(json=rec("om_forecast_2026-10-24.json"))
    router.get(open_meteo.AIR_URL).respond(json=rec("om_air_2026-10-24.json"))
    router.route(url__startswith=acis.URL).respond(json=rec("acis_2026-10-24.json"))
    router.get("http://192.168.1.135/get_livedata_info").mock(
        return_value=httpx.Response(200, text=(REC / "ecowitt_livedata_1.json").read_text(encoding="utf-8")))


async def collect(now: datetime, only: set[str] | None, mock) -> dict:
    from app import collector, config
    from app.providers.base import DeviceInfo, RequestContext

    cfg = config.load(SERVER.parent / "config" / "config.example.yaml")
    fields = {k: v for k, v in cfg.fields.items() if only is None or k in only}
    dev = DeviceInfo(**DEVICE)
    with respx.mock(assert_all_called=False) as router:
        mock(router)
        resolved = await collector.collect(collector.build_providers(cfg), fields,
                                           RequestContext(now=now, device=dev, config=cfg))
    readings = {}
    for name, r in sorted(resolved.items()):
        if not r.reading.usable or name.startswith("device."):
            continue
        rd = r.reading
        seen = min(rd.observed_at, now) if rd.observed_at else None  # the gateway stamps its wall-clock time
        readings[name] = {"value": rd.value, "unit": rd.unit, "status": rd.status.value,
                          "observed_at": seen.isoformat() if seen else None}
    return readings


def write(name: str, now: datetime, readings: dict) -> None:
    out = FIXTURES / f"{name}.json"
    data = {"now": now.isoformat(), "device": DEVICE, "readings": readings}
    out.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {out} ({len(readings)} readings)")


def main() -> None:
    os.environ["INKPLATE_CACHE_DIR"] = tempfile.mkdtemp(prefix="inkplate-live-")
    write("live-2026-10-24", FALL_NOW, asyncio.run(collect(FALL_NOW, None, mock_fall)))

    nws = {"forecast.hourly", "forecast.daily", "alerts.active", "astro.sun", "astro.moon"}
    live = asyncio.run(collect(WINTER_NOW, nws,
                               lambda r: mock_nws(r, "nws_winter_", "nws_alerts_winter.json")))
    base = json.loads((FIXTURES / "winter.json").read_text(encoding="utf-8"))["readings"]
    readings = {k: v for k, v in base.items() if k not in GENERATED and k not in live}
    write("live-winter-2027-01-19", WINTER_NOW, dict(sorted({**readings, **live}.items())))


if __name__ == "__main__":
    main()
