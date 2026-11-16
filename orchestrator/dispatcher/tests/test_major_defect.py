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


def s1(cur, gh, *, drill=False, severity="s1", issue=ISSUE):
    if drill:
        cur.execute("insert into events(name, payload) values ('incident.drill_started', %s)", (Jsonb({"repo": REPO, "drill": "d1"}),))
    comments = []
    inc = incident.from_qa(cur, gh, lambda r, n, b: comments.append(b), REPO, key=f"{issue}#{severity}", severity=severity, title="完了を記録できない",
                           issue_url=issue, env="staging", release_id=None, qa_corr=f"github:{REPO}#5", job_id=None,
                           policy={"error_budget": {}})
    return inc, comments


def test_s1_defect_is_planned_without_waiting_for_a_human(cur):
    gh = FakeGh()
    inc, comments = s1(cur, gh)
    assert inc and incident.held(cur)
    posted = [c[2]["labels"] for c in gh.calls if c[0] == "POST"]
    assert [incident.HOLD_PRIORITY_LABEL] in posted and [major_defect.PLAN, incident.HOLD_PRIORITY_LABEL] in posted
    assert any(c[0] == "DELETE" and c[1].endswith("factory%3Afix-proposed") for c in gh.calls)
    assert "ADR 0053" in comments[0]


def test_drill_is_not_planned(cur):
    gh = FakeGh()
    s1(cur, gh, drill=True)
    assert not [c for c in gh.calls if c[0] == "POST" and major_defect.PLAN in c[2]["labels"]]


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


def test_rerelease_overtakes_a_release_waiting_for_prod(cur):
    """本番を立てる段階で、staging の版が本番待ちでも S1 の修正の緊急便は出る（ADR 0053。本番待ちで修正が止まらない）"""
    ship.observe_stage(cur, REPO, "launch")
    gh = FakeGh("closed")
    merged(cur, 9)
    rid, _ = ship.create_release(cur, REPO, ship.next_version(cur, REPO))
    ship.request_ship(cur, rid, "t", "test")
    finish_ship(cur)                                                    # staging に出て本番待ち
    assert ship.busy(cur, REPO) is True
    s1(cur, gh)
    merged(cur, 1)
    logs = []
    assert len(major_defect.rerelease_step(cur, gh, log=lambda *a: logs.append(a))) == 1, logs


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


def test_s2_only_train_goes_to_staging_but_waits_for_prod_approval(cur):
    """S2 だけの緊急便は staging まで出すが、本番は QA の関門の報告と承認者を待つ（ADR 0053 の追記 2026-11-14）"""
    ship.observe_stage(cur, REPO, "launch")
    gh = FakeGh("closed")
    s1(cur, gh, severity="s2")
    merged(cur, 1)
    [tid] = major_defect.rerelease_step(cur, gh)
    finish_ship(cur)
    assert major_defect.approve_step(cur) == []
    cur.execute("select 1 from events where name='release.approved'")
    assert cur.fetchone() is None


def test_s2_is_major_but_does_not_hold_the_line(cur):
    gh = FakeGh()
    inc, comments = s1(cur, gh, severity="s2")
    assert inc and not incident.held(cur)
    assert [major_defect.PLAN, incident.HOLD_PRIORITY_LABEL] in [c[2]["labels"] for c in gh.calls if c[0] == "POST"]
    assert "ADR 0053" in comments[0]


def test_s3_is_not_major(cur):
    gh = FakeGh()
    s1(cur, gh, severity="s3")
    assert not [c for c in gh.calls if c[0] == "POST"]


def test_defects_fixed_together_ride_one_train(cur):
    gh = FakeGh("closed")
    s1(cur, gh)
    s1(cur, gh, severity="s2", issue=f"https://github.com/{REPO}/issues/8")
    merged(cur, 1)
    tids = major_defect.rerelease_step(cur, gh)
    assert len(tids) == 1
    cur.execute("select count(*) as n from events where name=%s and payload->>'train_id'=%s", (major_defect.REREL, tids[0]))
    assert cur.fetchone()["n"] == 2
    finish_ship(cur)
    assert major_defect.rerelease_step(cur, gh) == []


def test_planner_sees_other_open_defects(cur):
    import planner
    gh = FakeGh()
    s1(cur, gh, severity="s2")
    s1(cur, gh, severity="s2", issue=f"https://github.com/{REPO}/issues/8")
    rows = planner.open_defects(cur, REPO, f"github:{REPO}#8")
    assert rows == [{"issue": 7, "severity": "s2", "title": "完了を記録できない"}]


# ---------------------------------------------------------------- 直しだけの便（ADR 0064）
# 2026-11-16 rally 0.53.0: S1 の緊急便が main をまるごと載せ、QA の関門を通っていない別の直し（#1768）が新しい S1（#1798）を本番に持ち込んだ

