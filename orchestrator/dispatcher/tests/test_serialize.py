"""衝突を避ける並べ方（ADR 0027 の 2）: よく衝突するファイル・見込みのファイル・投入の順番"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL がない")

if URL:
    os.environ.setdefault("DATABASE_URL", URL)
    os.environ.setdefault("GH_TOKEN", "x")
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb

    import process_learn
    import serialize
    import supervisor

R = "https://github.com/o/r/pull/"
LIB = "apps/backend/crates/server/src/lib.rs"


@pytest.fixture()
def cur():
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


def conflict_job(cur, ticket, files, cost):
    cur.execute("insert into jobs(correlation_id, repo, issue_number, state) values (%s,'o/r',%s,'succeeded') returning id",
                (f"github:o/r#{ticket}", ticket))
    jid = cur.fetchone()["id"]
    corr = f"github:o/r#{ticket}"
    cur.execute("insert into events(name, correlation_id, job_id, payload) values ('job.finished',%s,%s,%s)", (corr, jid, Jsonb({"cost_usd": cost})))
    cur.execute("insert into events(name, correlation_id, job_id, payload) values ('continue.enqueued',%s,%s,%s)",
                (corr, jid, Jsonb({"reason": "conflict", "pr_url": R + str(ticket)})))
    process_learn.record_conflict_files(cur, jid, corr, files)


def test_hot_files_need_count_and_agent_cost(cur):
    for t in range(3):
        conflict_job(cur, t + 1, [LIB], 2.0)
        conflict_job(cur, t + 10, ["CHANGELOG.md"], 9.0)        # 機械的に解く・断片にしたファイルは並べない
        conflict_job(cur, t + 20, ["specs/a.yaml"], 0.0)        # 機械的に解けた（費用なし）
    conflict_job(cur, 30, ["x.ts"], 50.0)                       # 1 回だけ
    assert serialize.hot_files(cur) == {LIB: {"conflicts": 3, "cost": 6.0}}


def test_paths_and_touches():
    body = "変更: `apps/backend/crates/server/src/lib.rs` に登録し、openapi.yaml と README.md を直す。v1.2 や e.g. は拾わない"
    assert serialize.paths_in(body) == {LIB, "openapi.yaml", "README.md"}
    assert serialize.touches({"lib.rs"}, LIB)
    assert serialize.touches({"src/lib.rs"}, LIB)
    assert not serialize.touches({"apps/backend/crates/interface/src/lib.rs"}, LIB)


def test_expected_files_merges_plan_and_body(cur):
    cur.execute("insert into artifacts(type, correlation_id, payload) values ('Plan','github:o/r#5',%s)",
                (Jsonb({"expected_files": ["specs/contracts/api/admin-web/openapi.yaml"]}),))
    assert serialize.expected_files(cur, "github:o/r#5", "see apps/x/y.ts") == {"specs/contracts/api/admin-web/openapi.yaml", "apps/x/y.ts"}


def test_blocker_and_record(cur):
    hot = {LIB: {"conflicts": 4, "cost": 8.0}}
    inflight = {"github:o/r#1": {LIB, "a.rs"}, "github:o/r#2": {"b.rs"}}
    assert serialize.blocker({"lib.rs"}, hot, inflight, "github:o/r#9") == (LIB, "github:o/r#1")
    assert serialize.blocker({"lib.rs"}, hot, inflight, "github:o/r#1") is None      # 自分の PR は数えない
    assert serialize.blocker({"b.rs"}, hot, inflight, "github:o/r#9") is None        # よく衝突するファイルでない
    assert serialize.record(cur, "github:o/r#9", LIB, "github:o/r#1", hot[LIB]) is True
    assert serialize.record(cur, "github:o/r#9", LIB, "github:o/r#1", hot[LIB]) is False
    assert "4 回衝突" in serialize.serialized(cur, "github:o/r#9")[0]["why"]


def test_enqueue_step_holds_second_task_on_same_hot_file(cur, monkeypatch):
    for t in range(3):
        conflict_job(cur, t + 1, [LIB], 2.0)
    issues = [{"number": 41, "title": "A", "body": f"{LIB} にルートを足す", "labels": [], "created_at": "1"},
              {"number": 42, "title": "B", "body": f"{LIB} にルートを足す", "labels": [], "created_at": "2"},
              {"number": 43, "title": "C", "body": "apps/admin-web/src/x.ts だけ", "labels": [], "created_at": "3"}]
    posted = []

    def fake_gh(method, path, body=None):
        if "issues?state=open" in path:
            return issues
        if method == "POST" and path.endswith("/labels"):
            posted.append(int(path.split("/")[-2]))
        return {}

    monkeypatch.setattr(supervisor, "gh", fake_gh)
    monkeypatch.setattr(supervisor, "REPOS", ["o/r"])
    monkeypatch.setattr(supervisor, "DRY_RUN", False)
    monkeypatch.setattr(supervisor, "MAX_INFLIGHT", 13)
    monkeypatch.setattr(supervisor, "over_budget", lambda cur: None)
    monkeypatch.setattr(supervisor, "factory_prs", lambda cur: [])
    supervisor.enqueue_step(cur, set())
    assert posted == [41, 43]            # 42 は 41 と同じよく衝突するファイルに触るので、41 が終わるまで待つ
    s = serialize.serialized(cur, "github:o/r#42")
    assert s and s[0]["behind"] == "github:o/r#41" and s[0]["file"] == LIB
