"""工場の入れ替え（ADR 0018）: ラインの一時停止と再開、入れ替えてよいかの判定、番人の drain:、
上げ直した dispatcher が実行中のジョブのコンテナにつなぎ直すこと（docker は偽物に差し替える）。

空の Postgres に postgres/init/*.sql を当て、TEST_DATABASE_URL を向けて動かす（なければ飛ばす）:
  TEST_DATABASE_URL=postgresql://factory@localhost:5432/factory_test python -m pytest orchestrator/dispatcher/tests
"""
import json
import os
import stat
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL がない")

if URL:
    for k, v in {"DATABASE_URL": URL, "GH_TOKEN": "x", "CLAUDE_CODE_OAUTH_TOKEN": "x", "OBJECTSTORE_ENDPOINT": "localhost:1",
                 "OBJECTSTORE_ACCESS_KEY": "x", "OBJECTSTORE_SECRET_KEY": "x", "NOTIFIER": "log", "JOB_DB_ENABLED": "0"}.items():
        os.environ.setdefault(k, v)
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb

    import dispatcher
    import drain
    import ops
    import stoppage

# 偽の docker。$FAKE_DOCKER/containers/<名前> に「ジョブの id 終了コード」、$FAKE_DOCKER/logs/<名前> に出力を置く
FAKE_DOCKER = r"""#!/bin/sh
d="$FAKE_DOCKER"
echo "$*" >> "$d/calls"
case "$1" in
  container) [ -f "$d/containers/$3" ] ;;
  ps) for f in "$d"/containers/*; do [ -f "$f" ] && echo "$(cut -d' ' -f1 "$f") $(basename "$f")"; done; exit 0 ;;
  rm) rm -f "$d/containers/$3"; exit 0 ;;
  wait) [ -f "$d/containers/$2" ] || exit 1; cut -d' ' -f2 "$d/containers/$2" ;;
  logs) cat "$d/logs/$3" 2>/dev/null; exit 0 ;;
  *) exit 0 ;;
esac
"""


@pytest.fixture()
def cur():
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


@pytest.fixture()
def docker(tmp_path, monkeypatch):
    (tmp_path / "containers").mkdir()
    (tmp_path / "logs").mkdir()
    (tmp_path / "bin").mkdir()
    exe = tmp_path / "bin" / "docker"
    exe.write_text(FAKE_DOCKER)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("FAKE_DOCKER", str(tmp_path))
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")

    def put(name, job_id, exit_code=0, output=""):
        (tmp_path / "containers" / name).write_text(f"{job_id} {exit_code}")
        (tmp_path / "logs" / name).write_text(output)
    put.dir = tmp_path
    return put


def job(cur, ref, state="queued", kind="implement", priority=None, container=None):
    repo, n = ref.split("#")
    t = ops.upsert_ticket(cur, "github", ref, repo, title="T" + n, priority=priority)
    cur.execute("insert into jobs(correlation_id, repo, issue_number, kind, state, ticket_id, priority, started_at, container_id) "
                "values (%s,%s,%s,%s,%s,%s,%s, case when %s='running' then now() end, %s) returning id",
                (f"github:{ref}", repo, int(n), kind, state, t["id"], t["priority"], state, container))
    return cur.fetchone()["id"]


def test_drain_and_resume(cur):
    assert drain.state(cur) is None
    assert drain.set_drain(cur, True, "factoryctl", "postgres の入れ替え")
    assert not drain.set_drain(cur, True, "console:builder")   # 二重には記録しない
    st = drain.state(cur)
    assert st["by"] == "factoryctl" and st["reason"] == "postgres の入れ替え"
    assert drain.set_drain(cur, False, "factoryctl")
    assert drain.state(cur) is None
    cur.execute("select name from events where name like 'factory.drain%%' order by id")
    assert [r["name"] for r in cur.fetchall()] == ["factory.drain_requested", "factory.drain_released"]


def test_ready_waits_for_running_jobs_and_shipping(cur):
    assert not drain.status(cur)["ready"]   # 一時停止していなければ入れ替えない
    drain.set_drain(cur, True, "factoryctl")
    jid = job(cur, "o/r#1", state="running")
    job(cur, "o/r#2")   # 順番待ちは待たない
    st = drain.status(cur)
    assert [j["id"] for j in st["running"]] == [jid] and not st["ready"]
    cur.execute("update jobs set state='succeeded', finished_at=now() where id=%s", (jid,))
    assert drain.status(cur)["ready"]
    cur.execute("insert into events(name, payload) values ('release.ship_requested', %s)", (Jsonb({"release_id": "o/r@0.3.0"}),))
    st = drain.status(cur)
    assert st["shipping"] == [{"name": "release.ship_requested", "release_id": "o/r@0.3.0"}] and not st["ready"]


