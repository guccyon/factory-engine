"""PR ごとの使い捨て環境（ADR 0043 の O7 ephemeral_env）。依頼・枠・壊す時期・shipper の手順。docker・git・GitHub は呼ばずに argv を見る。

  TEST_DATABASE_URL=postgresql://factory@localhost:5432/factory_test python -m pytest orchestrator/dispatcher/tests/test_ephemeral.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL がない")

if URL:
    import psycopg
    from psycopg.rows import dict_row

    import ephemeral
    import ship

REPO = "o/app"
CFG = {
    "compose": "deploy/compose.yaml",
    "images": {"backend": {"context": "apps/backend", "env": "BACKEND_IMAGE"}},
    "environments": {"staging": {"env": {"BACKEND_PORT": 18080, "WEB_PORT": 13000, "POSTGRES_IMAGE": "postgres:17-alpine"}}},
    "seed": {"environments": ["staging"], "service": "backend", "command": ["seed"]},
    "touch": {"environments": ["staging"], "bind_env": "WEB_BIND_HOST", "entries": [{"label": "画面", "port_env": "WEB_PORT", "qr": True}]},
    "verify": {"service": "backend", "url": "http://127.0.0.1:8080/version"},
    "ephemeral": {"base": "staging", "port_step": 1000},
}


@pytest.fixture()
def cur():
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


@pytest.fixture()
def docker(monkeypatch, tmp_path):
    calls = []

    def sh(args, cwd=None, env=None, timeout=1800):
        calls.append((list(args), dict(env or {})))
        if "curl" in args:   # 確かめる部品は、プロジェクト名（app-pr-<番号>）の版を答える
            return '{"app_version": "%s"}' % args[3].split("-", 1)[1]
        return ""
    monkeypatch.setattr(ship, "sh", sh)
    monkeypatch.setattr(ship, "WORK", str(tmp_path))
    monkeypatch.setattr(ship, "source", lambda repo, version, commit=None: (str(tmp_path), commit))
    monkeypatch.setattr(ship, "ship_config", lambda path: CFG)
    monkeypatch.setattr(ephemeral, "open_pr", lambda repo, pr: "abc123")
    return calls


def run_pending(cur):
    for ev in ephemeral.pending(cur):
        ephemeral.handle(cur, ev)


def test_up_touch_down_leaves_the_circuit_in_events(cur, docker):
    ok, _ = ephemeral.request_up(cur, REPO, 7, "builder", "test")
    assert ok
    assert ephemeral.request_up(cur, REPO, 7, "builder", "test")[0] is False      # 重ねて依頼しない
    run_pending(cur)
    [a] = ephemeral.active(cur)
    assert (a["slot"], a["project"], a["head_sha"]) == (1, "app-pr-7", "abc123")
    assert a["entries"][0]["url"].endswith(":14000/")                              # staging の 13000 に 1 × 1000
    up = [c for c in docker if c[0][:2] == ["docker", "compose"] and "up" in c[0]][0]
    assert up[1]["COMPOSE_PROJECT_NAME"] == "app-pr-7" and up[1]["BACKEND_PORT"] == "19080" and up[1]["BACKEND_IMAGE"] == "app-backend:pr-7"
    assert any(c[0][:4] == ["docker", "compose", "-p", "app-pr-7"] and c[0][-1] == "seed" for c in docker)   # 合成シード
    assert ephemeral.touch(cur, REPO, 7, "owner", note="iPad で開いた")[0] is True
    assert ephemeral.request_down(cur, REPO, 7, "supervisor", "test", "pr_closed")[0] is True
    run_pending(cur)
    assert ephemeral.active(cur) == []
    assert any(c[0] == ["docker", "compose", "-p", "app-pr-7", "down", "-v", "--remove-orphans"] for c in docker)
    cur.execute("select payload from events where name='env.ephemeral_down'")
    d = cur.fetchone()["payload"]
    assert d["reason"] == "pr_closed" and d["touched"] == [{"by": "owner", "kind": "human"}]


def test_at_most_max_at_once_and_slots_are_reused(cur, docker):
    assert ephemeral.request_up(cur, REPO, 1, "b", "t")[0] and ephemeral.request_up(cur, REPO, 2, "b", "t")[0]
    ok, msg = ephemeral.request_up(cur, REPO, 3, "b", "t")
    assert ok is False and "2 つまで" in msg                                         # 依頼中も数える
    run_pending(cur)
    assert sorted(a["slot"] for a in ephemeral.active(cur)) == [1, 2]
    ephemeral.request_down(cur, REPO, 1, "b", "t")
    run_pending(cur)
    assert ephemeral.request_up(cur, REPO, 3, "b", "t")[0]
    run_pending(cur)
    assert {int(a["pr"]): a["slot"] for a in ephemeral.active(cur)} == {2: 2, 3: 1}


def test_failed_up_is_recorded_and_cleaned(cur, docker, monkeypatch):
    def fork(repo, pr):
        raise RuntimeError("fork から出た PR")
    monkeypatch.setattr(ephemeral, "open_pr", fork)
    ephemeral.request_up(cur, REPO, 9, "b", "t")
    run_pending(cur)
    cur.execute("select payload from events where name='env.ephemeral_failed'")
    assert "fork" in cur.fetchone()["payload"]["error"]
    assert ephemeral.active(cur) == [] and ephemeral.pending(cur) == []
    assert any(c[0][:4] == ["docker", "compose", "-p", "app-pr-9"] and "down" in c[0] for c in docker)
    assert not any("build" in c[0] for c in docker)                                # fork のコードは作らない


def test_due_down_on_pr_closed_or_ttl(cur, docker):
    ephemeral.request_up(cur, REPO, 4, "b", "t")
    ephemeral.request_up(cur, REPO, 5, "b", "t")
    run_pending(cur)
    now = datetime.now(timezone.utc)
    assert ephemeral.due_down(cur, lambda r, n: False, now) == []
    assert ephemeral.due_down(cur, lambda r, n: n == 4, now) == [(REPO, 4, "pr_closed")]
    assert ephemeral.due_down(cur, lambda r, n: False, now + timedelta(hours=ephemeral.TTL_HOURS + 1)) == [(REPO, 4, "ttl"), (REPO, 5, "ttl")]
    ephemeral.request_down(cur, REPO, 4, "b", "t", "pr_closed")
    assert ephemeral.due_down(cur, lambda r, n: True, now) == [(REPO, 5, "pr_closed")]   # 依頼済みは重ねない


def test_product_without_ephemeral_is_refused(cur, docker, monkeypatch):
    monkeypatch.setattr(ship, "ship_config", lambda path: {k: v for k, v in CFG.items() if k != "ephemeral"})
    ephemeral.request_up(cur, REPO, 6, "b", "t")
    run_pending(cur)
    cur.execute("select payload from events where name='env.ephemeral_failed'")
    assert "ephemeral がない" in cur.fetchone()["payload"]["error"]
