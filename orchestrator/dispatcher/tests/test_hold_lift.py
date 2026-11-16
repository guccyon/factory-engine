"""S1 の差し止めを、直しが出たのを QA 役が確かめ直して解く（ADR 0062、hold_lift.py）。GitHub は差し替え、DB は TEST_DATABASE_URL の Postgres（なければ飛ばす）。

2026-11-16 rally #1785・#1798: 直しが本番に出たあとも、人が解除するまでライン全体が止まっていた。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL がない")

if URL:
    import hold_lift
    import incident
    import major_defect
    import psycopg
    import ship
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb

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
    def __init__(self, state="closed"):
        self.state, self.calls = state, []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET" and "labels=" in path:
            return [{"number": 5}]
        if method == "GET" and path.startswith(f"repos/{REPO}/issues/"):
            return {"state": self.state, "title": "回数が減る", "body": "## 再現\n1. 体験予約をキャンセル"}
        if method == "GET":
            return {}
        if method == "POST" and path.endswith("/issues"):
            return {"number": 5}
        return None

    def comments(self, n):
        return [c[2]["body"] for c in self.calls if c[0] == "POST" and c[1] == f"repos/{REPO}/issues/{n}/comments"]


def merged(cur, n):
    url = f"https://github.com/{REPO}/pull/{n}"
    cur.execute("insert into events(name, payload, dedupe_key) values ('pr.merged', %s, %s)",
                (Jsonb({"pr_url": url, "merged_at": f"2026-11-16T00:00:{n:02d}Z"}), f"pr.merged:{url}"))


def finish_ship(cur, env="staging"):
    for ev in ship.pending(cur):
        ship.record(cur, "release.deployed", ev["payload"]["release_id"],
                    {"repo": ship.release_of(cur, ev["payload"]["release_id"])["repo"], "env": env, "ok": True, "request_id": ev["id"]})


def held_s1(cur, gh, *, drill=False, issue=ISSUE):
    if drill:
        cur.execute("insert into events(name, payload) values ('incident.drill_started', %s)", (Jsonb({"repo": REPO, "drill": "d1"}),))
    return incident.from_qa(cur, gh, lambda r, n, b: None, REPO, key=f"{issue}#s1", severity="s1", title="回数が減る",
                            issue_url=issue, env="staging", release_id=None, qa_corr=f"github:{REPO}#5", job_id=None,
                            policy={"error_budget": {}})


def fixed_and_shipped(cur, gh):
    """S1 で差し止め → 直しがマージ → 緊急便 → staging（development の製品は staging が出し先）"""
    held_s1(cur, gh)
    merged(cur, 1)
    [tid] = major_defect.rerelease_step(cur, gh)
    finish_ship(cur)
    return tid


def recheck_job(cur):
    cur.execute("select id, kind, priority from jobs where kind='qa' order by id desc limit 1")
    return cur.fetchone()


def finish_job(cur, jid, fixed="yes", confidence=0.9, state="succeeded"):
    res = {"kind": "qa", "ok": True, "mode": "recheck",
           "recheck": {"checks": [{"issue": 7, "fixed": fixed, "confidence": confidence, "saw": "残り 1 回のまま", "tried": [{"did": "a", "saw": "b"}]}]}}
    cur.execute("update jobs set state=%s, result=%s where id=%s", (state, Jsonb(res), jid))


def test_qa_lifts_the_hold_after_rechecking_the_fix(cur):
    gh = FakeGh()
    held_s1(cur, gh)
    assert hold_lift.step(cur, gh)["lifted"] is False                  # 直しがまだ出ていない
    assert recheck_job(cur) is None
    merged(cur, 1)
    major_defect.rerelease_step(cur, gh)
    finish_ship(cur)
    got = hold_lift.step(cur, gh)
    j = recheck_job(cur)
    assert got["lifted"] is False and j and j["priority"] == 100        # p0 で積む（差し止めの間も流れる）
    cur.execute("select payload->'input' as i from events where name='job.enqueued' and job_id=%s", (j["id"],))
    inp = cur.fetchone()["i"]
    assert inp["mode"] == "recheck" and inp["defects"][0]["issue"] == 7 and "再現" in inp["defects"][0]["body"]
    assert hold_lift.step(cur, gh)["lifted"] is False                  # 動いている間は待つ（積み直さない）
    assert recheck_job(cur)["id"] == j["id"]
    finish_job(cur, j["id"])
    got = hold_lift.step(cur, gh)
    assert got["lifted"] is True and not incident.held(cur)
    cur.execute("select payload from events where name=%s order by id desc limit 1", (incident.RELEASED,))
    p = cur.fetchone()["payload"]
    assert p["by"] == "qa" and p["via"] == "qa_verified" and p["checks"][0]["job_id"] == j["id"]
    assert incident.open_incidents(cur) == []
    assert any("差し止めを解きました" in b for b in gh.comments(7))


def test_low_confidence_is_rechecked_once_then_left_to_a_human(cur):
    gh = FakeGh()
    fixed_and_shipped(cur, gh)
    hold_lift.step(cur, gh)
    finish_job(cur, recheck_job(cur)["id"], confidence=0.5)            # 0.7 未満の「直っていた」は確かめられなかった扱い
    hold_lift.step(cur, gh)
    j2 = recheck_job(cur)
    finish_job(cur, j2["id"], fixed="unconfirmed", confidence=0.2)
    got = hold_lift.step(cur, gh)
    assert got["lifted"] is False and got["states"][incident.incident_id(REPO, f"{ISSUE}#s1")]["state"] == "unconfirmed"
    assert incident.held(cur)
    cur.execute("select count(*) as n from jobs where kind='qa'")
    assert cur.fetchone()["n"] == 2                                     # 3 本目は積まない
    hold_lift.step(cur, gh)
    cur.execute("select count(*) as n from jobs where kind='qa'")
    assert cur.fetchone()["n"] == 2


def test_not_fixed_keeps_the_hold_and_says_so(cur):
    gh = FakeGh()
    fixed_and_shipped(cur, gh)
    hold_lift.step(cur, gh)
    finish_job(cur, recheck_job(cur)["id"], fixed="no")
    got = hold_lift.step(cur, gh)
    assert got["lifted"] is False and incident.held(cur)
    assert any("直っていませんでした" in b for b in gh.comments(7))


def test_all_incidents_must_be_fixed(cur):
    gh = FakeGh()
    held_s1(cur, gh)
    held_s1(cur, gh, issue=f"https://github.com/{REPO}/issues/8")      # 2 件目の S1。直しはまだ（緊急便に載っていない）
    cur.execute("select payload from events where name='incident.opened' and payload->>'issue' like '%%/8'")
    second = cur.fetchone()["payload"]
    gh.state = "open"
    merged(cur, 1)
    assert major_defect.rerelease_step(cur, gh) == []
    gh.state = "closed"
    [tid] = major_defect.rerelease_step(cur, gh)
    finish_ship(cur)
    hold_lift.step(cur, gh)
    cur.execute("select id from jobs where kind='qa' order by id")
    jobs = [r["id"] for r in cur.fetchall()]
    assert len(jobs) >= 1
    for jid in jobs:
        finish_job(cur, jid)                                           # 答えは #7 だけ。#8 の答えがない
    assert hold_lift.step(cur, gh)["lifted"] is False
    assert any(i["incident_id"] == second["incident_id"] for i in incident.open_incidents(cur))


def test_prod_stage_waits_for_prod(cur):
    """本番を立てる製品は、直しの版が本番に出てから確かめ直す"""
    ship.observe_stage(cur, REPO, "launch")
    gh = FakeGh()
    fixed_and_shipped(cur, gh)
    hold_lift.step(cur, gh)
    assert recheck_job(cur) is None                                    # staging だけではまだ
    major_defect.approve_step(cur)
    finish_ship(cur, env="prod")
    hold_lift.step(cur, gh)
    assert recheck_job(cur) is not None


def test_drill_is_left_to_a_human(cur):
    gh = FakeGh()
    held_s1(cur, gh, drill=True)
    assert hold_lift.step(cur, gh) is None


def test_qa_cannot_release_by_name(cur):
    gh = FakeGh()
    held_s1(cur, gh)
    with pytest.raises(ValueError):
        incident.release(cur, "qa", "直った")
    assert incident.qa_lift(cur, ["other"], "x", []) is False          # 差し止めの Incident と違えば解かない
    assert incident.held(cur)


def test_switch_off(cur, monkeypatch):
    gh = FakeGh()
    fixed_and_shipped(cur, gh)
    monkeypatch.setattr(hold_lift, "ENABLED", False)
    assert hold_lift.step(cur, gh) is None
    assert recheck_job(cur) is None
