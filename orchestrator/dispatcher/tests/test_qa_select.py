"""出荷の QA の関門で流すストーリーを選ぶ（ADR 0060。dispatcher/qa_select.py）

  TEST_DATABASE_URL=postgresql://factory@localhost:5432/factory_test python -m pytest orchestrator/dispatcher/tests/test_qa_select.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
URL = os.environ.get("TEST_DATABASE_URL")
REPO = "o/app"
NOW = datetime(2026, 11, 14, 9, 0, tzinfo=timezone.utc)

import qa_select  # noqa: E402

if URL:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb

DOCS = [{"id": "chat", "touches": {"apps": ["student-app", "admin-web"], "contexts": ["messaging"]}},
        {"id": "booking", "touches": {"apps": ["student-app"], "contexts": ["scheduling", "live-lesson"]}},
        {"id": "trace", "touches": {"apps": ["admin-web"], "contexts": ["scheduling"]}}]


def test_by_files_maps_paths_to_stories():
    f = qa_select.by_files
    assert f(["docs/adr/0001.md", "CHANGELOG.md", "changelog.d/1.md", "apps/backend/README.md"], DOCS) == set()   # 利用者の流れに触らない
    assert f(["apps/backend/crates/domain/src/messaging/thread.rs"], DOCS) == {"chat"}                         # コンテキストの名前
    assert f(["apps/backend/crates/application/src/live_lesson/join.rs"], DOCS) == {"booking"}                 # - と _ は同じ
    assert f(["apps/student-app/lib/features/scheduling.dart"], DOCS) == {"booking", "trace"}                  # ファイル名でも当たる
    assert f(["apps/admin-web/src/components/Button.tsx"], DOCS) == {"chat", "trace"}                           # コンテキスト不明はアプリで
    assert f(["specs/stories/trace.yaml"], DOCS) == {"trace"}
    assert f(["apps/backend/crates/infra/src/db.rs"], DOCS) == qa_select.ALL                                    # 共通の土台は全部
    assert f(["deploy/compose.yaml", "apps/backend/crates/domain/src/messaging/x.rs"], DOCS) == qa_select.ALL   # 1 つでも当たらなければ全部


def test_ordered_puts_changed_before_stale():
    sel = {"a": {"run": True, "why": "stale"}, "b": {"run": False, "why": "fresh"}, "c": {"run": True, "why": "changed"},
           "d": {"run": True, "why": "never"}}
    assert qa_select.ordered(["a", "b", "c", "d", "e"], sel) == ["c", "d", "e", "a"]   # 選び方にないものは流す


class GH:
    """PR ごとの変えたファイル"""
    def __init__(self, files):
        self.files, self.calls = files, []

    def __call__(self, method, path, body=None):
        self.calls.append(path)
        n = int(path.split("/pulls/")[1].split("/")[0])
        if n not in self.files:
            raise RuntimeError("404")
        return [{"filename": f} for f in self.files[n]] if "page=1" in path else []


@pytest.fixture()
def cur():
    if not URL:
        pytest.skip("TEST_DATABASE_URL がない")
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


def verified(cur, story, ago, decision="pass"):
    cur.execute("insert into jobs(correlation_id, repo, issue_number, kind, state, started_at, finished_at) values ('c',%s,7,'qa','succeeded',%s,%s) returning id",
                (REPO, NOW - ago, NOW - ago))
    cur.execute("insert into artifacts(type, correlation_id, job_id, produced_by, payload) values ('QAVerdict','c',%s,'verify',%s)",
                (cur.fetchone()["id"], Jsonb({"story_id": story, "decision": decision})))


def release(cur, version, prs, ago):
    cur.execute("insert into events(name, correlation_id, payload, occurred_at) values ('release.created',%s,%s,%s)",
                (f"release:{REPO}@{version}", Jsonb({"release_id": f"{REPO}@{version}", "repo": REPO, "prs": [f"https://github.com/{REPO}/pull/{n}" for n in prs]}),
                 NOW - ago))


def plan(cur, issue, pr, stories):
    corr = f"github:{REPO}#{issue}"
    cur.execute("insert into jobs(correlation_id, repo, issue_number, kind, state, result) values (%s,%s,%s,'implement','succeeded',%s)",
                (corr, REPO, issue, Jsonb({"pr_url": f"https://github.com/{REPO}/pull/{pr}"})))
    cur.execute("insert into artifacts(type, correlation_id, payload) values ('Plan',%s,%s)",
                (corr, Jsonb({"decision": "ready", "stories": [{"id": s, "action": "keep"} for s in stories]})))


def test_select_reasons(cur):
    """never・changed（宣言）・fresh・stale と、blocked は確かめたことにしないこと"""
    verified(cur, "chat", timedelta(hours=3))
    verified(cur, "booking", timedelta(hours=3))
    verified(cur, "trace", timedelta(hours=30))
    release(cur, "0.1.0", [10], timedelta(hours=5))    # 確かめる前の版は数えない
    release(cur, "0.2.0", [11], timedelta(hours=1))
    plan(cur, 1, 10, ["booking"])
    plan(cur, 2, 11, ["chat"])
    docs = DOCS + [{"id": "new", "touches": {"apps": ["student-app"]}}]
    sel = qa_select.select(cur, GH({}), REPO, docs, 24, NOW)
    assert sel["chat"]["why"] == "changed" and sel["chat"]["prs"] == [11] and sel["chat"]["run"]
    assert sel["booking"] == {"run": False, "why": "fresh", "last": (NOW - timedelta(hours=3)).isoformat()}
    assert sel["trace"]["why"] == "stale" and sel["trace"]["run"]
    assert sel["new"] == {"run": True, "why": "never"}
    verified(cur, "new", timedelta(minutes=5), decision="blocked")
    assert qa_select.select(cur, GH({}), REPO, docs, 24, NOW)["new"]["why"] == "never"


def test_select_falls_back_to_files_then_all(cur):
    for s in ("chat", "booking", "trace"):
        verified(cur, s, timedelta(hours=2))
    release(cur, "0.2.0", [20], timedelta(hours=1))
    gh = GH({20: ["apps/backend/crates/domain/src/messaging/thread.rs", "docs/x.md"]})
    sel = qa_select.select(cur, gh, REPO, DOCS, 24, NOW)
    assert [s for s in sel if sel[s]["run"]] == ["chat"]                 # 宣言がなければファイルで
    assert len(gh.calls) == 1                                             # PR ごとに 1 回だけ読む
    release(cur, "0.3.0", [21], timedelta(minutes=30))                    # 読めない PR は全部
    sel = qa_select.select(cur, gh, REPO, DOCS, 24, NOW)
    assert all(sel[s]["why"] == "changed" and 21 in sel[s]["prs"] for s in sel)


def test_carry_over_after_superseded_gate(cur):
    """打ち切られた関門で確かめた分はやり直さず、確かめていない分だけ持ち越す"""
    for s in ("chat", "booking", "trace"):
        verified(cur, s, timedelta(hours=6))
    release(cur, "0.2.0", [30], timedelta(hours=2))     # 全部に効く変更
    verified(cur, "chat", timedelta(hours=1))           # 0.2.0 の関門は chat だけ確かめて打ち切り
    release(cur, "0.3.0", [31], timedelta(minutes=10))  # 文書だけの変更
    gh = GH({30: ["deploy/compose.yaml"], 31: ["docs/readme.md"]})
    sel = qa_select.select(cur, gh, REPO, DOCS, 24, NOW)
    assert sel["chat"]["why"] == "fresh"
    assert sel["booking"]["why"] == "changed" and sel["booking"]["prs"] == [30] and sel["trace"]["why"] == "changed"