def test_no_preemption_while_draining(cur):
    low = job(cur, "o/r#1", state="running", priority=50)
    job(cur, "o/r#2", priority=100)
    drain.set_drain(cur, True, "factoryctl")
    dispatcher.MAX_PARALLEL = 1
    dispatcher.ops_once()
    cur.execute("select cancel_requested from jobs where id=%s", (low,))
    assert not cur.fetchone()["cancel_requested"]
    drain.set_drain(cur, False, "factoryctl")
    dispatcher.ops_once()
    cur.execute("select cancel_requested, preempted_for from jobs where id=%s", (low,))
    assert cur.fetchone()["cancel_requested"]


def test_watcher_opens_drain_stoppage_and_skips_idle(cur):
    drain.set_drain(cur, True, "factoryctl", "入れ替え")
    cur.execute("update events set occurred_at = now() - interval '45 minutes' where name='factory.drain_requested'")
    cur.execute("insert into jobs(correlation_id, repo, issue_number, state, enqueued_at, finished_at) "
                "values ('github:o/r#9','o/r',9,'succeeded', now() - interval '2 hours', now() - interval '2 hours')")
    dispatcher.watch_once()
    assert stoppage.open_keys(cur, "drain:") and not stoppage.open_keys(cur, "idle:")
    cur.execute("select category from stoppages where key like 'drain:%%'")
    assert cur.fetchone()["category"] == "human"
    drain.set_drain(cur, False, "factoryctl")
    dispatcher.watch_once()
    assert not stoppage.open_keys(cur, "drain:")


def test_recover_reattaches_live_containers_and_requeues_lost(cur, docker):
    live = job(cur, "o/r#1", state="running", container="factory-job-1")
    lost = job(cur, "o/r#2", state="running", container="factory-job-2")
    done = job(cur, "o/r#3", state="succeeded")
    docker("factory-job-1", live)
    docker(f"factory-job-{done}", done)   # 記録の後、片付けの前に落ちた
    resumed = dispatcher.recover()
    assert [j["id"] for j in resumed] == [live]
    cur.execute("select id, state, error from jobs order by id")
    rows = {r["id"]: r for r in cur.fetchall()}
    assert rows[live]["state"] == "running"
    assert rows[lost]["state"] == "failed" and rows[lost]["error"] == "orphaned_by_restart"
    cur.execute("select count(*) as n from jobs where enqueued_by='recover'")
    assert cur.fetchone()["n"] == 1   # 取り残されたほうだけ積み直す
    assert not (docker.dir / "containers" / f"factory-job-{done}").exists()
    assert (docker.dir / "containers" / "factory-job-1").exists()


def test_run_job_reattaching_records_result_without_restarting(cur, docker, monkeypatch):
    class Store:
        def put_object(self, *a, **kw):
            pass
    monkeypatch.setattr(dispatcher, "store", Store())
    jid = job(cur, "o/r#5", state="running", kind="inspect", container="factory-job-x")
    cur.execute("insert into events(name, correlation_id, job_id, payload) values ('job.started','github:o/r#5',%s,%s)",
                (jid, Jsonb({"provenance": {"engine_version": "abc"}})))
    result = {"pr_url": "https://github.com/o/r/pull/5", "report": {"result": {"defects": [], "checked_rubric_ids": ["R1"], "summary": "ok"}}}
    docker("factory-job-x", jid, 0, "前の dispatcher のときの出力\nFACTORY_RESULT:" + json.dumps(result) + "\n")
    cur.execute("select * from jobs where id=%s", (jid,))
    dispatcher.run_job(cur.fetchone(), reattaching=True)
    cur.execute("select state from jobs where id=%s", (jid,))
    assert cur.fetchone()["state"] == "succeeded"
    cur.execute("select name from events where job_id=%s order by id", (jid,))
    names = [r["name"] for r in cur.fetchall()]
    assert names.count("job.started") == 1 and "job.reattached" in names and "gate.evaluated" in names
    calls = (docker.dir / "calls").read_text()
    assert "run " not in calls   # コンテナを起動し直さない
    assert not (docker.dir / "containers" / "factory-job-x").exists()   # 記録の後に片付ける
