"""触れる環境（ADR 0028 の touchable_env）: shipper が入口を残すこと、DB の入れ直し、factoryctl env up の版の選び方。docker と git は使わない。

test_ship.py と同じく、空の Postgres に postgres/init/*.sql を当てて TEST_DATABASE_URL を向ける（なければ飛ばす）。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL がない")

if URL:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb

    import ship
    import touch

REPO = "o/app"
CFG = {
    "compose": "deploy/compose.yaml",
    "images": {"web": {"context": "web", "env": "WEB_IMAGE"}},
    "environments": {"staging": {"env": {"WEB_PORT": 13100, "ADMIN_PORT": 13000}}, "prod": {"env": {"WEB_PORT": 23100}}},
    "seed": {"environments": ["staging"], "service": "backend", "command": ["seed", "--seed", "42"]},
    "verify": {"service": "backend", "url": "http://127.0.0.1:8080/version"},
    "touch": {
        "environments": ["staging"],
        "bind_env": "WEB_BIND_HOST",
        "entries": [{"label": "生徒アプリ", "port_env": "WEB_PORT", "path": "/", "qr": True},
                    {"label": "管理画面", "port_env": "ADMIN_PORT", "path": "/"},
                    {"label": "ポートのない入口", "port_env": "NOPE"}],
        "accounts": {"service": "postgres", "command": ["psql", "-c", "select 1"]},
        "reset": {"service": "postgres", "command": ["psql", "-c", "drop database"]},
    },
}


@pytest.fixture()
def cur():
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


@pytest.fixture()
def docker(monkeypatch):
    """sh・source・ship_config・deploy・verify を差し替え、呼ばれたコマンドを残す"""
    calls = []

    def sh(args, cwd=None, env=None, timeout=1800):
        calls.append({"args": args, "env": env or {}})
        if "psql" in args and "select 1" in args:
            return json.dumps([{"label": "本部", "value": "mock.headquarters.x"}]) + "\n"
        return ""
    monkeypatch.setattr(ship, "sh", sh)
    monkeypatch.setattr(ship, "source", lambda *a, **k: ("/src", "abc123"))
    monkeypatch.setattr(ship, "ship_config", lambda path: CFG)
    monkeypatch.setattr(ship, "deploy", lambda cfg, path, repo, env, rel: calls.append({"deploy": env, "env": ship.compose_env(cfg, repo, env, rel)}) or 1.0)
    monkeypatch.setattr(ship, "verify", lambda *a, **k: None)
    return calls


def release(cur, version):
    rid = f"{REPO}@{version}"
    cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values ('release.created',%s,%s,%s)",
                (f"release:{rid}", Jsonb({"release_id": rid, "repo": REPO, "app_version": version, "prs": []}), f"release.created:{rid}"))
    return rid


def run_pending(cur):
    for ev in ship.pending(cur):
        ship.handle(cur, ev)


def test_bind_only_for_touch_environments(monkeypatch):
    rel = {"release_id": f"{REPO}@1.0.0", "app_version": "1.0.0"}
    monkeypatch.setattr(ship, "TOUCH_BIND", "0.0.0.0")
    assert ship.compose_env(CFG, REPO, "staging", rel)["WEB_BIND_HOST"] == "0.0.0.0"
    assert "WEB_BIND_HOST" not in ship.compose_env(CFG, REPO, "prod", rel)       # 本番の画面は外に出さない
    monkeypatch.setattr(ship, "TOUCH_BIND", "0.0.0.0; rm -rf /")
    assert "WEB_BIND_HOST" not in ship.compose_env(CFG, REPO, "staging", rel)    # 形の合わない値は渡さない
    monkeypatch.setattr(ship, "TOUCH_BIND", "")
    assert "WEB_BIND_HOST" not in ship.compose_env(CFG, REPO, "staging", rel)    # 未設定なら製品の既定（127.0.0.1）


def test_ship_records_touch_points(cur, docker, monkeypatch):
    monkeypatch.setattr(ship, "TOUCH_HOST", "factory-host.lan")
    monkeypatch.setattr(ship, "TOUCH_BIND", "0.0.0.0")
    rid = release(cur, "1.0.0")
    ship.request_ship(cur, rid, "t", "test")
    run_pending(cur)
    cur.execute("select name from events where correlation_id=%s order by id", (f"release:{rid}",))
    names = [r["name"] for r in cur.fetchall()]
    assert names[-4:] == ["release.built", "release.deployed", "release.seeded", "release.touchable"]
    t = touch.latest(cur, REPO)
    assert t["reachable_from_phone"] is True
    assert [e["url"] for e in t["entries"]] == ["http://factory-host.lan:13100/", "http://factory-host.lan:13000/"]   # ポートのない入口は出さない
    assert t["accounts"] == [{"label": "本部", "value": "mock.headquarters.x"}]
    text = touch.render(t)
    assert "http://factory-host.lan:13100/" in text and "mock.headquarters.x" in text and "※" not in text
    assert touch.qr_svg(t["entries"][0]["url"]).startswith("<svg")


def test_touch_points_without_host_say_local_only(cur, docker, monkeypatch):
    monkeypatch.setattr(ship, "TOUCH_HOST", "")
    monkeypatch.setattr(ship, "TOUCH_BIND", "")
    rid = release(cur, "1.0.0")
    ship.request_ship(cur, rid, "t", "test")
    run_pending(cur)
    t = touch.latest(cur, REPO)
    assert t["reachable_from_phone"] is False and t["entries"][0]["url"] == "http://127.0.0.1:13100/"
    assert "この機械の中からしか開けない" in touch.render(t)


def test_accounts_failure_does_not_fail_the_ship(cur, docker, monkeypatch):
    def sh(args, cwd=None, env=None, timeout=1800):
        if "select 1" in args:
            raise RuntimeError("psql が失敗")
        return ""
    monkeypatch.setattr(ship, "sh", sh)
    rid = release(cur, "1.0.0")
    ship.request_ship(cur, rid, "t", "test")
    run_pending(cur)
    cur.execute("select count(*) as n from events where name='release.ship_failed'")
    assert cur.fetchone()["n"] == 0
    t = touch.latest(cur, REPO)
    assert t["accounts"] == [] and "読めなかった" in t["note"]


def test_latest_hides_entries_of_older_versions(cur, docker):
    a, b = release(cur, "1.0.0"), release(cur, "1.1.0")
    ship.request_ship(cur, a, "t", "test")
    run_pending(cur)
    ship.record(cur, "release.deployed", b, {"repo": REPO, "env": "staging", "ok": True, "request_id": 0})   # 入口を残さない版
    assert touch.latest(cur, REPO) is None


def test_reseed(cur, docker):
    assert ship.request_reseed(cur, REPO, "staging", "t", "test")[0] is False   # まだ版がない
    assert ship.request_reseed(cur, REPO, "prod", "t", "test")[0] is False
    rid = release(cur, "1.0.0")
    ship.request_ship(cur, rid, "t", "test")
    run_pending(cur)
    docker.clear()
    ok, msg = ship.request_reseed(cur, REPO, "staging", "t", "test")
    assert ok, msg
    assert ship.busy(cur, REPO) is True
    run_pending(cur)
    cmds = [c["args"] for c in docker if "args" in c]
    assert cmds[0][-3:] == ["psql", "-c", "drop database"]                    # 空にする
    assert "--force-recreate" in cmds[1] and cmds[1][-1] == "backend"         # アプリを作り直す
    assert cmds[2][-3:] == ["seed", "--seed", "42"]                           # シードから入れ直す
    cur.execute("select name from events where correlation_id=%s order by id desc limit 3", (f"release:{rid}",))
    assert [r["name"] for r in cur.fetchall()] == ["release.touchable", "release.reseeded", "release.seeded"]
    assert ship.pending(cur) == []


def test_reseed_refuses_when_staging_moved(cur, docker):
    a, b = release(cur, "1.0.0"), release(cur, "1.1.0")
    ship.request_ship(cur, a, "t", "test")
    run_pending(cur)
    ship.request_reseed(cur, REPO, "staging", "t", "test")
    ship.record(cur, "release.deployed", b, {"repo": REPO, "env": "staging", "ok": True, "request_id": 0})   # 依頼のあとで版が替わった
    run_pending(cur)
    cur.execute("select payload from events where name='release.ship_failed'")
    assert "替わった" in cur.fetchone()["payload"]["error"]


def merged(cur, n):
    url = f"https://github.com/{REPO}/pull/{n}"
    cur.execute("insert into events(name, payload, dedupe_key) values ('pr.merged', %s, %s)",
                (Jsonb({"pr_url": url, "merged_at": f"2026-10-30T00:00:{n:02d}Z"}), f"pr.merged:{url}"))


def test_choose_cuts_a_release_or_keeps_staging(cur, docker):
    assert touch.choose(cur, REPO, None)[0] is None                           # 何もない
    merged(cur, 1)
    rid, needs, _ = touch.choose(cur, REPO, None)
    assert rid == f"{REPO}@0.1.0" and needs is True                           # 未 Release の PR から切る
    ship.request_ship(cur, rid, "t", "test")
    run_pending(cur)
    assert touch.choose(cur, REPO, None)[:2] == (rid, False)                  # 切るものがなければいまの版のまま
    assert touch.choose(cur, REPO, f"{REPO}@9.9.9")[0] is None


def test_up_ships_then_reseeds_and_shows(cur, docker, monkeypatch):
    """up は依頼を書き、shipper の結果を待つ。ここでは待つ代わりに shipper の 1 周を回す"""
    real_wait = touch.wait

    def fake_wait(c, request_id, timeout, log=print, **k):
        run_pending(c)
        return real_wait(c, request_id, 1, poll=0, log=log)
    monkeypatch.setattr(touch, "wait", fake_wait)
    merged(cur, 1)
    out = []
    assert touch.up(cur, REPO, fresh=True, log=out.append) == 0
    text = "\n".join(out)
    assert "o/app@0.1.0 を切った" in text and "release.reseeded" in text and "http://127.0.0.1:13100/" in text


def test_wait_reports_failure(cur):
    rid = release(cur, "1.0.0")
    ship.request_ship(cur, rid, "t", "test")
    req = ship.pending(cur)[0]["id"]
    ship.record(cur, "release.ship_failed", rid, {"repo": REPO, "request_id": req, "error": "build が落ちた"})
    ok, msg = touch.wait(cur, req, 1, poll=0, log=lambda m: None)
    assert ok is False and "build が落ちた" in msg
