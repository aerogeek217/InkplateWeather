from datetime import UTC, datetime, timedelta

import pytest

from app import messages
from app.config import AppConfig, SourceCfg, parse_duration
from app.readings import Reading, Status
from app.resolver import resolve, resolve_field

NOW = datetime(2026, 10, 24, 19, 30, tzinfo=UTC)  # 2:30 PM CDT
TZ = "America/Chicago"


def src(provider, key=None, max_age=None):
    return SourceCfg.model_validate({"from": provider, "key": key, "max_age": max_age})


def reading(value, minutes_old=1, **kw):
    return Reading(value=value, observed_at=NOW - timedelta(minutes=minutes_old), **kw)


def test_first_fresh_source_wins_with_provenance():
    results = {"ecowitt": {"0x02": reading(58.1)}, "nws_obs": {"outdoor.temperature": reading(57.0)}}
    r = resolve_field("outdoor.temperature", [src("ecowitt", "0x02", "10m"), src("nws_obs")], results, NOW)
    assert r.reading.value == 58.1
    assert r.reading.source == "ecowitt"
    assert r.reading.status is Status.ok
    assert [a.outcome for a in r.tried] == [Status.ok]
    assert not r.fallback_used


def test_falls_back_in_order_when_first_is_missing_or_errored():
    results = {"ecowitt": RuntimeError("timeout"), "nws_obs": {"outdoor.temperature": reading(57.0)}}
    r = resolve_field("outdoor.temperature", [src("ecowitt", "0x02"), src("nws_obs")], results, NOW)
    assert r.reading.value == 57.0 and r.reading.source == "nws_obs"
    assert [a.outcome for a in r.tried] == [Status.error, Status.ok]
    assert r.tried[0].note == "provider_error:RuntimeError"
    assert r.fallback_used


def test_missing_key_and_missing_status_are_skipped():
    results = {
        "a": {},
        "b": {"x": Reading(status=Status.missing, note="sensor_offline")},
        "c": {"x": reading(3)},
    }
    r = resolve_field("uv.index", [src("a", "x"), src("b", "x"), src("c", "x")], results, NOW)
    assert r.reading.value == 3
    assert [a.note for a in r.tried] == ["no_such_key", "sensor_offline", None]


def test_older_than_max_age_is_stale_and_a_fresh_fallback_beats_it():
    results = {"ecowitt": {"t": reading(58, minutes_old=30)}, "nws_obs": {"t": reading(57, minutes_old=20)}}
    r = resolve_field("outdoor.temperature", [src("ecowitt", "t", "10m"), src("nws_obs", "t", "90m")],
                      results, NOW)
    assert r.reading.value == 57 and r.reading.status is Status.ok
    assert r.tried[0].outcome is Status.stale and r.tried[0].age == timedelta(minutes=30)


def test_stale_used_when_nothing_fresh():
    results = {"ecowitt": {"t": reading(58, minutes_old=30)}, "nws_obs": RuntimeError()}
    r = resolve_field("outdoor.temperature", [src("ecowitt", "t", "10m"), src("nws_obs", "t")], results, NOW)
    assert r.reading.value == 58 and r.reading.status is Status.stale and r.reading.source == "ecowitt"
    assert messages.as_of(r, TZ) == "as of 2:00 PM"
    assert messages.explain(r, TZ) is None


def test_no_max_age_never_stale():
    r = resolve_field("normals.high", [src("acis")], {"acis": {"normals.high": reading(63, 600)}}, NOW)
    assert r.reading.status is Status.ok


def test_all_missing_gives_missing_reading_and_plain_text():
    results = {"ecowitt": {}, "nws_obs": {}}
    r = resolve_field("garage.temperature", [src("ecowitt", "ch1.temp"), src("nws_obs")], results, NOW)
    assert r.reading.status is Status.missing and r.reading.value is None
    assert messages.explain(r, TZ) == "Garage sensor hasn't reported."


def test_missing_with_last_seen_says_since_when():
    last = NOW - timedelta(minutes=78)
    results = {"ecowitt": {"ch1.temp": Reading(status=Status.missing, observed_at=last, note="sensor_offline")}}
    r = resolve_field("garage.temperature", [src("ecowitt", "ch1.temp")], results, NOW)
    assert messages.explain(r, TZ) == "Garage sensor hasn't reported since 1:12 PM."


def test_all_errored_names_the_source():
    r = resolve_field("radar.frame", [src("mrms")], {"mrms": TimeoutError()}, NOW)
    assert r.reading.status is Status.missing
    text = messages.explain(r, TZ, {"mrms": "radar_mrms"})
    assert text == "Radar unavailable: NOAA's radar feed didn't respond. Retrying next update."


def test_resolve_all_fields_and_unfetched_provider():
    cfg = {"uv.index": [src("om")], "air.aqi": [src("om", "us_aqi")]}
    out = resolve(cfg, {}, NOW)
    assert set(out) == {"uv.index", "air.aqi"}
    assert out["air.aqi"].tried[0].note == "not_fetched"


@pytest.mark.parametrize("text,seconds", [("30s", 30), ("10m", 600), ("1.5h", 5400), ("2d", 172800), (45, 45)])
def test_parse_duration(text, seconds):
    assert parse_duration(text) == timedelta(seconds=seconds)


def test_config_rejects_unknown_field_and_provider():
    base = {"location": {"name": "x", "lat": 0, "lon": 0}, "providers": {"p": {"type": "fixture"}}}
    with pytest.raises(ValueError, match="unknown field"):
        AppConfig.model_validate({**base, "fields": {"nope": [{"from": "p"}]}})
    with pytest.raises(ValueError, match="unknown provider"):
        AppConfig.model_validate({**base, "fields": {"uv.index": [{"from": "q"}]}})


def test_example_config_loads():
    from pathlib import Path

    from app.config import load

    cfg = load(Path(__file__).resolve().parents[2] / "config" / "config.example.yaml")
    assert cfg.server.fixture == "fall"
    assert cfg.providers["fixture"].type == "fixture"
    assert cfg.fields["air.aqi"][0].from_ == "fixture"
