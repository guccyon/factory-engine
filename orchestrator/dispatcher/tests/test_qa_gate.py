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
    for n in ("qa.gate_opened", "qa.gate_plan", "qa.gate_job", "qa.gate_superseded", "qa.gate_decided", "qa.gate_blocked"):
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
    return {w: [j["job_id"] for j in g["jobs"][w]] for w in qa_gate.ALL}


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


class StoriesGH(FakeGH):
    """製品の specs/stories/ に、時刻をまたぐストーリー 1 本とまたがないストーリー 2 本がある"""
    STORIES = {"a-prejoin.yaml": {"type": "UserStory", "id": "a-prejoin", "status": "active", "time": ["開始 5 分前をまたぐ"]},
               "b-chat.yaml": {"type": "UserStory", "id": "b-chat", "status": "active"},
               "c-old.yaml": {"type": "UserStory", "id": "c-old", "status": "retired", "time": ["翌日"]},
               "d-cs.yaml": {"type": "UserStory", "id": "d-cs"}}

    def __call__(self, method, path, body=None):
        import base64
        if method == "GET" and path.endswith("/contents/specs/stories"):
            self.calls.append((method, path, body))
            return [{"type": "file", "name": n, "path": f"specs/stories/{n}"} for n in [*self.STORIES, "actors.yaml", "README.md"]]
        if method == "GET" and "/contents/specs/stories/" in path:
            self.calls.append((method, path, body))
            doc = self.STORIES[path.rsplit("/", 1)[1]]
            return {"content": base64.b64encode(yaml.safe_dump(doc, allow_unicode=True).encode()).decode()}
        return super().__call__(method, path, body)


def test_time_stories_run_last_alone(cur, monkeypatch):
    """ADR 0055 の決定 3: 時刻をまたぐストーリーは判定から外し、判定と探索が終わってから 1 つだけで流す。
    単位の列がない DB（009_job_unit.sql の前）での、固定 Issue に積む形"""
    monkeypatch.setattr(qa_line, "unit_ready", lambda cur: False)
    gh, rid = StoriesGH(), "o/app@0.9.0"
    deploy(cur, rid)
    out = qa_gate.step(cur, gh, REPO, now=NOW)
    assert sorted(x["which"] for x in out["enqueued"]) == ["explore", "verdict"]   # 時刻の判定はまだ積まない
    g = qa_gate.gate(cur, rid)
    assert g["plan"]["time_stories"] == ["a-prejoin"] and g["plan"]["other_stories"] == ["b-chat", "d-cs"]
    cur.execute("select payload->'input'->'stories' as s from events where name='job.enqueued' order by id limit 1")
    assert cur.fetchone()["s"] == ["b-chat", "d-cs"]
    j = jobs_of(cur, rid)
    finish(cur, j["verdict"][0])
    verdicts(cur, j["verdict"][0], [("b-chat", "pass", []), ("d-cs", "pass", [])])
    assert qa_gate.step(cur, gh, REPO, now=NOW)["enqueued"] == []                  # 探索が動いている間は積まない
    finish(cur, j["explore"][0])
    explored(cur, j["explore"][0], [])
    out = qa_gate.step(cur, gh, REPO, now=NOW)
    assert [x["which"] for x in out["enqueued"]] == [qa_gate.TIME] and out["decided"] is None
    cur.execute("select payload->'input'->'stories' as s from events where name='job.enqueued' order by id desc limit 1")
    assert cur.fetchone()["s"] == ["a-prejoin"]
    t = jobs_of(cur, rid)[qa_gate.TIME][0]
    finish(cur, t)
    verdicts(cur, t, [("a-prejoin", "hold", [{"title": "入室のボタンが押せない", "severity": "s2"}])])
    d = qa_gate.step(cur, gh, REPO, now=NOW)["decided"]
    assert d["candidates"] == 1 and d[qa_gate.TIME]["stories"] == {"hold": 1}
    assert any(r["label"] == "判定（時刻をまたぐ）" and "止める" in r["text"] for r in d["report"])
    lists = [p for m, p, b in gh.calls if m == "GET" and p.endswith("/contents/specs/stories")]
    assert len(lists) == 1                                                          # 分け方は 1 回だけ読む


