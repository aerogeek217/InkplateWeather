"""scripts/backup-config.sh: archive creation, permissions, pruning, failure exit codes."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

SH = shutil.which("sh")
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "backup-config.sh"

pytestmark = pytest.mark.skipif(SH is None or not SCRIPT.is_file(), reason="needs a POSIX sh and the script")


def run(*args: object, **env: str) -> subprocess.CompletedProcess:
    full_env = {**os.environ, **env}
    return subprocess.run([SH, SCRIPT.as_posix(), *map(str, args)], capture_output=True, text=True, env=full_env,
                          timeout=60)


@pytest.fixture
def stack(tmp_path: Path) -> Path:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.yaml").write_text("location: {lat: 1}\n", encoding="utf-8", newline="\n")
    (tmp_path / "config" / "config.example.yaml").write_text("example\n", encoding="utf-8", newline="\n")
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "history.json").write_text("{}", encoding="utf-8")
    return tmp_path


def archives(stack: Path) -> list[Path]:
    return sorted((stack / "backups").glob("config-*.tar.gz"))


def test_archives_config_and_env_only(stack: Path):
    (stack / ".env").write_text("HA_TOKEN=abc\n", encoding="utf-8", newline="\n")
    r = run(stack)
    assert r.returncode == 0, r.stderr
    (arc,) = archives(stack)
    assert arc.name.startswith("config-") and len(arc.name) == len("config-20260101-120000.tar.gz")
    with tarfile.open(arc) as t:
        assert sorted(t.getnames()) == [".env", "config/config.yaml"]
        assert t.extractfile("config/config.yaml").read() == b"location: {lat: 1}\n"
    assert "wrote" in r.stdout
    if os.name != "nt":
        assert stat.S_IMODE(arc.stat().st_mode) == 0o600


def test_works_without_env_and_prints_what_it_did(stack: Path):
    r = run(stack)
    assert r.returncode == 0, r.stderr
    (arc,) = archives(stack)
    with tarfile.open(arc) as t:
        assert t.getnames() == ["config/config.yaml"]
    assert "1 backup(s) kept" in r.stdout


def test_prunes_to_the_newest_n(stack: Path):
    (stack / "backups").mkdir()
    old = [f"config-2020010{d}-000000.tar.gz" for d in range(1, 6)]
    for n in old:
        (stack / "backups" / n).write_bytes(b"x")
    (stack / "backups" / "notes.txt").write_text("keep me")  # unrelated files are never touched
    r = run(stack, 3)
    assert r.returncode == 0, r.stderr
    left = [p.name for p in archives(stack)]
    assert len(left) == 3
    assert left[:2] == old[-2:]  # the two newest old ones survive; the new archive sorts last
    assert left[2].startswith("config-2") and left[2] > old[-1]
    assert (stack / "backups" / "notes.txt").exists()
    assert "removed old" in r.stdout


def test_keep_from_environment_and_default(stack: Path):
    (stack / "backups").mkdir()
    for d in range(1, 4):
        (stack / "backups" / f"config-2020010{d}-000000.tar.gz").write_bytes(b"x")
    assert run(stack, BACKUP_KEEP="1").returncode == 0
    (arc,) = archives(stack)
    arc.unlink()  # a second archive within the same second would be refused
    assert "(limit 14)" in run(stack).stdout  # default


def test_failures_exit_non_zero(stack: Path, tmp_path: Path):
    assert run(tmp_path / "nowhere").returncode != 0
    assert run(stack, "many").returncode != 0
    assert run(stack, 0).returncode != 0
    (stack / "config" / "config.yaml").unlink()
    r = run(stack)
    assert r.returncode != 0 and "config.yaml" in r.stderr
    assert not (stack / "backups").exists() or not archives(stack)
