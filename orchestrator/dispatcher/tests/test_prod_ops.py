"""本番を立てた製品の運用の最小の形（ADR 0057）: 出す前の DB の控え、本番への出荷の失敗での自動の戻し、本番の見張り、本番待ちに contract を添える。
docker と git は使わない（sh・source・deploy・verify・dump を差し替える）。

test_ship.py と同じく、空の Postgres に postgres/init/*.sql を当てて TEST_DATABASE_URL を向ける（なければ飛ばす）。
"""
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

    import incident
    import inbox
    import prod_watch
    import ship
    import stoppage

REPO = "o/app"
CFG = {
    "compose": "deploy/compose.yaml",
    "images": {"web": {"context": "web", "env": "WEB_IMAGE"}},
    "environments": {"staging": {"env": {}}, "prod": {"approval": True, "env": {}}},
    "verify": {"service": "backend", "url": "http://127.0.0.1:8080/version"},
    "backup": {"environments": ["prod"], "service": "postgres", "command": ["pg_dump", "-U", "app", "-Fc", "app"], "keep": 2},
}


@pytest.fixture()
def cur(monkeypatch):
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    ship.observe_stage(c, REPO, "launch")
    monkeypatch.setattr(stoppage, "notify", lambda *a, **k: None)
    prod_watch._state.clear()
    yield c
    conn.close()


@pytest.fixture()
def env(monkeypatch, tmp_path):
    """docker を使わない出荷。fail_deploy に版を入れるとその版の起動が落ち、down が True の間は本番が答えない"""
    st = {"deploys": [], "fail_deploy": set(), "down": False, "dumps": []}
    monkeypatch.setattr(ship, "WORK", str(tmp_path))
    monkeypatch.setattr(ship, "source", lambda repo, version, commit=None: (f"/src/{version}", "abc"))
    monkeypatch.setattr(ship, "ship_config", lambda path: CFG)

    def deploy(cfg, path, repo, e, rel):
        st["deploys"].append((e, rel["app_version"]))
        if rel["app_version"] in st["fail_deploy"]:
            raise RuntimeError(f"{e} の backend が答えた版が None（期待 {rel['app_version']}）")
        return 1.0
    monkeypatch.setattr(ship, "deploy", deploy)

    def verify(cfg, repo, e, rel):
        if st["down"]:
            raise RuntimeError("curl: (7) Failed to connect")
    monkeypatch.setattr(ship, "verify", verify)

    def dump(args, out_path, timeout=1800):
        st["dumps"].append(args)
        with open(out_path, "wb") as f:
            f.write(b"PGDMP")
        return 5
    monkeypatch.setattr(ship, "dump", dump)
    return st


def release(cur, version):
    rid = f"{REPO}@{version}"
    cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values ('release.created',%s,%s,%s)",
                (f"release:{rid}", Jsonb({"release_id": rid, "repo": REPO, "app_version": version, "prs": []}), f"release.created:{rid}"))
    return rid


def deployed(cur, rid, env):
    ship.record(cur, "release.deployed", rid, {"repo": REPO, "env": env, "ok": True, "request_id": 0})


def approve(cur, rid):
    ship.record(cur, "release.approved", rid, {"by": "owner", "via": "test"})
    for ev in ship.pending(cur):
        ship.handle(cur, ev)


def names(cur, name):
    cur.execute("select payload from events where name=%s order by id", (name,))
    return [r["payload"] for r in cur.fetchall()]