def test_plan_falls_back_when_stories_cannot_be_read(cur):
    """製品のストーリーを読めなければ分けない（今までどおり判定は全部、時刻の判定は積まない）"""
    class Broken(FakeGH):
        def __call__(self, method, path, body=None):
            if "/contents/" in path:
                raise RuntimeError("404")
            return super().__call__(method, path, body)
    gh, rid = Broken(), "o/app@0.9.0"
    deploy(cur, rid)
    qa_gate.step(cur, gh, REPO, now=NOW, log=lambda *a: None)
    p = qa_gate.gate(cur, rid)["plan"]
    assert p["time_stories"] == [] and p["other_stories"] is None and p["error"]
    cur.execute("select payload->'input'->'stories' as s from events where name='job.enqueued' order by id limit 1")
    assert cur.fetchone()["s"] == []


# ------------------------------------------------------------------ 版ごとのチケットとシナリオごとのジョブ（ADR 0054）
class ReleaseGH(StoriesGH):
    """StoriesGH のストーリー（またがない b-chat・d-cs、時刻をまたぐ a-prejoin）。版ごとの Issue は 51 から"""
    def __init__(self):
        super().__init__()
        self.next_issue = 50

    def __call__(self, method, path, body=None):
        if method == "POST" and path.endswith("/issues") and (body or {}).get("labels") == ["factory:qa-release"]:
            self.calls.append((method, path, body))
            self.next_issue += 1
            return {"number": self.next_issue}
        return super().__call__(method, path, body)

    def closed(self):
        return [int(p.rsplit("/", 1)[1]) for m, p, b in self.calls if m == "PATCH" and (b or {}).get("state") == "closed"]


def units_of(cur, rid):
    return {u: [j["job_id"] for j in js] for u, js in qa_gate.gate(cur, rid)["units"].items()}


def judged(cur, gh, rid, unit, decision, defects=()):
    """その単位のいまのジョブを終わらせて判定を残し、次の周回を回す"""
    jid = units_of(cur, rid)[unit][-1]
    finish(cur, jid)
    verdicts(cur, jid, [(unit.split(":", 1)[1], decision, list(defects))])
    return qa_gate.step(cur, gh, REPO, now=NOW)


def test_release_ticket_gets_one_job_per_scenario(cur):
    gh, rid = ReleaseGH(), "o/app@0.9.0"
    deploy(cur, rid)
    out = qa_gate.step(cur, gh, REPO, now=NOW)
    plan = qa_gate.gate(cur, rid)["plan"]
    assert plan["issue"] == 51 and plan["corr"] == "github:o/app#51" and plan["time_stories"] == ["a-prejoin"]
    assert [u["unit"] for u in plan["units"]] == ["verdict:b-chat", "verdict:d-cs", "explore:detour", "verdict_time:a-prejoin"]
    assert plan["cap_usd"] == (3 + 3 + 5 + 5) * 1.5
    assert sorted(x["unit"] for x in out["enqueued"]) == ["explore:detour", "verdict:b-chat"]   # 判定は同時に 1 本、探索は並ぶ
    cur.execute("select issue_number, unit from jobs order by id")
    assert [(r["issue_number"], r["unit"]) for r in cur.fetchall()] == [(51, "verdict:b-chat"), (51, "explore:detour")]   # 同じ Issue に並ぶ
    cur.execute("select payload->'input' as i from events where name='job.enqueued' order by id")
    v, x = [r["i"] for r in cur.fetchall()]
    assert v["stories"] == ["b-chat"] and v["max_turns"] == 40 and v["max_budget_usd"] == 3 and v["gate"] == rid
    assert x["mode"] == "explore" and x["max_budget_usd"] == 5
    assert qa_gate.step(cur, gh, REPO, now=NOW)["enqueued"] == []   # b-chat が終わるまで d-cs は積まない
    assert len([p for m, p, b in gh.calls if m == "GET" and p.endswith("/contents/specs/stories")]) == 1


