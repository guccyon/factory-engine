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
        "restart": ["backend"],
        "clock_env": "CLOCK_OFFSET_SECONDS",
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
    assert cmds[2][-3:] == ["up", "-d", "--wait"]                             # 残りの部品をいまの変数にそろえる（#305）
    assert cmds[3][-3:] == ["seed", "--seed", "42"]                           # シードから入れ直す
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


def test_restart_requests_are_bounded(cur, docker):
    """時間をまたぐシナリオ（ADR 0028 の scenario_time）: 本番・版のない環境・進めすぎは受け付けない"""
    assert ship.request_restart(cur, REPO, "staging", "t", "test")[0] is False            # まだ版がない
    assert ship.request_restart(cur, REPO, "prod", "t", "test")[0] is False
    rid = release(cur, "1.0.0")
    ship.request_ship(cur, rid, "t", "test")
    run_pending(cur)
    assert ship.request_restart(cur, REPO, "staging", "t", "test", advance_seconds=-1)[0] is False
    assert ship.request_restart(cur, REPO, "staging", "t", "test", advance_seconds=ship.ADVANCE_MAX_SECONDS + 1)[0] is False
    assert ship.request_restart(cur, REPO, "staging", "t", "test", advance_seconds=3600, reset_clock=True)[0] is False
    assert ship.request_restart(cur, REPO, "staging", "t", "test", advance_seconds=86400)[0] is True


def test_restart_advances_the_clock_and_keeps_the_db(cur, docker):
    rid = release(cur, "1.0.0")
    ship.request_ship(cur, rid, "t", "test")
    run_pending(cur)
    assert ship.clock_offset(cur, REPO, "staging") == 0
    docker.clear()
    ship.request_restart(cur, REPO, "staging", "qa:7", "runner_relay", advance_seconds=86400)
    run_pending(cur)
    cmds = [c for c in docker if "args" in c]
    up = cmds[0]
    assert "--force-recreate" in up["args"] and "--no-deps" in up["args"] and up["args"][-1] == "backend"
    assert up["env"]["CLOCK_OFFSET_SECONDS"] == "86400"
    assert cmds[1]["args"][-3:] == ["up", "-d", "--wait"] and cmds[1]["env"]["CLOCK_OFFSET_SECONDS"] == "86400"   # 残りもそろえる（時計は同じ値）
    assert not any("drop database" in c["args"] for c in cmds)                            # DB は残す
    assert ship.clock_offset(cur, REPO, "staging") == 86400
    cur.execute("select name, payload from events where correlation_id=%s order by id desc limit 2", (f"release:{rid}",))
    rows = cur.fetchall()
    assert [r["name"] for r in rows] == ["release.touchable", "release.restarted"]
    assert rows[1]["payload"]["services"] == ["backend"] and rows[1]["payload"]["by"] == "qa:7"
    ship.request_restart(cur, REPO, "staging", "t", "test", advance_seconds=3600)         # 進めた分に足す
    run_pending(cur)
    assert ship.clock_offset(cur, REPO, "staging") == 90000
    ship.request_restart(cur, REPO, "staging", "t", "test", reset_clock=True)
    run_pending(cur)
    assert ship.clock_offset(cur, REPO, "staging") == 0
    ship.request_restart(cur, REPO, "staging", "t", "test", advance_seconds=86400)
    run_pending(cur)
    ship.request_reseed(cur, REPO, "staging", "t", "test")                                 # 入れ直すと時計は 0 に戻る
    run_pending(cur)
    assert ship.clock_offset(cur, REPO, "staging") == 0
    assert ship.pending(cur) == []


def test_restart_without_clock_env_cannot_advance(cur, docker, monkeypatch):
    cfg = {**CFG, "touch": {k: v for k, v in CFG["touch"].items() if k not in ("clock_env", "restart")}}
    monkeypatch.setattr(ship, "ship_config", lambda path: cfg)
    rid = release(cur, "1.0.0")
    ship.request_ship(cur, rid, "t", "test")
    run_pending(cur)
    ship.request_restart(cur, REPO, "staging", "t", "test", advance_seconds=3600)
    run_pending(cur)
    cur.execute("select payload from events where name='release.ship_failed'")
    assert "clock_env" in cur.fetchone()["payload"]["error"]
    docker.clear()
    ship.request_restart(cur, REPO, "staging", "t", "test")                               # 再起動だけならできる（確かめる部品）
    run_pending(cur)
    up = [c for c in docker if "args" in c][0]
    assert up["args"][-1] == "backend" and "CLOCK_OFFSET_SECONDS" not in up["env"]


