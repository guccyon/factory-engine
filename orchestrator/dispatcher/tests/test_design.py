"""週 1 回の設計の診断（ADR 0022）: supervisor が週に 1 回積むこと、dispatcher が所見を記録して固定 Issue に書くこと。

空の Postgres に postgres/init/*.sql を当て、TEST_DATABASE_URL を向けて動かす（なければ飛ばす）:
  TEST_DATABASE_URL=postgresql://factory@localhost:5432/factory_test python -m pytest orchestrator/dispatcher/tests
"""
import base64
import json
import os
import re
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL がない")
DEFS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "definitions", "design_review.yaml")

if URL:
    os.environ.setdefault("DATABASE_URL", URL)
    os.environ.setdefault("GH_TOKEN", "x")
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb

    import design
    import supervisor

REPO = "o/r"
SAT = datetime(2026, 10, 31, 3, 0, tzinfo=design.JST)   # 土曜 3 時（JST）。2026-W44
FRI = datetime(2026, 10, 30, 23, 0, tzinfo=design.JST)


@pytest.fixture()
def cur():
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


class FakeGH:
    def __init__(self, issues=None):
        self.calls, self.issues = [], issues or []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET" and "/issues?" in path:
            return self.issues
        if method == "POST" and path.endswith("/issues"):
            self.issues = [{"number": 400}]
            return {"number": 400}
        return {}


@pytest.fixture()
def sup(monkeypatch):
    gh = FakeGH()
    monkeypatch.setattr(supervisor, "gh", gh)
    monkeypatch.setattr(supervisor, "QUALITY_REPOS", [REPO])
    monkeypatch.setattr(supervisor, "DESIGN_REVIEW_DEFS", DEFS)
    monkeypatch.setattr(supervisor, "DESIGN_REVIEW", True)
    monkeypatch.setattr(supervisor, "DRY_RUN", False)
    return gh


def jobs(cur):
    cur.execute("select j.id, j.issue_number, j.kind, j.model_tier, e.payload from jobs j join events e on e.job_id=j.id and e.name='job.enqueued' order by j.id")
    return cur.fetchall()


def test_due_after_weekday_and_hour():
    s = {"weekday": "saturday", "hour": 2}
    assert not supervisor.design_review_due(FRI, s)
    assert not supervisor.design_review_due(SAT.replace(hour=1), s)
    assert supervisor.design_review_due(SAT, s)
    assert supervisor.design_review_due(datetime(2026, 11, 1, 0, 0, tzinfo=design.JST), s)   # 逃しても日曜まで拾う


def test_enqueues_once_per_week_on_fixed_issue(cur, sup):
    supervisor.design_review_step(cur, FRI)
    assert jobs(cur) == []
    supervisor.design_review_step(cur, SAT)
    supervisor.design_review_step(cur, SAT.replace(hour=5))
    rows = jobs(cur)
    assert [(r["issue_number"], r["kind"], r["model_tier"]) for r in rows] == [(400, "design_review", "tier-judge")]
    assert rows[0]["payload"]["input"] == {"previous": []}
    assert rows[0]["payload"]["week"] == "2026-W44"
    created = [c for c in sup.calls if c[0] == "POST"]
    assert len(created) == 1 and created[0][2]["labels"] == [design.LABEL]
    cur.execute("update jobs set state='succeeded'")
    supervisor.design_review_step(cur, datetime(2026, 11, 7, 3, 0, tzinfo=design.JST))   # 次の週はまた積む。Issue は作り直さない
    assert len(jobs(cur)) == 2 and len([c for c in sup.calls if c[0] == "POST"]) == 1


def test_passes_previous_findings(cur, sup):
    cur.execute("insert into events(name, payload) values ('design.reviewed', %s)",
                (Jsonb({"repo": REPO, "findings": [{"title": "依存の輪", "severity": "mid", "body": "長い本文"}]}),))
    supervisor.design_review_step(cur, SAT)
    assert jobs(cur)[0]["payload"]["input"] == {"previous": [{"title": "依存の輪", "severity": "mid"}]}


RESULT = {"kind": "design_review", "ok": True, "repo": REPO, "sha": "0123456789abcdef", "model": "m", "cost_usd": 1.5,
          "report": {"summary": "概ね良い。", "strengths": ["層が守られている"], "checked_viewpoints": ["glossary", "errors"],
                     "findings": [{"title": "失敗の型が 1 つ", "viewpoint": "errors", "severity": "mid", "body": "DomainError に集中",
                                   "evidence": ["src/domain/error.rs"], "fix": "コンテキストごとに分ける"},
                                  {"title": "古いコメント", "viewpoint": "drift", "severity": "low", "body": "暦月 2 回", "evidence": [], "fix": ""}]}}


def job(cur):
    cur.execute("insert into jobs(correlation_id, repo, issue_number, kind) values (%s,%s,400,'design_review') returning id", (f"github:{REPO}#400", REPO))
    return cur.fetchone()["id"]


def test_record_writes_issue_once_per_week(cur):
    jid = job(cur)
    cur.execute("insert into events(name, payload) values ('quality.snapshot', %s)",
                (Jsonb({"repo": REPO, "day": "2026-10-30", "score": 88, "counts": {"functions": 1285, "complex_functions": 27}}),))
    gh, comments = FakeGH(), []
    ok = design.record(cur, REPO, 400, f"github:{REPO}#400", jid, RESULT, gh, lambda r, i, b: comments.append((r, i, b)))
    assert ok
    assert not design.record(cur, REPO, 400, f"github:{REPO}#400", jid, RESULT, gh, lambda *a: comments.append(a))   # 同じ週の 2 回目は書かない
    cur.execute("select payload from events where name='design.reviewed'")
    ev = cur.fetchall()
    assert len(ev) == 1 and ev[0]["payload"]["checked"] == ["glossary", "errors"]
    cur.execute("select type, produced_by from artifacts")
    assert cur.fetchall() == [{"type": "DesignReview", "produced_by": "govern"}]
    patches = [c for c in gh.calls if c[0] == "PATCH"]
    assert len(patches) == 1 and patches[0][1] == f"repos/{REPO}/issues/400"
    body = patches[0][2]["body"]
    assert "### 1. [中] 失敗の型が 1 つ" in body and "スコア 88" in body and "`src/domain/error.rs`" in body
    snap = json.loads(base64.b64decode(re.search(r"<!-- factory-design:v1 ([A-Za-z0-9+/=]+) -->", body).group(1)))
    assert snap["review"]["summary"] == "概ね良い。" and snap["quality"]["score"] == 88 and snap["quality_trend"] == [{"day": "2026-10-30", "score": 88.0}]
    assert len(comments) == 1 and "所見 2 件（重 0・中 1・小 1）" in comments[0][2]


def test_page_renders_snapshot(cur):
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "health", "design"))
    import render_html
    gh = FakeGH()
    design.record(cur, REPO, 400, "c", job(cur), RESULT, gh, lambda *a: None)
    snap = render_html.parse([c for c in gh.calls if c[0] == "PATCH"][0][2]["body"])
    html = render_html.render(snap)
    assert "/*__SNAPSHOT__*/" not in html and "失敗の型が 1 つ" in html and '"errors": "失敗の表し方"' in html