def test_release_ticket_runs_time_story_last_then_reports_and_closes(cur):
    gh, rid = ReleaseGH(), "o/app@0.9.0"
    deploy(cur, rid)
    qa_gate.step(cur, gh, REPO, now=NOW)
    out = judged(cur, gh, rid, "verdict:b-chat", "pass")
    assert [x["unit"] for x in out["enqueued"]] == ["verdict:d-cs"]
    out = judged(cur, gh, rid, "verdict:d-cs", "blocked")
    assert [x["unit"] for x in out["enqueued"]] == ["verdict:d-cs"]   # 確かめられなかった 1 本だけ積み直す
    out = judged(cur, gh, rid, "verdict:d-cs", "blocked")
    assert out["enqueued"] == [] and out["decided"] is None             # 探索が動いている間は、時刻をまたぐものを積まない
    ex = units_of(cur, rid)["explore:detour"][0]
    finish(cur, ex)
    explored(cur, ex, [{"title": "他人の予約が見える", "severity": "s1"}])
    out = qa_gate.step(cur, gh, REPO, now=NOW)
    assert [x["unit"] for x in out["enqueued"]] == ["verdict_time:a-prejoin"]
    cur.execute("select payload->'input' as i from events where name='job.enqueued' order by id desc limit 1")
    assert cur.fetchone()["i"]["max_turns"] == 80
    d = judged(cur, gh, rid, "verdict_time:a-prejoin", "hold", [{"title": "入室のボタンが押せない", "severity": "s2"}])["decided"]
    assert d["candidates"] == 2 and d["issue"] == 51
    assert d["verdict"]["stories"] == {"pass": 1, "blocked": 1} and d[qa_gate.TIME]["stories"] == {"hold": 1}
    assert [m["story_id"] for m in d["verdict"]["missing"]] == ["d-cs"]
    assert any(r["label"] == "そろわなかったシナリオ" and "d-cs（" in r["text"] for r in d["report"])
    assert len(units_of(cur, rid)["verdict:b-chat"]) == 1
    assert "出荷の QA の関門" in gh.comments(51)[0] and gh.closed() == [51] and gh.comments(7) == []   # 報告は版の Issue に書いて閉じる


def test_release_ticket_cost_cap_reports_without_human_stoppage(cur):
    import workflow_guard
    import workflow_shadow
    gh, rid = ReleaseGH(), "o/app@0.9.0"
    deploy(cur, rid)
    qa_gate.step(cur, gh, REPO, now=NOW)
    corr = "github:o/app#51"
    assert workflow_shadow.gate_caps(cur, corr) == {"ticket.cost_usd": 24.0, "retries.qa_rerun": None}
    assert not workflow_shadow.standing_qa(cur, corr)
    u = units_of(cur, rid)
    for jid in (u["verdict:b-chat"][0], u["explore:detour"][0]):
        finish(cur, jid)
    cur.execute("update jobs set result=%s where id=%s", (Jsonb({"cost_usd": 25.0}), u["verdict:b-chat"][0]))
    verdicts(cur, u["verdict:b-chat"][0], [("b-chat", "pass", [])])
    explored(cur, u["explore:detour"][0], [])
    assert workflow_guard.cost_gate(cur, corr)["hit"] == ["ticket.cost_usd"]
    out = qa_gate.step(cur, gh, REPO, now=NOW)
    assert out["enqueued"] == [] and out["decided"]["capped"] is True   # 残りを積まず、そろった分で報告を作る
    assert [m["story_id"] for m in out["decided"]["verdict"]["missing"]] == ["d-cs"]
    assert [m["story_id"] for m in out["decided"][qa_gate.TIME]["missing"]] == ["a-prejoin"]
    assert any(r["label"] == "費用の上限" for r in out["decided"]["report"])


def test_release_ticket_supersede_closes_issue(cur):
    gh = ReleaseGH()
    deploy(cur, "o/app@0.9.0")
    qa_gate.step(cur, gh, REPO, now=NOW)
    deploy(cur, "o/app@0.10.0", "0.10.0")
    out = qa_gate.step(cur, gh, REPO, now=NOW)
    assert out["opened"] == "o/app@0.10.0" and gh.closed() == [51]
    assert qa_gate.gate(cur, "o/app@0.10.0")["plan"]["issue"] == 52


