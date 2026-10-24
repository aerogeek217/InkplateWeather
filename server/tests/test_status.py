"""/status, /status.json, /admin/reload and the history sampler (fixture mode and live mode with fake providers)."""
import asyncio
import types
from datetime import timedelta

import pytest
from conftest import TEST_CONFIG

from app import main, status
from app.cache import ProviderCache
from app.history import HistoryStore
from app.providers.base import register
from app.readings import Reading

SECRET = "s3cret-token-value"


@register("fake_station")
class FakeStation:
    calls = 0

    def __init__(self, name, cfg, app):
        self.name, self.ttl = name, cfg.ttl
        self.fail = cfg.options.get("fail", False)

    async def fetch(self, ctx):
        type(self).calls += 1
        if self.fail:
            raise ConnectionError("station offline")
        return {"temp": Reading(value=58.4, unit="°F", observed_at=ctx.now - timedelta(minutes=2)),
                "rel": Reading(value=29.94, unit="inHg", observed_at=ctx.now - timedelta(minutes=2))}


LIVE_CONFIG = """
location: {{name: "Savoy, IL", lat: 40.0617, lon: -88.2517, tz: America/Chicago}}
server: {{cache_dir: unused}}
providers:
  station: {{type: fake_station, ttl: {ttl}, url: "http://admin:pw123@192.168.1.135/x", token: {secret},
             token_env: HA_TOKEN, nested: {{password: hunter2}}{extra}}}
  backup: {{type: fake_station, ttl: 10m, fail: true}}
  history: {{type: history, ttl: 0s, sample_minutes: 0}}
fields:
  outdoor.temperature: [{{from: station, key: temp, max_age: 10m}}, {{from: backup, key: temp}}]
  pressure.sea_level: [{{from: station, key: rel, max_age: 15m}}]
  outdoor.temperature_history: [{{from: history, max_age: 40m}}, {{from: backup, key: hist}}]
  pressure.trend_3h: [{{from: history, max_age: 40m}}]
"""


def write_config(path, ttl="5m", extra=""):
    path.write_text(LIVE_CONFIG.format(ttl=ttl, secret=SECRET, extra=extra), encoding="utf-8")