def test_touch_restart_waits_and_reports_offset(cur, docker, monkeypatch):
    real_wait = touch.wait

    def fake_wait(c, request_id, timeout, log=print, **k):
        run_pending(c)
        return real_wait(c, request_id, 1, poll=0, log=log)
    monkeypatch.setattr(touch, "wait", fake_wait)
    rid = release(cur, "1.0.0")
    ship.request_ship(cur, rid, "t", "test")
    run_pending(cur)
    ok, msg, offset = touch.restart(cur, REPO, advance_hours=24, log=lambda m: None)
    assert ok and offset == 86400 and "24 時間進んでいる" in msg
    assert touch.restart(cur, REPO, "prod", log=lambda m: None)[0] is False


def _shipper_waits(monkeypatch):
    """up の待ちの代わりに shipper の 1 周を回す"""
    real_wait = touch.wait

    def fake_wait(c, request_id, timeout, log=print, **k):
        run_pending(c)
        return real_wait(c, request_id, 1, poll=0, log=log)
    monkeypatch.setattr(touch, "wait", fake_wait)


def test_up_same_version_aligns_bind_and_entries(cur, docker, monkeypatch):
    """#305: 待ち受けと入口の名前を変えたあと、同じ版の env up で画面の部品を作り直し、入口を新しい名前で書き直す"""
    _shipper_waits(monkeypatch)
    monkeypatch.setattr(ship, "TOUCH_HOST", "")
    monkeypatch.setattr(ship, "TOUCH_BIND", "")
    rid = release(cur, "1.0.0")
    ship.request_ship(cur, rid, "t", "test")
    run_pending(cur)
    ship.request_restart(cur, REPO, "staging", "t", "test", advance_seconds=3600)
    run_pending(cur)
    assert touch.latest(cur, REPO)["reachable_from_phone"] is False
    monkeypatch.setattr(ship, "TOUCH_HOST", "factory-host.lan")                          # shipper の設定を変えた
    monkeypatch.setattr(ship, "TOUCH_BIND", "0.0.0.0")
    docker.clear()
    out = []
    assert touch.up(cur, REPO, release_id=rid, log=out.append) == 0
    cmds = [c for c in docker if "args" in c]
    assert not any("drop database" in c["args"] or "seed" in c["args"] for c in cmds)    # DB は残す
    aligned = [c for c in cmds if c["args"][-3:] == ["up", "-d", "--wait"]]
    assert aligned and aligned[0]["env"]["WEB_BIND_HOST"] == "0.0.0.0"                   # 画面の部品は新しい待ち受けで作り直る
    t = touch.latest(cur, REPO)
    assert t["reachable_from_phone"] is True and t["bind"] == "0.0.0.0"
    assert t["entries"][0]["url"] == "http://factory-host.lan:13100/"
    assert ship.clock_offset(cur, REPO, "staging") == 3600                               # 進めた時計も残す
    assert "http://factory-host.lan:13100/" in "\n".join(out)
    assert touch.up(cur, REPO, log=out.append) == 0                                      # --release なしでも同じ
    assert ship.pending(cur) == []


def test_fresh_aligns_bind_too(cur, docker, monkeypatch):
    """#305: --fresh（入れ直し）でも画面の部品を新しい待ち受けにそろえる"""
    _shipper_waits(monkeypatch)
    monkeypatch.setattr(ship, "TOUCH_BIND", "")
    rid = release(cur, "1.0.0")
    ship.request_ship(cur, rid, "t", "test")
    run_pending(cur)
    monkeypatch.setattr(ship, "TOUCH_BIND", "0.0.0.0")
    docker.clear()
    assert touch.up(cur, REPO, release_id=rid, fresh=True, log=lambda m: None) == 0
    cmds = [c for c in docker if "args" in c]
    assert any(c["args"][-3:] == ["up", "-d", "--wait"] and c["env"].get("WEB_BIND_HOST") == "0.0.0.0" for c in cmds)
    cur.execute("select count(*) as n from events where name='release.restart_requested'")
    assert cur.fetchone()["n"] == 0                                                      # 入れ直すときは再起動を別に積まない


def test_up_same_version_without_touch_does_not_restart(cur, docker, monkeypatch):
    """ship.yaml に touch がない製品は、同じ版の env up で再起動を積まない（shipper が断って停止に開くのを避ける）"""
    cfg = {k: v for k, v in CFG.items() if k != "touch"}
    monkeypatch.setattr(ship, "ship_config", lambda path: cfg)
    rid = release(cur, "1.0.0")
    ship.request_ship(cur, rid, "t", "test")
    run_pending(cur)
    assert touch.up(cur, REPO, release_id=rid, log=lambda m: None) == 0
    cur.execute("select count(*) as n from events where name='release.restart_requested'")
    assert cur.fetchone()["n"] == 0


