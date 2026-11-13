"""メジャー不具合の即時修正と再リリース（ADR 0053、major_defect.py）。GitHub は差し替え、DB は TEST_DATABASE_URL の Postgres（なければ飛ばす）。

2026-11-12 rally #1423: S1 の Defect が factory:fix-proposed のまま人の 1 タップを待ち、差し止めの間も修正が流れなかった。
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
    import major_defect
    import ship
    import train

REPO = "o/app"
ISSUE = f"https://github.com/{REPO}/issues/7"


@pytest.fixture()
def cur():
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


class FakeGh:
    def __init__(self, state="open"):
        self.state, self.calls = state, []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET" and path.startswith(f"repos/{REPO}/issues/"):
            return {"state": self.state}
        if method == "GET":
            return {}
        return None


def merged(cur, n):
    url = f"https://github.com/{REPO}/pull/{n}"
    cur.execute("insert into events(name, payload, dedupe_key) values ('pr.merged', %s, %s)",
                (Jsonb({"pr_url": url, "merged_at": f"2026-11-13T00:00:{n:02d}Z"}), f"pr.merged:{url}"))


def finish_ship(cur):
    for ev in ship.pending(cur):
        ship.record(cur, "release.deployed", ev["payload"]["release_id"],
                    {"repo": ship.release_of(cur, ev["payload"]["release_id"])["repo"], "env": "staging", "ok": True, "request_id": ev["id"]})


def s1(cur, gh, *, drill=False):
    if drill:
        cur.execute("insert into events(name, payload) values ('incident.drill_started', %s)", (Jsonb({"repo": REPO, "drill": "d1"}),))
    comments = []
    inc = incident.from_qa(cur, gh, lambda r, n, b: comments.append(b), REPO, key=f"{ISSUE}#s1", severity="s1", title="完了を記録できない",
                           issue_url=ISSUE, env="staging", release_id=None, qa_corr=f"github:{REPO}#5", job_id=None,
                           policy={"error_budget": {}})
    return inc, comments


def test_s1_defect_is_planned_without_waiting_for_a_human(cur):
    gh = FakeGh()
    inc, comments = s1(cur, gh)
    assert inc and incident.held(cur)
    posted = [c[2]["labels"] for c in gh.calls if c[0] == "POST"]
    assert [incident.HOLD_PRIORITY_LABEL] in posted and [major_defect.PLANNED] in posted
    assert any(c[0] == "DELETE" and c[1].endswith("factory%3Afix-proposed") for c in gh.calls)
    assert "ADR 0053" in comments[0]


def test_drill_is_not_planned(cur):
    gh = FakeGh()
    s1(cur, gh, drill=True)
    assert not [c for c in gh.calls if c[0] == "POST" and c[2]["labels"] == [major_defect.PLANNED]]


def test_rerelease_once_after_the_fix_issue_closes(cur):
    gh = FakeGh()
    s1(cur, gh)
    merged(cur, 1)
    assert major_defect.rerelease_step(cur, gh) == []                  # Issue が開いている間は出さない
    gh.state = "closed"
    tids = major_defect.rerelease_step(cur, gh)
    assert len(tids) == 1
    t = train.train_of(cur, tids[0])
    assert t["kind"] == "emergency" and t["by"] == "supervisor" and ISSUE in t["reason"]
    assert t["approval"]["required"] is False                          # development の製品は本番の承認がない
    finish_ship(cur)
    merged(cur, 2)
    assert major_defect.rerelease_step(cur, gh) == []                  # 同じ Incident で 2 回は出さない


def test_rerelease_waits_while_shipping_is_busy(cur):
    gh = FakeGh("closed")
    s1(cur, gh)
    merged(cur, 1)
    rid, _ = ship.create_release(cur, REPO, ship.next_version(cur, REPO))
    ship.request_ship(cur, rid, "t", "test")                           # 別の出荷が処理中
    assert major_defect.rerelease_step(cur, gh) == []
    finish_ship(cur)
    merged(cur, 2)
    assert len(major_defect.rerelease_step(cur, gh)) == 1              # 次の周回で出す


def test_launch_stage_is_approved_after_staging_without_the_qa_report(cur):
    ship.observe_stage(cur, REPO, "launch")
    gh = FakeGh("closed")
    s1(cur, gh)
    merged(cur, 1)
    [tid] = major_defect.rerelease_step(cur, gh)
    assert train.train_of(cur, tid)["approval"]["required"] is True
    assert major_defect.approve_step(cur) == []                        # staging に出る前は承認しない
    finish_ship(cur)
    assert major_defect.approve_step(cur) == [tid]
    cur.execute("select payload from events where name='release.approved'")
    p = cur.fetchone()["payload"]
    assert p["by"] == "supervisor" and p.get("qa_skipped")
    assert major_defect.approve_step(cur) == []                        # 2 回目はない