@pytest.fixture
def fixture_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("INKPLATE_CONFIG", str(TEST_CONFIG))
    monkeypatch.setenv("INKPLATE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("DEPLOYED_COMMIT", raising=False)
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def live(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    cfg = tmp_path / "config.yaml"
    write_config(cfg)
    monkeypatch.setenv("INKPLATE_CONFIG", str(cfg))
    monkeypatch.setenv("INKPLATE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("DEPLOYED_COMMIT", "abc1234")
    with TestClient(main.app) as c:
        yield c, cfg, tmp_path / "cache"


# ------------------------------------------------------------------ fixture mode


def test_status_in_fixture_mode(fixture_client):
    data = fixture_client.get("/status.json").json()
    assert data["mode"] == "fixture" and data["fixture"] == "fall"
    assert data["deployed_commit"] in ("unknown", status.deployed_commit())
    by_field = {f["field"]: f for f in data["fields"]}
    t = by_field["outdoor.temperature"]
    assert t["status"] == "ok" and t["source"] == "fixture" and t["value"] == 58.4
    assert t["attempts"] == [{"source": "fixture", "key": "outdoor.temperature", "outcome": "ok", "note": None,
                              "age_s": 0.0, "age": "0 s", "last_seen": t["observed_at"]}]
    assert by_field["forecast.daily"]["value"] == "list of 7"
    assert all(f["attempts"] for f in data["fields"])
    assert [p["name"] for p in data["providers"]] == ["fixture"]

    html = fixture_client.get("/status")
    assert html.status_code == 200 and html.headers["content-type"].startswith("text/html")
    assert "Fixture mode" in html.text and "outdoor.temperature" in html.text and "Outdoor temperature" in html.text


# ------------------------------------------------------------------ live mode


def test_status_live_shows_provenance_health_and_hides_secrets(live):
    client, _, _ = live
    r = client.get("/status.json")
    assert r.status_code == 200
    data = r.json()
    assert data["mode"] == "live" and data["deployed_commit"] == "abc1234"
    by_field = {f["field"]: f for f in data["fields"]}

    t = by_field["outdoor.temperature"]
    assert (t["status"], t["source"], t["value"], t["unit"], t["age"]) == ("ok", "station", 58.4, "°F", "2 min")
    assert t["display"] == "58.4 °F" and t["message"] is None

    hist = by_field["outdoor.temperature_history"]  # no history yet, backup provider down
    assert hist["status"] == "missing"
    assert [(a["source"], a["outcome"], a["note"]) for a in hist["attempts"]] == [
        ("history", "missing", "insufficient_history"), ("backup", "error", "provider_error:ConnectionError")]
    assert hist["message"] == ("Temperature history unavailable: the station history hasn't recorded enough yet. "
                               "Retrying next update.")

    provs = {p["name"]: p for p in data["providers"]}
    assert provs["station"]["served"] == "live" and provs["station"]["ttl"] == "5 min"
    assert provs["station"]["last_success"] and provs["station"]["used_by"] == ["outdoor.temperature",
                                                                                "pressure.sea_level"]
    assert provs["backup"]["served"] == "error" and "station offline" in provs["backup"]["last_error"]
    opts = provs["station"]["options"]
    assert opts["token"] == opts["token_env"] == opts["nested"]["password"] == status.REDACTED
    assert opts["url"] == f"http://{status.REDACTED}@192.168.1.135/x"
    for secret in (SECRET, "HA_TOKEN", "hunter2", "pw123"):
        assert secret not in r.text

    # history was recorded by the live resolve
    assert data["history"]["series"]["outdoor.temperature"]["points"] == 1
    assert data["history"]["chart"] == {"ok": False, "points": 0, "note": "insufficient_history"}

    # a second look within the TTL is served from the cache
    data2 = client.get("/status.json").json()
    assert {p["name"]: p for p in data2["providers"]}["station"]["served"] == "cache"

    html = client.get("/status").text
    assert "Deployed commit" in html and "abc1234" in html and "insufficient_history" in html
    assert SECRET not in html and "hunter2" not in html


def test_reload_form(live):
    client, _, _ = live
    r = client.get("/admin/reload")
    assert r.status_code == 200 and 'method="post"' in r.text


def test_reload_keeps_unchanged_caches_and_resets_changed(live):
    client, cfg, cache = live
    client.get("/status.json")
    assert (cache / "providers" / "station.json").is_file()

    write_config(cfg, ttl="10m")  # TTL only: cached data stays
    r = client.post("/admin/reload")
    assert r.status_code == 200, r.text
    assert "Cache kept: backup, history, station." in r.text
    assert client.app.state.config.providers["station"].ttl == timedelta(minutes=10)
    assert (cache / "providers" / "station.json").is_file()

    write_config(cfg, ttl="10m", extra=", host: 10.0.0.2")  # options changed: reset
    r = client.post("/admin/reload")
    assert r.status_code == 200 and "Cache reset (config changed): station." in r.text
    assert not (cache / "providers" / "station.json").exists()
    assert client.get("/status.json").status_code == 200


@pytest.mark.parametrize("bad,expect", [
    ("location: [unclosed\n", "YAML syntax error"),
    ("location: {name: x, lat: 1, lon: 2}\nfields: {outdoor.temperature: [{from: nope}]}\n", "unknown provider"),
    ("location: {name: x, lat: 1, lon: 2}\nproviders: {p: {type: no_such_type, token: zzz-secret}}\n",
     "unknown type"),
    ("location: {name: x, lat: 1, lon: 2}\nfields: {not.a.field: []}\n", "unknown field"),
])
def test_reload_rejects_bad_config_and_keeps_the_old_one(live, bad, expect):
    client, cfg, _ = live
    write_config(cfg)
    old = client.app.state.config
    cfg.write_text(bad, encoding="utf-8")
    r = client.post("/admin/reload")
    assert r.status_code == 400
    assert "keeping the previous config" in r.text and expect in r.text
    assert "zzz-secret" not in r.text
    assert client.app.state.config is old
    assert client.get("/status.json").json()["mode"] == "live"


# ------------------------------------------------------------------ sampler


def _state(tmp_path, cfg_text):
    from app import config

    p = tmp_path / "config.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    cfg = config.load(p)
    return types.SimpleNamespace(config=cfg, providers=None, provider_errors={}, cache=ProviderCache(tmp_path),
                                 history=HistoryStore(tmp_path / "history.json"), sampler=None)


async def test_sampler_records_station_fields_from_primary_source_only(tmp_path, monkeypatch):
    monkeypatch.setenv("INKPLATE_CACHE_DIR", str(tmp_path))
    st = _state(tmp_path, LIVE_CONFIG.format(ttl="0s", secret="x", extra=""))
    st.cache.configure(st.config)
    FakeStation.calls = 0
    await main.sample_history(st)
    assert [p.v for p in st.history.series("outdoor.temperature")] == [58.4]
    assert [p.v for p in st.history.series("pressure.sea_level")] == [29.94]
    assert st.cache.health()["backup"].last_fetch is None  # fallback sources aren't polled by the sampler
    assert st.cache.health()["station"].served == "live"
    assert FakeStation.calls == 1  # one fetch serves both fields


def test_sample_interval_from_config():
    from app.config import AppConfig

    def cfg(opts):
        return AppConfig.model_validate({"location": {"name": "x", "lat": 1, "lon": 2},
                                         "providers": {"history": {"type": "history", **opts}}})

    assert main.history_sample_minutes(cfg({})) == 10
    assert main.history_sample_minutes(cfg({"sample_minutes": 0})) == 0
    assert main.history_sample_minutes(cfg({"sample_minutes": "15"})) == 15
    assert main.history_sample_minutes(cfg({"sample_minutes": "often"})) == 10


async def test_sampler_task_runs_and_stops(tmp_path, monkeypatch):
    monkeypatch.setenv("INKPLATE_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(main, "SAMPLER_FIRST_DELAY_S", 0)
    st = _state(tmp_path, LIVE_CONFIG.format(ttl="0s", secret="x", extra="").replace("sample_minutes: 0",
                                                                                      "sample_minutes: 10"))
    main._start_sampler(st)
    assert st.sampler is not None
    for _ in range(50):
        if st.history.series("outdoor.temperature"):
            break
        await asyncio.sleep(0.01)
    assert st.history.series("outdoor.temperature")
    task = st.sampler
    main._stop_sampler(st)
    with pytest.raises(asyncio.CancelledError):
        await task


def test_no_sampler_in_fixture_mode_or_when_disabled(fixture_client):
    assert fixture_client.app.state.sampler is None


# ------------------------------------------------------------------ helpers


@pytest.mark.parametrize("secs,text", [(None, ""), (0, "0 s"), (89, "89 s"), (120, "2 min"), (5400, "1 h 30 min"),
                                       (7200, "2 h"), (200000, "2 d 7 h"), (-300, "in 5 min")])
def test_age_text(secs, text):
    assert status.age_text(secs) == text


def test_deployed_commit_from_file(tmp_path, monkeypatch):
    monkeypatch.delenv("DEPLOYED_COMMIT", raising=False)
    monkeypatch.chdir(tmp_path)
    before = status.deployed_commit()
    (tmp_path / "DEPLOYED_COMMIT").write_text("caca703\n", encoding="utf-8")
    assert status.deployed_commit() == ("caca703" if before == "unknown" else before)