def test_plan_without_stories_stays_on_fixed_issues(cur):
    gh, rid = FakeGH(), "o/app@0.9.0"   # ストーリーを数えられない製品
    deploy(cur, rid)
    out = qa_gate.step(cur, gh, REPO, now=NOW)
    p = qa_gate.gate(cur, rid)["plan"]
    assert "units" not in p and p["legacy_why"]
    assert sorted(x["which"] for x in out["enqueued"]) == ["explore", "verdict"]


# ------------------------------------------------------------------ QA だけのチケット（ADR 0056）
class TicketGH(ReleaseGH):
    """QA だけのチケット #30（factory:qa-ticket）"""
    def __call__(self, method, path, body=None):
        if method == "GET" and "factory%3Aqa-ticket" in path:
            self.calls.append((method, path, body))
            return [{"number": 30}]
        return super().__call__(method, path, body)


def verify_plan(cur, n=30, stage=False, scope=("a-prejoin", "b-chat"), effort="standard"):
    wf = {"path": "verify", "steps": [{"step": "stage", "run": stage}, {"step": "qa", "run": True, "effort": effort, "scope": list(scope)},
                                      {"step": "respond", "run": True}, {"step": "learn", "run": True}],
          "caps": {"ticket.cost_usd": 15}, "caps_declared": {}}
    cur.execute("insert into artifacts(type, correlation_id, job_id, produced_by, payload) values ('Plan',%s,null,'plan',%s)",
                (f"github:{REPO}#{n}", Jsonb({"decision": "ready", "steps": [], "workflow": wf})))


def tickets(out):
    return {k: [t[k] for t in out.get("tickets") or [] if k in t] for k in ("opened", "enqueued", "decided")}


def test_qa_ticket_runs_scope_on_current_staging_and_closes(cur):
    gh, rid = TicketGH(), "o/app@0.9.0"
    deploy(cur, rid)
    qa_gate.step(cur, gh, REPO, now=NOW)                      # 版の関門は別に開く（このテストでは見ない）
    assert tickets(qa_gate.step(cur, gh, REPO, now=NOW))["opened"] == []   # 道 verify の計画がなければ何もしない
    verify_plan(cur)
    assert tickets(qa_gate.step(cur, gh, REPO, now=NOW))["opened"] == [True]
    assert "いま staging にある版 0.9.0" in gh.comments(30)[0]
    out = qa_gate.step(cur, gh, REPO, now=NOW)
    key = qa_gate.ticket_key(REPO, 30)
    pl = qa_gate.gate(cur, key)["plan"]
    assert [u["unit"] for u in pl["units"]] == ["verdict:b-chat", "verdict_time:a-prejoin"] and pl["issue"] == 30   # 範囲のストーリーだけ。探索なし
    assert pl["cap_usd"] == (3 + 5) * 1.5
    cur.execute("select issue_number, unit from jobs where issue_number=30 order by id")
    assert [(r["issue_number"], r["unit"]) for r in cur.fetchall()] == [(30, "verdict:b-chat")]
    assert key not in qa_gate.open_gates(cur, REPO)          # 版の関門の一覧には出ない
    jid = units_of(cur, key)["verdict:b-chat"][0]
    finish(cur, jid)
    verdicts(cur, jid, [("b-chat", "pass", [])])
    out = qa_gate.step(cur, gh, REPO, now=NOW)
    assert tickets(out)["enqueued"] == [[units_of(cur, key)["verdict_time:a-prejoin"][0]]]   # 時刻をまたぐものは最後
    t = units_of(cur, key)["verdict_time:a-prejoin"][0]
    finish(cur, t)
    verdicts(cur, t, [("a-prejoin", "hold", [{"title": "入室のボタンが押せない", "severity": "s2"}])])
    d = tickets(qa_gate.step(cur, gh, REPO, now=NOW))["decided"][0]
    assert d["candidates"] == 1 and d["ticket"] == 30 and d["on_release"] == rid
    assert 30 in gh.closed() and "QA だけのチケットの結果" in gh.comments(30)[-1] and "本番への出荷には何もしていません" in gh.comments(30)[-1]
    assert qa_gate.gate(cur, key)["state"] == "decided"
    cur.execute("select count(*) as n from events where name='release.approved'")
    assert cur.fetchone()["n"] == 0