# ------------------------------------------------------------------ 時刻を指定して合わせる（ADR 0055）
def test_restart_sets_the_clock_to_an_offset(cur, docker):
    """set_to: 進める量を足すのではなく、その値にする（前より小さくもできる）。足す・戻すとは一緒に使わない"""
    rid = release(cur, "1.0.0")
    ship.request_ship(cur, rid, "t", "test")
    run_pending(cur)
    ship.request_restart(cur, REPO, "staging", "t", "test", advance_seconds=86400)
    run_pending(cur)
    assert ship.request_restart(cur, REPO, "staging", "t", "test", offset_seconds=600)[0] is True
    run_pending(cur)
    assert ship.clock_offset(cur, REPO, "staging") == 600
    assert ship.request_restart(cur, REPO, "staging", "t", "test", offset_seconds=-1)[0] is False
    assert ship.request_restart(cur, REPO, "staging", "t", "test", offset_seconds=ship.OFFSET_MAX_SECONDS + 1)[0] is False
    assert ship.request_restart(cur, REPO, "staging", "t", "test", offset_seconds=60, reset_clock=True)[0] is False
    assert ship.request_restart(cur, REPO, "staging", "t", "test", offset_seconds=60, advance_seconds=60)[0] is False


def _running_qa(cur, job_id):
    cur.execute("insert into jobs(id, correlation_id, repo, issue_number, kind, state) values (%s,%s,%s,%s,'qa','running')",
                (job_id, f"github:{REPO}#{job_id}", REPO, job_id))


def test_clock_refuses_while_other_qa_jobs_run(cur, docker):
    """決定 3: 同じ製品でほかの QA のジョブが動いていれば、時計を動かさない。0 に戻すのは通す。回数の上限も守る"""
    import scenario_clock
    rid = release(cur, "1.0.0")
    ship.request_ship(cur, rid, "t", "test")
    run_pending(cur)
    _running_qa(cur, 7)
    assert scenario_clock.refuse(cur, REPO, 7, {"offset_seconds": 60}, 8) is None
    _running_qa(cur, 8)
    why = scenario_clock.refuse(cur, REPO, 7, {"offset_seconds": 60}, 8)
    assert why and "8" in why and "skipped" in why
    assert scenario_clock.refuse(cur, REPO, 7, {"advance_seconds": 0}, 8)            # 再起動だけでも止まるので断る
    assert scenario_clock.refuse(cur, REPO, 7, {"reset_clock": True, "advance_seconds": 0}, 8) is None
    cur.execute("update jobs set state='succeeded' where id=8")
    ship.request_restart(cur, REPO, "staging", "qa:7", "runner_relay", advance_seconds=60)
    ship.request_restart(cur, REPO, "staging", "qa:7", "runner_relay", advance_seconds=60)
    assert "使い切った" in scenario_clock.refuse(cur, REPO, 7, {"advance_seconds": 60}, 2)


def test_clock_cleanup_resets_only_what_the_job_left(cur, docker):
    """決定 4: 時計を動かしたまま終わったジョブの分だけ、工場が 0 に戻す"""
    import scenario_clock
    rid = release(cur, "1.0.0")
    ship.request_ship(cur, rid, "t", "test")
    run_pending(cur)
    assert scenario_clock.cleanup(cur, REPO, "staging", 7) is None                   # 触っていない
    ship.request_restart(cur, REPO, "staging", "qa:7", "runner_relay", offset_seconds=3600)
    run_pending(cur)
    assert scenario_clock.cleanup(cur, REPO, "staging", 9) is None                   # ほかのジョブの分は戻さない
    assert scenario_clock.cleanup(cur, REPO, "staging", 7)
    run_pending(cur)
    assert ship.clock_offset(cur, REPO, "staging") == 0
    cur.execute("select payload from events where name='release.restarted' order by id desc limit 1")
    assert cur.fetchone()["payload"]["by"] == "qa:7:cleanup"
    assert scenario_clock.cleanup(cur, REPO, "staging", 7) is None                   # もう戻した（最後の依頼が戻し）
    ship.request_restart(cur, REPO, "staging", "qa:7", "runner_relay", reset_clock=True)
    run_pending(cur)
    assert scenario_clock.cleanup(cur, REPO, "staging", 7) is None                   # 自分で戻した


def test_clock_describe_says_server_time(cur, docker):
    import scenario_clock
    from datetime import datetime, timezone
    rid = release(cur, "1.0.0")
    ship.request_ship(cur, rid, "t", "test")
    run_pending(cur)
    ship.request_restart(cur, REPO, "staging", "qa:7", "runner_relay", offset_seconds=3600)
    run_pending(cur)
    text = scenario_clock.describe(cur, REPO, "staging", datetime(2026, 11, 13, 10, 0, tzinfo=timezone.utc))
    assert "2026-11-13T20:00:00+09:00" in text and "3600 秒" in text
