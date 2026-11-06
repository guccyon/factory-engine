"""サービスごとの版の一覧（ADR 0031）。docker inspect の結果を与えて、ずれの判定を見る（docker も DB も使わない）"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import version


def c(name, image, ver=None, *, job=False, state="running"):
    labels = {"factory.job": "12"} if job else {"com.docker.compose.project": "factory", "com.docker.compose.service": name}
    if ver:
        labels["factory.engine_version"] = ver
    return {"Name": "/" + name, "State": {"Status": state}, "Config": {"Image": image, "Labels": labels}}


def test_all_same_version_is_ok():
    rows = version.rows_from_inspect([c("dispatcher", "factory-dispatcher:p0", "6d12e6e"), c("supervisor", "factory-dispatcher:p0", "6d12e6e"),
                                      c("postgres", "postgres:17-alpine")])
    text, code = version.report(rows, "6d12e6e1234")
    assert code == 0 and "6d12e6e" in text
    assert [r["version"] for r in rows if r["name"] == "postgres"] == ["-"]


def test_left_behind_service_is_flagged():
    # 2026-11-05: console と supervisor だけ上げ、dispatcher が古いまま
    rows = version.rows_from_inspect([c("dispatcher", "factory-dispatcher:p0", "c9fe85b"), c("supervisor", "factory-dispatcher:p0", "6d12e6e"),
                                      c("console", "factory-shipper:p2", "6d12e6e")])
    assert [r["name"] for r in version.mismatched(rows)] == ["dispatcher"]
    _, code = version.report(rows, "6d12e6e")
    assert code == 1


def test_unknown_version_and_stopped_service_are_flagged():
    rows = version.rows_from_inspect([c("dispatcher", "factory-dispatcher:p0"), c("desk", "factory-dispatcher:p0", "6d12e6e", state="exited"),
                                      c("objectstore", "versity/versitygw:v1.10.0", state="exited")])
    names = {r["name"] for r in version.mismatched(rows, "6d12e6e")}
    assert names == {"dispatcher", "desk", "objectstore"}


def test_jobs_are_listed_but_not_counted():
    rows = version.rows_from_inspect([c("dispatcher", "factory-dispatcher:p0", "6d12e6e"), c("factory-job-12", "factory-worker:p0", "c9fe85b", job=True),
                                      {"Name": "/other", "State": {"Status": "running"}, "Config": {"Image": "x", "Labels": {}}}])
    assert [r["kind"] for r in rows] == ["service", "job"]
    assert version.mismatched(rows, "6d12e6e") == []


def test_short_and_long_commits_match():
    assert version._same("6d12e6e", "6d12e6e0b1c2")
    assert not version._same("6d12", "6d12e6e")
    assert not version._same("unknown", "6d12e6e")


def test_engine_version_prefers_baked_file(tmp_path, monkeypatch):
    f = tmp_path / "ENGINE_VERSION"
    f.write_text("9430cbc\n")
    monkeypatch.setattr(version, "VERSION_FILE", str(f))
    assert version.engine_version({"ENGINE_VERSION": "c9fe85b"}) == "9430cbc"   # .env（env_file）の古い値より焼き込んだ版
    f.write_text("unknown\n")
    assert version.engine_version({"ENGINE_VERSION": "c9fe85b"}) == "c9fe85b"
    monkeypatch.setattr(version, "VERSION_FILE", str(tmp_path / "none"))
    assert version.engine_version({}) == "unknown"
