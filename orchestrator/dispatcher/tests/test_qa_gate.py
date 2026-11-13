"""出荷の QA の関門（ADR 0049）: staging の配備で開き、判定と探索を別の固定 Issue に同時に積み、結果を報告にまとめて承認に添える。

  TEST_DATABASE_URL=postgresql://factory@localhost:5432/factory_test python -m pytest orchestrator/dispatcher/tests/test_qa_gate.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
URL = os.environ.get("TEST_DATABASE_URL")
ROOT = Path(__file__).resolve().parents[3]
REPO = "o/app"
NOW = datetime(2026, 11, 11, 9, 0, tzinfo=timezone.utc)

import qa_line  # noqa: E402

if URL:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb

    import inbox
    import qa_gate
    import ship


def test_defs_copy_matches_definitions():
    full = yaml.safe_load((ROOT / "definitions" / "qa.yaml").read_text(encoding="utf-8"))
    assert full["gate"] == qa_line.DEFS["gate"]
    assert full["act"]["explore_issue_label"] == qa_line.DEFS["act"]["explore_issue_label"]
    names = yaml.safe_load((ROOT / "definitions" / "events.yaml").read_text(encoding="utf-8"))["events"]
    for n in ("qa.gate_opened", "qa.gate_job", "qa.gate_superseded", "qa.gate_decided", "qa.gate_blocked"):
        assert n in names, n
    assert "factory:qa-explore" in (ROOT / "orchestrator" / "scripts" / "labels.sh").read_text(encoding="utf-8")


class FakeGH:
    """固定 Issue は判定 7・探索 8。コメントを覚える"""
    def __init__(self):
        self.calls = []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET" and "factory:qa-explore" in path:
            return [{"number": 8}]
        if method == "GET" and "factory:qa-defect" in path:
            return []
        if method == "GET":
            return [{"number": 7}]
        return {"number": 100, "html_url": f"https://github.com/{REPO}/issues/100"}

    def comments(self, n):
        return [b["body"] for m, p, b in self.calls if m == "POST" and p.endswith(f"/issues/{n}/comments")]


@pytest.fixture()
def cur():
    if not URL:
        pytest.skip("TEST_DATABASE_URL がない")
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


def deploy(cur, rid, version="0.9.0"):
    cur.execute("insert into events(name, correlation_id, payload) values ('release.created',%s,%s)",
                (f"release:{rid}", Jsonb({"release_id": rid, "repo": REPO, "app_version": version, "prs": []})))
    ship.record(cur, "release.deployed", rid, {"repo": REPO, "env": "staging", "ok": True, "request_id": 0})


def finish(cur, job_id, state="succeeded", ago=timedelta(minutes=1)):
    cur.execute("update jobs set state=%s, finished_at=%s where id=%s", (state, NOW - ago, job_id))


def verdicts(cur, job_id, rows):
    for i, (story, decision, defects) in enumerate(rows):
        cur.execute("insert into artifacts(type, correlation_id, job_id, produced_by, payload) values ('QAVerdict','c',%s,'verify',%s)",
                    (job_id, Jsonb({"story_id": story, "decision": decision, "defects": defects, "interpretation": "見た"})))


def explored(cur, job_id, defects, charter="detour", gate="o/app@0.9.0"):
    cur.execute("insert into events(name, correlation_id, job_id, payload) values ('qa.explore','c',%s,%s)",
                (job_id, Jsonb({"repo": REPO, "charter": charter, "tried": 4, "defects": defects, "flags": [], "gate": gate, "unattended": False})))


def jobs_of(cur, rid):
    g = qa_gate.gate(cur, rid)
    return {w: [j["job_id"] for j in g["jobs"][w]] for w in qa_gate.WHICH}


def test_opens_on_staging_deploy_and_enqueues_both_on_separate_issues(cur):
    gh, rid = FakeGH(), "o/app@0.9.0"
    assert qa_gate.step(cur, gh, REPO, now=NOW)["opened"] is None   # 配備がなければ開かない
    deploy(cur, rid)
    out = qa_gate.step(cur, gh, REPO, now=NOW)
    assert out["opened"] == rid
    assert sorted(x["which"] for x in out["enqueued"]) == ["explore", "verdict"]
    cur.execute("select issue_number, kind from jobs order by id")
    assert [r["issue_number"] for r in cur.fetchall()] == [7, 8]   # 判定は factory:qa、探索は factory:qa-explore（並行に動ける）
    cur.execute("select payload->'input' as i from events where name='job.enqueued' order by id")
    v, x = [r["i"] for r in cur.fetchall()]
    assert v["gate"] == rid and not v.get("mode")
    assert x["mode"] == "explore" and x["gate"] == rid and x["unattended"] is False and x["charter"] == "detour"
    again = qa_gate.step(cur, gh, REPO, now=NOW)   # 二度は開かず、積まない
    assert again["opened"] is None and again["enqueued"] == []


def test_waits_for_both_and_reports_blocker_candidates(cur):
    gh, rid = FakeGH(), "o/app@0.9.0"
    deploy(cur, rid)
    qa_gate.step(cur, gh, REPO, now=NOW)
    j = jobs_of(cur, rid)
    finish(cur, j["verdict"][0])
    verdicts(cur, j["verdict"][0], [("coach-replies", "pass", []),
                                    ("subscription", "hold", [{"title": "入口がない", "severity": "s2", "known_gap": False},
                                                              {"title": "ログインが消える", "severity": "s3", "known_gap": True}])])
    assert qa_gate.step(cur, gh, REPO, now=NOW)["decided"] is None   # 探索の結果を待つ
    finish(cur, j["explore"][0])
    explored(cur, j["explore"][0], [{"title": "他人の予約が見える", "severity": "s1", "issue": "https://x/1"},
                                    {"title": "文言が硬い", "severity": "s3"},
                                    {"title": "既知", "severity": "s2", "known_gap": True}])
    d = qa_gate.step(cur, gh, REPO, now=NOW)["decided"]
    assert d["candidates"] == 2
    assert {b["title"] for b in d["blockers"]} == {"入口がない", "他人の予約が見える"}
    text = " ".join(r["text"] for r in d["report"])
    assert "2 件" in text and "known_gap" in text and "detour" in text
    assert qa_gate.gate(cur, rid)["state"] == "decided"
    assert "出荷の QA の関門" in gh.comments(7)[0]   # 判定の固定 Issue にまとめ


def test_retries_unconfirmed_once_then_reports(cur):
    gh, rid = FakeGH(), "o/app@0.9.0"
    deploy(cur, rid)
    qa_gate.step(cur, gh, REPO, now=NOW)
    j = jobs_of(cur, rid)
    finish(cur, j["verdict"][0], "failed")
    finish(cur, j["explore"][0])
    explored(cur, j["explore"][0], [])
    out = qa_gate.step(cur, gh, REPO, now=NOW)
    assert [x["which"] for x in out["enqueued"]] == ["verdict"] and out["decided"] is None   # 1 回だけ積み直す
    j2 = jobs_of(cur, rid)["verdict"][1]
    finish(cur, j2)
    verdicts(cur, j2, [("a", "blocked", []), ("b", "blocked", [])])   # blocked だけも確かめられなかったうち
    d = qa_gate.step(cur, gh, REPO, now=NOW)["decided"]
    assert d["candidates"] == 0 and d["verdict"]["status"] == "unconfirmed"
    assert any("確かめられなかった" in r["text"] for r in d["report"])


def test_succeeded_but_not_yet_recorded_waits(cur):
    gh, rid = FakeGH(), "o/app@0.9.0"
    deploy(cur, rid)
    qa_gate.step(cur, gh, REPO, now=NOW)
    j = jobs_of(cur, rid)
    finish(cur, j["verdict"][0], ago=timedelta(minutes=1))   # 終わったが dispatcher がまだ記録していない
    assert qa_gate.verdict_outcome(cur, j["verdict"][0], NOW) is None
    finish(cur, j["verdict"][0], ago=timedelta(minutes=30))   # 猶予を過ぎても記録がない
    assert qa_gate.verdict_outcome(cur, j["verdict"][0], NOW)["status"] == "unconfirmed"


def test_times_out_and_supersedes(cur):
    gh = FakeGH()
    deploy(cur, "o/app@0.9.0")
    qa_gate.step(cur, gh, REPO, now=NOW)
    deploy(cur, "o/app@0.9.1")   # 結果の前に次の版
    cur.execute("update jobs set state='cancelled', finished_at=now()")   # 同じ Issue に動くジョブがあると積めないので、前のジョブを終わらせる
    out = qa_gate.step(cur, gh, REPO, now=NOW)
    assert out["opened"] == "o/app@0.9.1"
    assert qa_gate.gate(cur, "o/app@0.9.0")["state"] == "superseded"
    d = qa_gate.step(cur, gh, REPO, now=NOW + timedelta(hours=7))["decided"]   # 時間切れはそろった分だけ
    assert d["release_id"] == "o/app@0.9.1" and d["timed_out"]


def test_no_gate_for_rollback_or_over_budget(cur):
    gh = FakeGH()
    assert qa_gate.step(cur, gh, REPO, now=NOW, may_enqueue=False)["opened"] is None
    deploy(cur, "o/app@0.9.0")
    assert qa_gate.step(cur, gh, REPO, now=NOW, may_enqueue=False)["opened"] is None   # 予算の枠を使い切っている間は開かない
    assert qa_gate.to_open(cur, REPO) == "o/app@0.9.0"
    ship.record(cur, "release.rolled_back", "o/app@0.8.0", {"repo": REPO, "env": "staging", "ok": True})
    assert qa_gate.to_open(cur, REPO) is None   # 戻した版は最後の配備ではない


def test_approval_waits_for_report_and_carries_it(cur):
    gh, rid = FakeGH(), "o/app@0.9.0"
    ship.observe_stage(cur, REPO, "launch")
    deploy(cur, rid)
    qa_gate.step(cur, gh, REPO, now=NOW)
    assert inbox._releases(cur) == []   # 報告の前は本番待ちに出さない
    ok, msg = ship.request_approve(cur, rid, "owner", "factoryctl")
    assert not ok and "--skip-qa" in msg
    j = jobs_of(cur, rid)
    for w in qa_gate.WHICH:
        finish(cur, j[w][0])
    verdicts(cur, j["verdict"][0], [("s", "hold", [{"title": "入口がない", "severity": "s2"}])])
    explored(cur, j["explore"][0], [])
    qa_gate.step(cur, gh, REPO, now=NOW)
    [it] = inbox._releases(cur)
    assert "候補が 1 件" in it["action"] and any(r["label"] == "候補 1" for r in it["detail"])
    assert [e["id"] for e in it["answer"]["extra"]] == ["block"]
    ok, msg = ship.request_approve(cur, rid, "owner", "factoryctl")
    assert ok
    cur.execute("select payload from events where name='release.approved'")
    assert cur.fetchone()["payload"]["qa_gate"]["candidates"] == 1


def test_skip_qa_and_block(cur):
    gh, rid = FakeGH(), "o/app@0.9.0"
    ship.observe_stage(cur, REPO, "launch")
    deploy(cur, rid)
    qa_gate.step(cur, gh, REPO, now=NOW)
    ok, _ = ship.request_approve(cur, rid, "owner", "factoryctl", skip_qa="S1 の修正を急ぐ")
    assert ok
    cur.execute("select payload from events where name='release.approved'")
    assert cur.fetchone()["payload"]["qa_skipped"] == "S1 の修正を急ぐ"
    cur.execute("delete from events where name='release.approved'")
    j = jobs_of(cur, rid)
    for w in qa_gate.WHICH:
        finish(cur, j[w][0])
    verdicts(cur, j["verdict"][0], [("s", "pass", [])])
    explored(cur, j["explore"][0], [{"title": "他人の予約が見える", "severity": "s1"}])
    qa_gate.step(cur, gh, REPO, now=NOW)
    assert inbox._apply(cur, ("release", rid), "block", "owner", "console", "", None) == (False, "どれをブロッカーにしたか、コメントが要る")
    ok, _ = inbox._apply(cur, ("release", rid), "block", "owner", "console", "他人の予約が見えるのは出せない", None)
    assert ok and inbox._releases(cur) == []   # 見送った版は本番待ちに出さない


def test_development_product_records_only(cur):
    gh, rid = FakeGH(), "o/app@0.9.0"
    deploy(cur, rid)   # 段階を読めていない製品は development（本番を立てない）
    qa_gate.step(cur, gh, REPO, now=NOW)
    assert qa_gate.gate(cur, rid)["state"] == "open"
    assert inbox._releases(cur) == []


def test_nightly_explore_moves_to_explore_issue(cur):
    gh = FakeGH()
    got = qa_line.enqueue_explore(cur, gh, REPO, "2026-11-10")
    assert got["issue"] == 8


# ------------------------------------------------------------------ 版ごとのチケットとシナリオごとのジョブ（ADR 0054）
STORY = "type: UserStory\nid: {id}\nstatus: {status}\n"


class ReleaseGH(FakeGH):
    """製品の specs/stories/ に active が 3 本（a・b・c）と、止めた 1 本、ストーリーでない 1 本。版ごとの Issue は 50 から"""
    def __init__(self):
        super().__init__()
        self.next_issue = 50

    def __call__(self, method, path, body=None):
        import base64
        if method == "GET" and path.endswith("/contents/specs/stories"):
            self.calls.append((method, path, body))
            return [{"name": n} for n in ("a.yaml", "b.yaml", "c.yaml", "old.yaml", "actors.yaml", "README.md")]
        if method == "GET" and "/contents/specs/stories/" in path:
            name = path.rsplit("/", 1)[1].removesuffix(".yaml")
            text = "type: Actors\n" if name == "actors" else STORY.format(id=name, status="retired" if name == "old" else "active")
            return {"content": base64.b64encode(text.encode()).decode()}
        if method == "POST" and path.endswith("/issues") and (body or {}).get("labels") == ["factory:qa-release"]:
            self.calls.append((method, path, body))
            self.next_issue += 1
            return {"number": self.next_issue}
        return super().__call__(method, path, body)

    def closed(self):
        return [int(p.rsplit("/", 1)[1]) for m, p, b in self.calls if m == "PATCH" and (b or {}).get("state") == "closed"]


def units_of(cur, rid):
    return {u: [j["job_id"] for j in js] for u, js in qa_gate.gate(cur, rid)["units"].items()}


def run_all_verdicts(cur, gh, rid, rows):
    """判定は同時に 1 本ずつ。積まれた順に終わらせ、次の周回で次を積ませる"""
    for story, decision, defects in rows:
        jid = units_of(cur, rid)[f"verdict:{story}"][-1]
        finish(cur, jid)
        verdicts(cur, jid, [(story, decision, defects)])
        qa_gate.step(cur, gh, REPO, now=NOW)


def test_release_ticket_gets_one_job_per_scenario(cur):
    gh, rid = ReleaseGH(), "o/app@0.9.0"
    deploy(cur, rid)
    out = qa_gate.step(cur, gh, REPO, now=NOW)
    plan = qa_gate.gate(cur, rid)["plan"]
    assert plan["legacy"] is False and plan["issue"] == 51 and plan["corr"] == "github:o/app#51"
    assert [u["unit"] for u in plan["units"]] == ["verdict:a", "verdict:b", "verdict:c", "explore:detour"]   # 止めたストーリーは数えない
    assert plan["cap_usd"] == (3 * 3 + 5) * 1.5
    assert sorted(x["unit"] for x in out["enqueued"]) == ["explore:detour", "verdict:a"]   # 判定は同時に 1 本、探索は並ぶ
    cur.execute("select issue_number, unit from jobs order by id")
    assert [(r["issue_number"], r["unit"]) for r in cur.fetchall()] == [(51, "verdict:a"), (51, "explore:detour")]   # 同じ Issue に並ぶ
    cur.execute("select payload->'input' as i from events where name='job.enqueued' order by id")
    v, x = [r["i"] for r in cur.fetchall()]
    assert v["stories"] == ["a"] and v["max_turns"] == 40 and v["max_budget_usd"] == 3 and v["gate"] == rid
    assert x["mode"] == "explore" and x["max_budget_usd"] == 5
    assert qa_gate.step(cur, gh, REPO, now=NOW)["enqueued"] == []   # a が終わるまで b は積まない


def test_release_ticket_reports_and_closes(cur):
    gh, rid = ReleaseGH(), "o/app@0.9.0"
    deploy(cur, rid)
    qa_gate.step(cur, gh, REPO, now=NOW)
    ex = units_of(cur, rid)["explore:detour"][0]
    finish(cur, ex)
    explored(cur, ex, [{"title": "他人の予約が見える", "severity": "s1"}])
    run_all_verdicts(cur, gh, rid, [("a", "pass", []), ("b", "hold", [{"title": "入口がない", "severity": "s2", "known_gap": False}])])
    assert qa_gate.gate(cur, rid)["state"] == "open"   # c を待つ
    jid = units_of(cur, rid)["verdict:c"][-1]
    finish(cur, jid)
    verdicts(cur, jid, [("c", "blocked", [])])
    assert qa_gate.step(cur, gh, REPO, now=NOW)["enqueued"][0]["unit"] == "verdict:c"   # 確かめられなかった 1 本だけ積み直す
    assert len(units_of(cur, rid)["verdict:a"]) == 1
    jid = units_of(cur, rid)["verdict:c"][-1]
    finish(cur, jid)
    verdicts(cur, jid, [("c", "blocked", [])])
    d = qa_gate.step(cur, gh, REPO, now=NOW)["decided"]
    assert d["candidates"] == 2 and d["issue"] == 51 and d["verdict"]["stories"] == {"pass": 1, "hold": 1, "blocked": 1}
    assert [m["story_id"] for m in d["verdict"]["missing"]] == ["c"]
    text = " ".join(r["text"] for r in d["report"])
    assert "c（" in text
    assert "出荷の QA の関門" in gh.comments(51)[0] and gh.closed() == [51] and gh.comments(7) == []   # 報告は版の Issue に書いて閉じる


def test_release_ticket_cost_cap_reports_without_human_stoppage(cur):
    import workflow_guard
    import workflow_shadow
    gh, rid = ReleaseGH(), "o/app@0.9.0"
    deploy(cur, rid)
    qa_gate.step(cur, gh, REPO, now=NOW)
    corr = "github:o/app#51"
    assert workflow_shadow.gate_caps(cur, corr) == {"ticket.cost_usd": 21.0, "retries.qa_rerun": None}
    assert not workflow_shadow.standing_qa(cur, corr)
    u = units_of(cur, rid)
    for jid in (u["verdict:a"][0], u["explore:detour"][0]):
        finish(cur, jid)
    cur.execute("update jobs set result=%s where id=%s", (Jsonb({"cost_usd": 22.0}), u["verdict:a"][0]))
    verdicts(cur, u["verdict:a"][0], [("a", "pass", [])])
    explored(cur, u["explore:detour"][0], [])
    assert workflow_guard.cost_gate(cur, corr)["hit"] == ["ticket.cost_usd"]
    out = qa_gate.step(cur, gh, REPO, now=NOW)
    assert out["enqueued"] == [] and out["decided"]["capped"] is True   # 残りを積まず、そろった分で報告を作る
    assert [m["story_id"] for m in out["decided"]["verdict"]["missing"]] == ["b", "c"]
    assert any(r["label"] == "費用の上限" for r in out["decided"]["report"])


def test_release_ticket_supersede_closes_issue(cur):
    gh = ReleaseGH()
    deploy(cur, "o/app@0.9.0")
    qa_gate.step(cur, gh, REPO, now=NOW)
    deploy(cur, "o/app@0.10.0", "0.10.0")
    out = qa_gate.step(cur, gh, REPO, now=NOW)
    assert out["opened"] == "o/app@0.10.0" and gh.closed() == [51]
    assert qa_gate.gate(cur, "o/app@0.10.0")["plan"]["issue"] == 52


def test_plan_falls_back_to_fixed_issues(cur):
    gh, rid = FakeGH(), "o/app@0.9.0"   # ストーリーを数えられない製品
    deploy(cur, rid)
    out = qa_gate.step(cur, gh, REPO, now=NOW)
    assert qa_gate.gate(cur, rid)["plan"]["legacy"] is True
    assert sorted(x["which"] for x in out["enqueued"]) == ["explore", "verdict"]