def merged_for(cur, n, issue, commit):
    url = f"https://github.com/{REPO}/pull/{n}"
    cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values ('pr.merged', %s, %s, %s)",
                (f"github:{REPO}#{issue}", Jsonb({"pr_url": url, "merged_at": f"2026-11-16T00:00:{n:02d}Z", "merge_commit": commit}),
                 f"pr.merged:{url}"))


def in_prod(cur, commit="base000"):
    """本番を立てる段階で、1 つの版を staging と本番に出した状態にする"""
    ship.observe_stage(cur, REPO, "launch")
    merged(cur, 40)
    rid, _ = ship.create_release(cur, REPO, ship.next_version(cur, REPO))
    ship.record(cur, "release.built", rid, {"repo": REPO, "commit": commit})
    for env in ("staging", "prod"):
        ship.record(cur, "release.deployed", rid, {"repo": REPO, "env": env, "ok": True})
    cur.execute("insert into events(name, payload) values ('release.ship_requested', %s) returning id", (Jsonb({"release_id": rid}),))
    ship.record(cur, "release.deployed", rid, {"repo": REPO, "env": "staging", "ok": True, "request_id": cur.fetchone()["id"]})
    return rid


def test_s1_with_prod_ships_only_the_fix_on_the_prod_version(cur):
    base = in_prod(cur)
    merged(cur, 41)                                       # QA の関門を通っていない別の変更
    gh = FakeGh("closed")
    s1(cur, gh)
    merged_for(cur, 42, 7, "fix0001")
    [tid] = major_defect.rerelease_step(cur, gh)
    t = train.train_of(cur, tid)
    assert t["mode"] == "hotfix" and t["base_release"] == base
    rel = ship.release_of(cur, t["releases"][0]["release_id"])
    assert rel["app_version"] == "0.1.1" and rel["mode"] == "hotfix" and rel["base_commit"] == "base000"
    assert rel["prs"] == [f"https://github.com/{REPO}/pull/42"] and rel["picks"] == ["fix0001"]
    assert ship.unreleased(cur, REPO) == 1                # #41 は次の定期便で関門を通って出る
    finish_ship(cur)
    assert major_defect.approve_step(cur) == [tid]        # 直しだけの便は今までどおり本番まで工場が承認する
    assert ship.next_version(cur, REPO) == "0.2.0"        # 直しの版が後に作られても main の次の版は大きさで決まる


def test_hotfix_conflict_switches_to_full_main_without_auto_approval(cur):
    import inbox
    in_prod(cur)
    merged(cur, 41)
    gh = FakeGh("closed")
    s1(cur, gh)
    merged_for(cur, 42, 7, "fix0001")
    [hid] = major_defect.rerelease_step(cur, gh)
    hrid = train.train_of(cur, hid)["releases"][0]["release_id"]
    [ev] = [e for e in ship.pending(cur) if e["payload"]["release_id"] == hrid]
    ship.record(cur, "release.ship_failed", hrid, {"repo": REPO, "request_id": ev["id"], "hotfix_conflict": True, "error": "conflict"})
    [fid] = major_defect.fallback_step(cur)
    f = train.train_of(cur, fid)
    assert f["mode"] == "fallback" and f["auto_approve"] is False and f["fallback_of"] == hid
    assert any(r["train_id"] == hid and r["dismissed"] for r in train.recent(cur))
    assert major_defect.fallback_step(cur) == []          # 1 回だけ
    finish_ship(cur)
    assert major_defect.approve_step(cur) == []           # 本番は関門と承認者を待つ
    cur.execute("select 1 from events where name='release.approved'")
    assert cur.fetchone() is None
    assert any(i.get("task_kind") == major_defect.FALLBACK_KIND for i in inbox.items(cur))


def test_no_fix_pr_found_ships_full_main_without_auto_approval(cur):
    in_prod(cur)
    gh = FakeGh("closed")
    s1(cur, gh)
    merged(cur, 41)                                       # Issue に結びつかない PR だけ
    [tid] = major_defect.rerelease_step(cur, gh)
    t = train.train_of(cur, tid)
    assert t["mode"] == "fallback" and t["auto_approve"] is False
    finish_ship(cur)
    assert major_defect.approve_step(cur) == []


def test_s2_with_prod_keeps_the_full_emergency_train(cur):
    in_prod(cur)
    gh = FakeGh("closed")
    s1(cur, gh, severity="s2")
    merged_for(cur, 42, 7, "fix0001")
    [tid] = major_defect.rerelease_step(cur, gh)
    assert train.train_of(cur, tid).get("mode") is None