# ------------------------------------------------------------------ 控えと自動の戻し
def test_backup_before_prod_and_keep_only_recent(cur, env):
    a = release(cur, "1.0.0")
    approve(cur, a)                       # 最初の本番は控えを取らない（動いている版がない）
    assert env["dumps"] == [] and names(cur, "release.backed_up") == []
    for v in ("1.1.0", "1.2.0", "1.3.0"):
        approve(cur, release(cur, v))
    b = names(cur, "release.backed_up")
    assert [x["of_release"] for x in b] == [f"{REPO}@1.0.0", f"{REPO}@1.1.0", f"{REPO}@1.2.0"]
    assert b[-1]["for_release"] == f"{REPO}@1.3.0" and b[-1]["bytes"] == 5
    assert env["dumps"][0][:5] == ["docker", "compose", "-p", "app-prod", "exec"] and env["dumps"][0][-5:] == CFG["backup"]["command"]
    assert len(os.listdir(ship.backup_dir(REPO, "prod"))) == 2 and len(b[-1]["pruned"]) == 1   # keep: 2


def test_no_backup_without_config_or_for_staging():
    assert ship.backup_config({**CFG, "backup": None}, "prod") is None
    assert ship.backup_config(CFG, "staging") is None


def test_backup_failure_stops_before_touching_prod(cur, env, monkeypatch):
    approve(cur, release(cur, "1.0.0"))
    monkeypatch.setattr(ship, "dump", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("pg_dump が失敗")))
    approve(cur, release(cur, "1.1.0"))
    assert env["deploys"] == [("prod", "1.0.0")]                    # 控えが取れなければ本番に触れない
    assert "pg_dump" in names(cur, "release.ship_failed")[-1]["error"]
    assert ship.status(cur, REPO)["environments"]["prod"]["release_id"] == f"{REPO}@1.0.0"


def test_failed_prod_deploy_rolls_back_automatically(cur, env):
    a, b = release(cur, "1.0.0"), release(cur, "1.1.0")
    approve(cur, a)
    env["fail_deploy"].add("1.1.0")
    approve(cur, b)
    assert env["deploys"] == [("prod", "1.0.0"), ("prod", "1.1.0"), ("prod", "1.0.0")]
    rb = names(cur, "release.rolled_back")
    assert rb[-1]["release_id"] == a and rb[-1]["auto"] is True and rb[-1]["from"] == b
    failed = names(cur, "release.ship_failed")[-1]
    assert "自動で戻した" in failed["error"]
    assert ship.status(cur, REPO)["environments"]["prod"]["release_id"] == a
    inc = incident.open_incidents(cur, REPO)
    assert inc[0]["severity"] == "s2" and inc[0]["env"] == "prod" and inc[0]["detected_by"] == "shipper"
    assert ship.pending(cur) == []                                   # 承認の依頼は処理済み


def test_failed_first_prod_deploy_has_nothing_to_roll_back(cur, env):
    env["fail_deploy"].add("1.0.0")
    approve(cur, release(cur, "1.0.0"))
    assert names(cur, "release.rolled_back") == [] and "前の版がない" in names(cur, "release.ship_failed")[-1]["error"]


def test_failed_rollback_is_reported(cur, env):
    approve(cur, release(cur, "1.0.0"))
    env["fail_deploy"].update({"1.0.0", "1.1.0"})
    approve(cur, release(cur, "1.1.0"))
    assert names(cur, "release.rolled_back") == [] and "戻しも失敗" in names(cur, "release.ship_failed")[-1]["error"]


