"""合成の利用者（ADR 0043 の O4）と品質の Learn（O9）: 定義の写し、提案の拾い方、記録と受信トレイ、重点とシナリオ、委員会への案。
GitHub は差し替え、DB は TEST_DATABASE_URL の Postgres（なければ飛ばす）"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
URL = os.environ.get("TEST_DATABASE_URL")
ROOT = Path(__file__).resolve().parents[3]
JST = timezone(timedelta(hours=9))
REPO = "o/app"

import qa_line  # noqa: E402
import quality_learn  # noqa: E402

if URL:
    os.environ.setdefault("DATABASE_URL", URL)
    os.environ.setdefault("GH_TOKEN", "x")
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb

    import defect
    import inbox
    import supervisor


def test_defs_copies_match_definitions():
    sd = yaml.safe_load((ROOT / "definitions" / "qa.yaml").read_text(encoding="utf-8"))["synthetic"]
    for k, v in qa_line.DEFS["synthetic"].items():
        assert sd[k] == v, k
    ld = yaml.safe_load((ROOT / "definitions" / "defects.yaml").read_text(encoding="utf-8"))
    assert ld["learn"] == quality_learn.DEFS
    assert tuple(s["id"] for s in ld["stages"] if not s["before_merge"]) == quality_learn.AFTER_MERGE
    names = yaml.safe_load((ROOT / "definitions" / "events.yaml").read_text(encoding="utf-8"))
    for n in ("qa.synthetic", "supervisor.synthetic_users_enqueued", "opportunity.proposed", "quality.learned", "qa.focus_updated",
              "qa.scenario_added", "quality.proposal_drafted", "quality.proposal_filed", "opportunity.ticket_requested", "opportunity.ticket_filed"):
        assert n in str(names), n


def test_friction_from_env_value():
    assert qa_line.friction_from("") == {} and qa_line.friction_from("off") == {} and qa_line.friction_from("{bad") == {}
    assert qa_line.friction_from("example") == qa_line.DEFS["synthetic"]["friction_example"]
    assert qa_line.friction_from('{"slow": {"path": "/api/x", "delay_ms": 10}, "other": {}}') == {"slow": {"path": "/api/x", "delay_ms": 10}}


SY = {"timings": [{"entry": "ADMIN_WEB_PORT", "method": "GET", "path": "/api/bookings", "n": 3, "p50": 2600, "p95": 2700, "max": 2700},
                  {"entry": "ADMIN_WEB_PORT", "method": "GET", "path": "/api/slow-once", "n": 1, "p50": 3000, "p95": 3000, "max": 3000},
                  {"entry": "ADMIN_WEB_PORT", "method": "GET", "path": "/api/slots", "n": 4, "p50": 40, "p95": 60, "max": 60}],
      "walks": [{"story_id": "cs-traces", "round": 1, "entry": "ADMIN_WEB_PORT", "found_entry": False, "attempts_to_entry": 5, "reached_exit": False,
                 "steps": [{"n": 1, "did": "トップで予約の一覧を探した", "note": "予約へのリンクが見当たらない", "waited": "none", "stuck": True}]},
                {"story_id": "cs-traces", "round": 2, "entry": "ADMIN_WEB_PORT", "found_entry": True, "attempts_to_entry": 4, "reached_exit": True,
                 "steps": [{"n": 1, "did": "URL の候補を探した", "waited": "long", "stuck": False}]},
                {"story_id": "coach-replies", "round": 1, "entry": "ADMIN_WEB_PORT", "found_entry": True, "attempts_to_entry": 1, "reached_exit": True,
                 "steps": [{"n": 1, "did": "送った", "event": "message.sent", "waited": "none", "stuck": False}]}],
      "interpretation": "予約の一覧が遅く、入口も見つけにくい", "confidence": 0.7, "requests": 12}
PLANTED = {"slow": {"path": "/api/bookings", "delay_ms": 2500}, "hide": {"entry": "ADMIN_WEB_PORT", "selector": "a"}}


def test_signals_pick_slow_and_hard_entry():
    sd = qa_line.DEFS["synthetic"]
    sig = qa_line.signals(SY, sd)
    assert [s["kind"] for s in sig] == ["slow", "hard_entry"]   # 1 回しか呼ばれていないパスと速いパスは拾わない
    assert sig[0]["path"] == "/api/bookings" and sig[0]["theme"] == 2 and sig[1]["story_id"] == "cs-traces" and sig[1]["theme"] == 1
    assert sig[1]["hard"] == 2 and sig[1]["not_found"] == 1
    assert [qa_line.planted_hit(s, PLANTED) for s in sig] == ["slow", "hide"]
    assert qa_line.planted_hit(sig[0], {}) is None
    o = qa_line.opportunity_of(sig[0], "r1")
    assert o["expected_effect"]["direction"] == "down" and "2700ms" in o["title"]


class FakeGH:
    def __init__(self):
        self.calls, self.n = [], 100

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET":
            return [{"number": 7}]
        self.n += 1
        return {"number": self.n, "html_url": f"https://github.com/{REPO}/issues/{self.n}"}


@pytest.fixture()
def cur():
    if not URL:
        pytest.skip("TEST_DATABASE_URL がない")
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


def test_enqueue_and_record_synthetic(cur):
    gh, comments = FakeGH(), []
    got = qa_line.enqueue_synthetic(cur, gh, REPO, friction=PLANTED, week="2026-W45")
    assert got["friction"] == PLANTED
    cur.execute("select payload->'input' as i from events where name='job.enqueued' and job_id=%s", (got["job_id"],))
    inp = cur.fetchone()["i"]
    assert inp["mode"] == "synthetic" and inp["friction"] == PLANTED and inp["week"] == "2026-W45"
    # staging 以外には仕込まない
    cur.execute("update jobs set state='succeeded' where id=%s", (got["job_id"],))
    other = qa_line.enqueue_synthetic(cur, gh, REPO, env="prod", friction=PLANTED)
    assert other["friction"] == {}
    res = {"mode": "synthetic", "env": "staging", "release_id": "r9", "model": "m", "synthetic": {**SY, "planted": {"slow": {"path": "/x"}}}}
    out = qa_line.record_synthetic(cur, gh, lambda r, n, b: comments.append((n, b)), REPO, got["issue"], got["corr"], got["job_id"], res)
    assert [p["planted_hit"] for p in out["proposed"]] == ["slow", "hide"]   # 仕込みは積んだときの入力が正本
    cur.execute("select payload from events where name='qa.synthetic'")
    ev = cur.fetchone()["payload"]
    assert ev["walks"] == 3 and ev["reached_exit"] == 2 and ev["events_logged"] == ["message.sent"] and ev["planted"] == ["hide", "slow"]
    cur.execute("select payload from events where name='opportunity.proposed' order by id")
    ops_ = [r["payload"] for r in cur.fetchall()]
    assert [o["theme"] for o in ops_] == [2, 1] and all(o["source"] == "synthetic_users" for o in ops_)
    cur.execute("select count(*) as n from artifacts where type='Opportunity'")
    assert cur.fetchone()["n"] == 2
    items = [i for i in inbox.items(cur) if i.get("task_kind") == "opportunity"]
    assert len(items) == 2 and items[0]["who"] == "po" and items[0]["answer"]["type"] == "ack"
    assert "合成の利用者" in comments[0][1] and "/api/bookings" in comments[0][1]
    # 同じ提案はもう一度出さない
    again = qa_line.record_synthetic(cur, gh, lambda *a: None, REPO, got["issue"], got["corr"], got["job_id"], res)
    assert all(p["opportunity_id"] is None for p in again["proposed"])
    # roadmap の測り方
    roadmap = yaml.safe_load((ROOT / "definitions" / "roadmap.yaml").read_text(encoding="utf-8"))
    item = next(i for ph in roadmap["phases"] for i in ph["items"] if i["id"] == "synthetic_users_loop")
    cur.execute(item["measure"]["db"]["sql"])
    assert list(cur.fetchone().values())[0] >= 1


def test_planted_proposal_is_labelled_and_leaves_the_real_key_free(cur):
    gh = FakeGH()
    res = {"mode": "synthetic", "env": "staging", "release_id": "r9", "model": "m", "synthetic": SY}
    first = qa_line.enqueue_synthetic(cur, gh, REPO, friction=PLANTED, week="2026-W45")
    planted = qa_line.record_synthetic(cur, gh, lambda *a: None, REPO, first["issue"], first["corr"], first["job_id"], res)
    items = [i for i in inbox.items(cur) if i.get("task_kind") == "opportunity"]
    assert len(items) == 2 and all("仕込み" in i["title"] and "わざと" in i["action"] for i in items)
    for i in items:
        assert inbox.resolve(cur, i["key"].split(":", 1)[1], "t")
    # 仕込みなしの回で同じ詰まりが出たら、本物の提案として出る
    cur.execute("update jobs set state='succeeded' where id=%s", (first["job_id"],))
    second = qa_line.enqueue_synthetic(cur, gh, REPO, friction={}, week="2026-W46")
    real = qa_line.record_synthetic(cur, gh, lambda *a: None, REPO, second["issue"], second["corr"], second["job_id"], res)
    assert all(p["opportunity_id"] is not None and p["planted_hit"] is None for p in real["proposed"])
    assert {p["opportunity_id"] for p in real["proposed"]}.isdisjoint(p["opportunity_id"] for p in planted["proposed"])
    items = [i for i in inbox.items(cur) if i.get("task_kind") == "opportunity"]
    assert len(items) == 2 and not any("仕込み" in i["title"] for i in items)


def test_opportunity_can_be_filed_to_the_planner_from_the_inbox(cur):
    import opportunity
    gh = FakeGH()
    res = {"mode": "synthetic", "env": "staging", "release_id": "r9", "model": "m", "synthetic": SY}
    first = qa_line.enqueue_synthetic(cur, gh, REPO, friction=PLANTED, week="2026-W45")
    qa_line.record_synthetic(cur, gh, lambda *a: None, REPO, first["issue"], first["corr"], first["job_id"], res)
    planted = [i for i in inbox.items(cur) if i.get("task_kind") == "opportunity"]
    assert all(i["answer"]["type"] == "ack" and [o["id"] for o in i["answer"]["options"]] == ["seen"] for i in planted)   # 仕込みは「見た」だけ
    assert opportunity.request_ticket(cur, planted[0]["key"].split(":", 1)[1], "t")[0] is False
    for i in planted:
        assert inbox.resolve(cur, i["key"].split(":", 1)[1], "t")
    cur.execute("update jobs set state='succeeded' where id=%s", (first["job_id"],))
    second = qa_line.enqueue_synthetic(cur, gh, REPO, friction={}, week="2026-W46")
    qa_line.record_synthetic(cur, gh, lambda *a: None, REPO, second["issue"], second["corr"], second["job_id"], res)
    items = [i for i in inbox.items(cur) if i.get("task_kind") == "opportunity"]
    assert [o["id"] for o in items[0]["answer"]["options"]] == ["file", "seen"]
    got, msg = inbox.answer(cur, items[0]["key"], "file", "console:owner", "console", note="まず 1 本")
    assert got == "answered" and "プランナー" in msg
    assert inbox.answer(cur, items[0]["key"], "file", "console:owner", "console")[0] == "already"
    # 次の周期に supervisor がプランナー宛ての Issue を作る（1 回だけ）
    gh2 = FakeGH()
    urls = opportunity.file_requested(cur, gh2)
    assert len(urls) == 1 and opportunity.file_requested(cur, gh2) == []
    (method, path, body), = gh2.calls
    assert method == "POST" and path == f"repos/{REPO}/issues" and body["labels"] == ["factory:plan"]
    assert "kpi_forecast" in body["body"] and "まず 1 本" in body["body"]
    cur.execute("select correlation_id, payload from events where name='opportunity.ticket_filed'")
    r = cur.fetchone()
    assert r["correlation_id"] == f"github:{REPO}#101" and r["payload"]["issue"] == urls[0]
    # 見ただけで閉じたものは起票しない
    got, _ = inbox.answer(cur, items[1]["key"], "seen", "console:owner", "console")
    assert got == "answered" and opportunity.file_requested(cur, gh2) == []


def test_supervisor_synthetic_users_weekly(cur, monkeypatch):
    gh = FakeGH()
    monkeypatch.setattr(supervisor, "gh", gh)
    monkeypatch.setattr(supervisor, "SYNTHETIC_USERS", [REPO])
    monkeypatch.setattr(supervisor, "DRY_RUN", False)
    monkeypatch.setattr(supervisor, "over_budget", lambda cur: None)
    monkeypatch.setattr(supervisor, "QA_DEFS", str(ROOT / "definitions" / "qa.yaml"))
    monkeypatch.setitem(supervisor.ENV, "SYNTH_FRICTION", "example")
    supervisor.synthetic_users_step(cur, datetime(2026, 11, 14, 5, 30, tzinfo=JST))   # 土曜は積まない
    cur.execute("select count(*) as n from jobs")
    assert cur.fetchone()["n"] == 0
    sun = datetime(2026, 11, 15, 5, 30, tzinfo=JST)
    supervisor.synthetic_users_step(cur, sun)
    supervisor.synthetic_users_step(cur, sun)   # 週に 1 回
    cur.execute("select payload from events where name='supervisor.synthetic_users_enqueued'")
    rows = cur.fetchall()
    assert len(rows) == 1 and rows[0]["payload"]["planted"] == ["hide", "slow"] and rows[0]["payload"]["week"] == "2026-W46"


def defect_rows(cur):
    rec = lambda fi, key, **kw: defect.record(cur, fi, key, "test", repo=REPO, **kw)  # noqa: E731
    rec("qa", f"{REPO}:cs-traces#2:https://x/1", viewpoint="authz", severity="s2", title="他人の予約が見える", reproduce=["開く", "id を変える"])
    rec("qa", f"{REPO}:cs-traces#2:https://x/2", viewpoint="authz", severity="s2", title="他人の予約が見える", reproduce=["開く"])
    rec("qa", f"{REPO}:explore:explore#title:戻ると消える:unfiled", viewpoint="flow", severity="s4", title="戻ると消える", reproduce=["戻る"])
    rec("pr", "https://github.com/o/app/pull/3:r1", viewpoint="naming", severity="minor")
    rec("ci", "https://github.com/o/app/pull/3:test", viewpoint="ci:test")


def test_quality_learn_step_focus_scenario_and_proposal(cur):
    defect_rows(cur)
    mon = datetime(2026, 11, 16, 8, tzinfo=JST)
    assert quality_learn.step(cur, REPO, datetime(2026, 11, 16, 6, tzinfo=JST)) is None   # 7 時より前は回さない
    got = quality_learn.step(cur, REPO, mon)
    assert got["facts"]["defects"] == 5 and got["facts"]["escape_prevention"] == 0.4 and got["facts"]["by_found_in"]["qa"] == 3
    assert got["focus"] == {"stories": ["cs-traces"], "viewpoints": ["authz", "flow"]}
    assert len(got["scenarios"]) == 1 and got["proposal"] == "authz"
    assert quality_learn.step(cur, REPO, mon) is None   # 週に 1 回
    cur.execute("select payload from events where name='qa.scenario_added'")
    sc = cur.fetchone()["payload"]
    assert sc["title"] == "他人の予約が見える" and sc["story_id"] == "cs-traces" and sc["n"] == 2   # s4 は足さない・同じ題が多い順
    # QA のジョブの入力に重点とシナリオが足される
    inp = qa_line.build_input(cur, REPO, "staging", [], "h")
    assert inp["focus"]["stories"] == ["cs-traces"] and inp["scenarios"][0]["id"] == sc["id"]
    # 基準の変更案を知見の Issue にし、委員会の列に入れる
    gh = FakeGH()
    filed = quality_learn.file_proposals(cur, gh, REPO, mon)
    assert len(filed) == 1 and quality_learn.file_proposals(cur, gh, REPO, mon) == []
    post = [c for c in gh.calls if c[0] == "POST"][0]
    assert post[2]["labels"] == ["factory:planned", "factory:learn"] and "quality_policy.yaml" in post[2]["body"] and "acts_on: process" in post[2]["body"]
    import committee
    q = committee.queue(cur, REPO)
    assert q and q[0]["corr"] == f"github:{REPO}#101"
    roadmap = yaml.safe_load((ROOT / "definitions" / "roadmap.yaml").read_text(encoding="utf-8"))
    item = next(i for ph in roadmap["phases"] for i in ph["items"] if i["id"] == "quality_learn")
    cur.execute(item["measure"]["db"]["sql"])
    assert list(cur.fetchone().values())[0] == 1


def test_quality_learn_without_enough_defects(cur):
    defect.record(cur, "pr", "https://github.com/o/app/pull/1:r", "test", repo=REPO, viewpoint="x")
    got = quality_learn.step(cur, REPO, datetime(2026, 11, 16, 8, tzinfo=JST))
    assert got["proposal"] is None and got["scenarios"] == [] and got["focus"] == {"stories": [], "viewpoints": ["x"]}


def test_supervisor_quality_learn_waits_for_committee_turn(cur, monkeypatch):
    defect_rows(cur)
    gh = FakeGH()
    monkeypatch.setattr(supervisor, "gh", gh)
    monkeypatch.setattr(supervisor, "REPOS", [REPO])
    monkeypatch.setattr(supervisor, "DRY_RUN", False)
    monkeypatch.setattr(supervisor, "knowledge_root_exists", lambda repo: True)
    monkeypatch.setattr(supervisor, "DEFECT_DEFS", str(ROOT / "definitions" / "defects.yaml"))
    cur.execute("insert into events(name, correlation_id, payload) values ('knowledge.case_filed', %s, %s)", (f"github:{REPO}#50", Jsonb({"incident": "x"})))
    mon = datetime(2026, 11, 16, 8, tzinfo=JST)
    supervisor.quality_learn_step(cur, mon)
    cur.execute("select count(*) as n from events where name='quality.proposal_filed'")
    assert cur.fetchone()["n"] == 0   # 流れている知見がある間は起票しない
    cur.execute("insert into events(name, correlation_id, payload) values ('pr.merged', %s, '{}')", (f"github:{REPO}#50",))
    supervisor.quality_learn_step(cur, mon)
    cur.execute("select count(*) as n from events where name='quality.proposal_filed'")
    assert cur.fetchone()["n"] == 1


def test_supervisor_synthetic_users_catches_up_after_the_hours(cur, monkeypatch):
    """日曜 5〜7 時を逃しても、その週のうちに起きた周回で 1 回積む（ADR 0050 の決定 3）。時刻より前は積まない"""
    gh = FakeGH()
    monkeypatch.setattr(supervisor, "gh", gh)
    monkeypatch.setattr(supervisor, "SYNTHETIC_USERS", [REPO])
    monkeypatch.setattr(supervisor, "DRY_RUN", False)
    monkeypatch.setattr(supervisor, "over_budget", lambda cur: None)
    monkeypatch.setattr(supervisor, "QA_DEFS", str(ROOT / "definitions" / "qa.yaml"))
    monkeypatch.setitem(supervisor.ENV, "SYNTH_FRICTION", "example")
    supervisor.synthetic_users_step(cur, datetime(2026, 11, 15, 4, 30, tzinfo=JST))   # 日曜の 5 時より前
    cur.execute("select count(*) as n from jobs")
    assert cur.fetchone()["n"] == 0
    late = datetime(2026, 11, 15, 21, tzinfo=JST)   # Mac が日曜の朝に眠っていて、夜に起きた
    supervisor.synthetic_users_step(cur, late)
    supervisor.synthetic_users_step(cur, late + timedelta(hours=1))
    cur.execute("select payload from events where name='supervisor.synthetic_users_enqueued'")
    rows = cur.fetchall()
    assert len(rows) == 1 and rows[0]["payload"]["week"] == "2026-W46"
