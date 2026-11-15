"""操業コンソールの委員会（開催と議事録）: 知見の委員会の審査・定例と事故の調査委員会の週次を、events から一覧と議事録に組み直す（読むだけ）

空の Postgres に postgres/init/*.sql を当て、TEST_DATABASE_URL を向けて動かす（なければ飛ばす）
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

URL = os.environ.get("TEST_DATABASE_URL")
PR = "https://github.com/o/r/pull/7"


@pytest.fixture()
def cur():
    if not URL:
        pytest.skip("TEST_DATABASE_URL がない")
    import psycopg
    from psycopg.rows import dict_row
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


def ev(cur, name, payload):
    cur.execute("insert into events(name, payload) values (%s, %s) returning id", (name, json.dumps(payload)))
    return cur.fetchone()["id"]


def test_list_and_minutes(cur):
    import console_committee as cc
    votes = [{"role": "consistency", "vote": "adopt", "reasons": ["既存と矛盾しない"]}, {"role": "dissent", "vote": "enforce", "reasons": ["繰り返す"]}]
    d = ev(cur, "committee.decided", {"pr_url": PR, "outcome": "enforce", "decision": "D-0003", "votes": votes, "added": ["K-0004"], "cost_usd": 0.4})
    ev(cur, "committee.enforce_filed", {"decision": "D-0003", "knowledge": "K-0004", "solution_kind": "deterministic", "issue": "https://github.com/o/r/issues/9"})
    w = ev(cur, "committee.weekly_reviewed", {"repo": "o/r", "week": "2026-W46", "facts": {"items": 3, "stale": [{"id": "K-0001", "confidence": "hypothesis", "review_by": "2026-11-01"}]},
                                              "report": {"summary": "落ち着いている", "promote": [{"ids": ["K-0002"], "why": "3 回効いた", "proposal": "confirmed へ"}]}})
    i = ev(cur, "fincident.reviewed", {"week": "2026-W46", "summary": "トークンの使いすぎが 1 件", "findings": 2, "incidents": 1, "opened": ["FI-20261114-01"],
                                       "outboxed": [], "issues_proposed": [], "rejected": [], "new_viewpoints": [], "check_reviews": []})
    ev(cur, "knowledge.case_filed", {})   # 委員会の開催ではない記録は並ばない
    out = cc.list_meetings(cur)["meetings"]
    assert [m["id"] for m in out] == [i, w, d]
    assert out[2]["title"] == "知見の審査 o/r#7" and out[2]["repo"] == "o/r" and out[2]["outcome"].startswith("採用")
    assert [m["id"] for m in cc.list_meetings(cur, "incident")["meetings"]] == [i]

    doc = cc.detail(cur, d)
    secs = {s["h"]: s for s in doc["sections"]}
    assert secs["票"]["items"][0].startswith("整合: 採る — 既存と矛盾しない")
    assert secs["担保の Issue"]["items"] == ["K-0004（deterministic）: https://github.com/o/r/issues/9"]
    assert doc["link"] == PR
    secs = {s["h"]: s for s in cc.detail(cur, w)["sections"]}
    assert secs["確かさを上げる候補"]["items"] == ["K-0002: 3 回効いた（案: confirmed へ）"]
    assert "廃止の候補" not in secs   # 中身のない節は出さない
    secs = {s["h"]: s for s in cc.detail(cur, i)["sections"]}
    assert "台帳に開いた: FI-20261114-01" in secs["所見"]["items"]


def test_get(cur):
    import console_committee as cc
    assert cc.get(cur, "/api/committees/999")[0] == 404
    assert cc.get(cur, "/api/committees?committee=x")[0] == 200
    assert cc.get(cur, "/api/other") is None
