"""Docker のディスクの見張り（disk_guard.py）: たまったら片付け、足りなければ停止を開き、戻ったら閉じる"""
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import disk_guard as dg  # noqa: E402

URL = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL がない")
GB = dg.GB


def test_size_bytes():
    assert dg.size_bytes("12.5GB") == 12.5e9 and dg.size_bytes("512MB") == 512e6 and dg.size_bytes("0B") == 0 and dg.size_bytes("?") == 0


class Docker:
    def __init__(self, cache="30GB"):
        self.cache, self.calls = cache, []

    def __call__(self, args, timeout=600):
        self.calls.append(args[:2])
        out = ""
        if args[:2] == ["system", "df"]:
            out = "\n".join(json.dumps(r) for r in [{"Type": "Images", "Size": "40GB"}, {"Type": "Build Cache", "Size": self.cache}])
        return subprocess.CompletedProcess(args, 0, out, "")


@pytest.fixture()
def cur():
    import psycopg
    from psycopg.rows import dict_row
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events restart identity cascade")
    yield c
    conn.close()


def open_keys(cur):
    import stoppage
    return stoppage.open_keys(cur, "disk:")


@needs_db
def test_quiet_when_small(cur):
    d = Docker("3GB")
    assert dg.step(cur, lambda *a: None, docker=d, free=lambda: 100 * GB) is None
    assert d.calls == [["system", "df"]]


@needs_db
def test_prunes_cache_when_large(cur):
    d = Docker("30GB")
    rec = dg.step(cur, lambda *a: None, docker=d, free=lambda: 100 * GB)
    assert rec["cache_gb"] > 20 and ["builder", "prune"] in d.calls and ["image", "prune"] not in d.calls
    cur.execute("select count(*) as n from events where name='ops.disk_pruned'")
    assert cur.fetchone()["n"] == 1 and open_keys(cur) == []


@needs_db
def test_low_disk_prunes_images_too_and_opens_then_closes_the_stoppage(cur):
    d = Docker("1GB")
    dg.step(cur, lambda *a: None, docker=d, free=lambda: 2 * GB)
    assert ["builder", "prune"] in d.calls and ["image", "prune"] in d.calls
    assert open_keys(cur) == ["disk:low"]
    dg.step(cur, lambda *a: None, docker=Docker("1GB"), free=lambda: 100 * GB)
    assert open_keys(cur) == []


def test_free_takes_the_smaller_of_vm_and_host(monkeypatch, tmp_path):
    """Mac 本体の側の満杯も見る（11/14 の 2 回目は仮想ディスクの中に 356GB 空きがあった）"""
    import shutil as sh
    monkeypatch.setattr(dg, "HOST_PATH", str(tmp_path))
    monkeypatch.setattr(sh, "disk_usage", lambda p: sh._ntuple_diskusage(100, 0, 2 if p == str(tmp_path) else 50))
    assert dg.free_bytes() == 2
    monkeypatch.setattr(dg, "HOST_PATH", str(tmp_path / "none"))
    assert dg.free_bytes() == 50


def test_stale_images_keep_recent_releases_used_ones_and_new_before_tags():
    from datetime import datetime, timezone
    R = lambda repo, tag, created="2026-11-01 10:00:00 +0900 JST", i="sha:x": {"Repository": repo, "Tag": tag, "CreatedAt": created, "ID": i}  # noqa: E731
    rows = [R("rally-backend", f"0.{n}.0") for n in (4, 9, 10, 31, 32, 33)] + [R("rally-web", "0.5.0", i="sha:used")] + [
        R("factory-dispatcher", "p0-before-a", "2026-11-01 10:00:00 +0900 JST"), R("factory-dispatcher", "p0-before-b", "2026-11-02 10:00:00 +0900 JST"),
        R("factory-dispatcher", "p0-before-c", "2026-11-03 10:00:00 +0900 JST"), R("factory-dispatcher", "p0-before-d", "2026-11-13 10:00:00 +0900 JST"),
        R("factory-dispatcher", "p0-1314e88"), R("<none>", "<none>")]
    now = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)
    out = dg.stale_images(rows, {"rally-backend:0.9.0", "sha:used"}, keep_releases=3, before_days=7, now=now)
    assert sorted(out) == ["factory-dispatcher:p0-before-a", "factory-dispatcher:p0-before-b", "rally-backend:0.10.0", "rally-backend:0.4.0"]