# ------------------------------------------------------------------ 本番の見張り
def test_watch_heals_then_reports_down_and_up(cur, env):
    a = release(cur, "1.0.0")
    approve(cur, a)
    assert prod_watch.step(cur, now=0, log=lambda m: None) == []
    env["down"] = True
    for i in range(prod_watch.FAILS - 1):
        assert prod_watch.step(cur, now=i, log=lambda m: None) == []
    assert prod_watch.step(cur, now=10, log=lambda m: None) == [f"{REPO}: self_healed ok=True"]   # まず立て直す
    assert names(cur, "prod.self_healed")[-1]["ok"] is True
    # 立て直したのにまだ答えない（同じ時間のうち）→ 落ちたとする
    out = [prod_watch.step(cur, now=20 + i, log=lambda m: None) for i in range(prod_watch.FAILS)]
    assert out[-1] == [f"{REPO}: down"] and len(names(cur, "prod.self_healed")) == 1
    down = names(cur, "prod.down")[-1]
    inc = incident.open_incidents(cur, REPO)
    assert inc[0]["incident_id"] == down["incident_id"] and inc[0]["severity"] == "s2" and inc[0]["detected_by"] == "prod_watch"
    cur.execute("select 1 from stoppages where key=%s and ended_at is null", (f"prod-down:{REPO}",))
    assert cur.fetchone()
    assert prod_watch.step(cur, now=40, log=lambda m: None) == []    # 止まっている間は数え直さない（Incident を重ねない）
    env["down"] = False
    assert prod_watch.step(cur, now=50, log=lambda m: None) == [f"{REPO}: up"]
    assert incident.open_incidents(cur, REPO) == [] and names(cur, "prod.up")[-1]["down_seconds"] >= 0
    cur.execute("select 1 from stoppages where key=%s and ended_at is null", (f"prod-down:{REPO}",))
    assert cur.fetchone() is None


def test_watch_reports_down_when_heal_fails(cur, env):
    approve(cur, release(cur, "1.0.0"))
    env["down"] = True
    env["fail_deploy"].add("1.0.0")
    out = [prod_watch.step(cur, now=i, log=lambda m: None) for i in range(prod_watch.FAILS)]
    assert out[-1] == [f"{REPO}: self_healed ok=False", f"{REPO}: down"]
    assert "答えた版" in names(cur, "prod.down")[-1]["error"]


def test_watch_skips_development_and_busy(cur, env):
    a = release(cur, "1.0.0")
    approve(cur, a)
    ship.record(cur, "release.approved", release(cur, "1.1.0"), {"by": "owner", "via": "test"})   # 入れ替えの途中
    env["down"] = True
    assert all(prod_watch.step(cur, now=i, log=lambda m: None) == [] for i in range(prod_watch.FAILS + 1))
    cur.execute("truncate events restart identity cascade")
    ship.observe_stage(cur, REPO, "development")
    deployed(cur, release(cur, "1.0.0"), "prod")
    assert prod_watch.watched(cur) == []


def test_tick_runs_at_interval(cur, env, monkeypatch):
    calls = []
    monkeypatch.setattr(prod_watch, "step", lambda c, now, log: calls.append(now) or [])
    monkeypatch.setattr(prod_watch, "_last_tick", [None])
    monkeypatch.setattr(prod_watch, "INTERVAL", 3600.0)
    prod_watch.tick(cur)
    prod_watch.tick(cur)
    assert len(calls) == 1


# ------------------------------------------------------------------ 本番待ちに contract を添える
def test_release_item_mentions_contract(cur):
    a, b, c = release(cur, "1.0.0"), release(cur, "1.1.0"), release(cur, "1.2.0")
    for r, m in ((a, []), (b, ["m/2_contract_drop_x.sql"]), (c, [])):
        ship.record(cur, "release.built", r, {"repo": REPO, "request_id": 0, "commit": "x", "contract_migrations": m})
    deployed(cur, a, "staging")
    deployed(cur, a, "prod")
    deployed(cur, c, "staging")
    item = inbox._release_item(cur, REPO)
    d = [x for x in item["detail"] if x["label"] == "DB の contract"]
    assert d and "m/2_contract_drop_x.sql" in d[0]["text"] and a in d[0]["text"]
    deployed(cur, b, "prod")
    assert not [x for x in inbox._release_item(cur, REPO)["detail"] if x["label"] == "DB の contract"]


def test_events_are_defined():
    import yaml
    from pathlib import Path
    ev = yaml.safe_load((Path(__file__).resolve().parents[3] / "definitions" / "events.yaml").read_text(encoding="utf-8"))["events"]
    for n in ("release.backed_up", "prod.down", "prod.up", "prod.self_healed"):
        assert n in ev
