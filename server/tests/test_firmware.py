"""Over-the-air firmware hosting (app/firmware.py): the X-Firmware-Update header, /firmware/<file>.bin and /status."""
import hashlib
import json

import pytest
from conftest import TEST_CONFIG

from app import firmware, main

BLOB = b"\xe9" + bytes(range(256)) * 8  # starts with the ESP image magic byte, content is irrelevant


@pytest.fixture
def fw_dir(tmp_path):
    return tmp_path / "cache" / "firmware"


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    async def fake_png(renderer, dctx):  # no Chromium needed: only the headers matter here
        return b"\x89PNG fake"

    monkeypatch.setenv("INKPLATE_CONFIG", str(TEST_CONFIG))
    monkeypatch.setenv("INKPLATE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(main, "render_png", fake_png)
    with TestClient(main.app) as c:
        yield c


def publish(fw_dir, version, data=BLOB):
    fw_dir.mkdir(parents=True, exist_ok=True)
    p = fw_dir / f"inkplate_weather-{version}.bin"
    p.write_bytes(data)
    return p


def manifest(fw_dir, **fields):
    (fw_dir / "manifest.json").write_text(json.dumps(fields), encoding="utf-8")


# ------------------------------------------------------------------ version parsing


@pytest.mark.parametrize("raw,expected", [("3", 3), (" 12 ", 12), (7, 7), ("1000000", None), ("0", None), ("-2", None),
                                          ("abc", None), ("", None), ("1.5", None), (None, None)])
def test_parse_version(raw, expected):
    assert firmware.parse_version(raw) == expected


# ------------------------------------------------------------------ which build is on offer


def test_no_folder_means_no_header(tmp_path):
    assert firmware.latest(tmp_path / "nope") is None
    assert firmware.update_headers(tmp_path / "nope", 1) == {}


def test_header_only_when_newer(tmp_path, fw_dir):
    publish(fw_dir, 3)
    cache = tmp_path / "cache"
    assert firmware.update_headers(cache, 2) == {"X-Firmware-Update": "3"}
    assert firmware.update_headers(cache, 3) == {}  # same version: up to date
    assert firmware.update_headers(cache, 4) == {}  # device is ahead (a dev build)
    assert firmware.update_headers(cache, None) == {}  # old firmware or junk `fw`: never offer


def test_highest_version_wins_numerically(tmp_path, fw_dir):
    for v in (2, 10, 9):  # 10 > 9 as numbers, but not as strings
        publish(fw_dir, v)
    (fw_dir / "inkplate_weather-11.bin.part").write_bytes(b"x")  # half-copied upload
    (fw_dir / "notes.txt").write_text("x")
    assert firmware.latest(tmp_path / "cache").version == 10


def test_empty_file_is_ignored(tmp_path, fw_dir):
    publish(fw_dir, 5, b"")
    publish(fw_dir, 4)
    assert firmware.latest(tmp_path / "cache").version == 4


def test_manifest_names_the_build(tmp_path, fw_dir):
    publish(fw_dir, 2)
    publish(fw_dir, 3)
    md5 = hashlib.md5(BLOB).hexdigest()
    manifest(fw_dir, version=2, file="inkplate_weather-2.bin", md5=md5.upper())
    b = firmware.latest(tmp_path / "cache")
    assert (b.version, b.name, b.md5) == (2, "inkplate_weather-2.bin", md5)


def test_manifest_with_wrong_md5_offers_nothing(tmp_path, fw_dir):
    publish(fw_dir, 2)
    manifest(fw_dir, version=2, file="inkplate_weather-2.bin", md5="0" * 32)
    publish(fw_dir, 1)  # an older, intact build doesn't get offered instead: the whole folder is suspect
    assert firmware.latest(tmp_path / "cache") is None
    assert firmware.update_headers(tmp_path / "cache", 0) == {}


@pytest.mark.parametrize("bad", [
    {"version": 2, "file": "inkplate_weather-3.bin"},  # name and version disagree
    {"version": 2, "file": "../inkplate_weather-2.bin"},  # path tricks
    {"version": "x", "file": "inkplate_weather-2.bin"},
    {"file": "inkplate_weather-2.bin"},
    {"version": 2, "file": "inkplate_weather-9.bin"},  # file missing
])
def test_bad_manifest_is_ignored(tmp_path, fw_dir, bad):
    publish(fw_dir, 2)
    manifest(fw_dir, **bad)
    assert firmware._from_manifest(fw_dir) is None
    assert firmware.latest(tmp_path / "cache").version == 2  # falls back to the folder scan


def test_unparseable_manifest_falls_back(tmp_path, fw_dir):
    publish(fw_dir, 2)
    (fw_dir / "manifest.json").write_text("{not json")
    assert firmware.latest(tmp_path / "cache").version == 2


# ------------------------------------------------------------------ HTTP


def test_display_png_adds_header_for_newer_build(client, fw_dir):
    publish(fw_dir, 4)
    r = client.get("/display.png?batt=80&fw=3")
    assert r.status_code == 200 and r.headers["X-Firmware-Update"] == "4"
    assert "X-Sleep-Seconds" in r.headers  # the rest of the contract is untouched


@pytest.mark.parametrize("query", ["fw=4", "fw=5", "fw=abc", "fw=", "batt=50", ""])
def test_display_png_has_no_header_otherwise(client, fw_dir, query):
    publish(fw_dir, 4)
    r = client.get("/display.png?" + query)
    assert r.status_code == 200 and "X-Firmware-Update" not in r.headers


def test_display_png_without_firmware_folder(client):
    r = client.get("/display.png?fw=1")
    assert r.status_code == 200 and "X-Firmware-Update" not in r.headers


def test_serves_binary_with_md5(client, fw_dir):
    publish(fw_dir, 4)
    r = client.get("/firmware/inkplate_weather-4.bin")
    assert r.status_code == 200 and r.content == BLOB
    assert r.headers["x-md5"] == hashlib.md5(BLOB).hexdigest()
    assert r.headers["content-length"] == str(len(BLOB))
    assert r.headers["content-type"] == "application/octet-stream"


def test_republished_file_gets_a_fresh_md5(client, fw_dir):
    p = publish(fw_dir, 4)
    assert client.get("/firmware/inkplate_weather-4.bin").headers["x-md5"] == hashlib.md5(BLOB).hexdigest()
    new = BLOB + b"more"
    p.write_bytes(new)
    assert client.get("/firmware/inkplate_weather-4.bin").headers["x-md5"] == hashlib.md5(new).hexdigest()


@pytest.mark.parametrize("path", [
    "/firmware/inkplate_weather-9.bin",  # not published
    "/firmware/manifest.json",  # only .bin builds are served
    "/firmware/other-4.bin",
    "/firmware/inkplate_weather-4.bin.part",
    "/firmware/..%2Fconfig.yaml",
    "/firmware/inkplate_weather-%2e%2e.bin",
])
def test_other_paths_are_404(client, fw_dir, path):
    publish(fw_dir, 4)
    (fw_dir / "inkplate_weather-4.bin.part").write_bytes(b"x")
    assert client.get(path).status_code == 404


def test_status_shows_available_and_reported(client, fw_dir):
    s = client.get("/status.json").json()["firmware"]
    assert s == {"reported": None, "available": None, "file": None, "size": None, "update_pending": False}

    publish(fw_dir, 4)
    client.get("/display.png?fw=3")
    s = client.get("/status.json").json()["firmware"]
    assert (s["reported"], s["available"], s["file"], s["update_pending"]) == (3, 4, "inkplate_weather-4.bin", True)
    assert "v4 available (update pending)" in client.get("/status").text

    client.get("/display.png?fw=4")
    s = client.get("/status.json").json()["firmware"]
    assert (s["reported"], s["update_pending"]) == (4, False)