def test_qa_ticket_stage_ships_and_reuses_release_gate(cur):
    gh = TicketGH()
    deploy(cur, "o/app@0.9.0")
    qa_gate.step(cur, gh, REPO, now=NOW)
    cur.execute("insert into events(name, correlation_id, payload) values ('pr.merged','c',%s)",
                (Jsonb({"pr_url": f"https://github.com/{REPO}/pull/9"}),))
    verify_plan(cur, stage=True, scope=("b-chat",))
    qa_gate.step(cur, gh, REPO, now=NOW)
    key = qa_gate.ticket_key(REPO, 30)
    staged = qa_gate.gate(cur, key)["opened"]["staged_release"]
    assert staged and staged != "o/app@0.9.0"
    cur.execute("select payload from events where name='release.ship_requested' and payload->>'release_id'=%s", (staged,))
    assert cur.fetchone()["payload"]["by"] == "qa_ticket:o/app#30"
    assert tickets(qa_gate.step(cur, gh, REPO, now=NOW)) == {"opened": [], "enqueued": [], "decided": []}   # 出荷を待つ（自分では積まない）
    ship.record(cur, "release.deployed", staged, {"repo": REPO, "env": "staging", "ok": True, "request_id": 0})
    out = qa_gate.step(cur, gh, REPO, now=NOW)
    assert out["opened"] == staged
    g = qa_gate.gate(cur, staged)
    for u in g["plan"]["units"]:   # 版の関門をすべて終わらせる
        while True:
            jid = units_of(cur, staged).get(u["unit"], [None])[-1]
            if jid is None:
                qa_gate.step(cur, gh, REPO, now=NOW)
                jid = units_of(cur, staged)[u["unit"]][-1]
            finish(cur, jid)
            if u["which"] == "explore":
                explored(cur, jid, [], gate=staged)
            else:
                verdicts(cur, jid, [(u["story"], "pass" if u["story"] == "b-chat" else "hold", [] if u["story"] == "b-chat" else [{"title": "x", "severity": "s2"}])])
            break
        qa_gate.step(cur, gh, REPO, now=NOW)
    assert qa_gate.gate(cur, staged)["state"] == "decided"
    d = qa_gate.gate(cur, key)["decided"]
    assert d["via_release"] == staged and d["verdict"]["stories"] == {"pass": 1} and d["candidates"] == 0   # 範囲の b-chat だけを抜き出す
    cur.execute("select count(*) as n from jobs where issue_number=30")
    assert cur.fetchone()["n"] == 0 and 30 in gh.closed()   # このチケットには積まない（同じ版を 2 回流さない）


def test_story_id_accepts_paths():
    assert [qa_gate.story_id(x) for x in ("b-chat", "specs/stories/b-chat.yaml", " a-prejoin.yml")] == ["b-chat", "b-chat", "a-prejoin"]


def test_qa_ticket_scope_written_as_files(cur):
    """プランナーが scope をファイルの道筋で書いても、ストーリーの id として流す（rally#1470）。stage の auto は動かすとして読む"""
    gh = TicketGH()
    deploy(cur, "o/app@0.9.0")
    qa_gate.step(cur, gh, REPO, now=NOW)
    verify_plan(cur, scope=("specs/stories/b-chat.yaml",))
    cur.execute("update artifacts set payload = jsonb_set(payload, '{workflow,steps,0,run}', '\"auto\"') where type='Plan'")
    assert qa_gate.verify_decl(cur, "github:o/app#30")["stage"] is True
    qa_gate.step(cur, gh, REPO, now=NOW)
    qa_gate.step(cur, gh, REPO, now=NOW)
    pl = qa_gate.gate(cur, qa_gate.ticket_key(REPO, 30))["plan"]
    assert [u["unit"] for u in pl["units"]] == ["verdict:b-chat"] and not pl.get("unknown")
