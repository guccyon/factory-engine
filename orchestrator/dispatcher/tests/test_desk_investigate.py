"""窓口の依頼を工場の中での一次調査に流す（desk_investigate.py、ADR 0048 の順番 4）。

受付 → 振り分け（相談役の route、path: investigate）→ 確認を飛ばして running → 調査役のジョブを governance の枠に積む（1 日の上限）
→ 結果を成果物に残して提出 → 受取確認。DB が要るものは TEST_DATABASE_URL の Postgres で動かす。
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("NOTIFIER", "log")
URL = os.environ.get("TEST_DATABASE_URL")
REPO = "guccyon/rally"
ROOT = Path(__file__).resolve().parents[3]
needs_db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL がない")
DEFS = yaml.safe_load((ROOT / "definitions" / "investigate.yaml").read_text(encoding="utf-8"))

import desk  # noqa: E402
import desk_investigate as di  # noqa: E402
import line  # noqa: E402


def test_limits_match_definition():
    lim = DEFS["limits"]
    assert di.PER_CASE_TURNS == lim["per_case_turns"] and di.PER_CASE_USD == lim["per_case_usd"]
    assert di.PER_DAY_USD == lim["per_day_usd"] and di.FOLLOW_UPS == lim["follow_ups"] and di.MODEL_TIER == lim["model_tier"]
    assert set(di.OUTCOME_JA) == set(DEFS["outcomes"]) and set(di.FIX_JA) == set(DEFS["fix_targets"]) - {"none"}


def test_path_is_declared_everywhere():
    wf = yaml.safe_load((ROOT / "definitions" / "workflow.yaml").read_text(encoding="utf-8"))
    dk = yaml.safe_load((ROOT / "definitions" / "desk.yaml").read_text(encoding="utf-8"))
    assert "investigate" in desk.PATHS
    assert any("investigate" in str(v) for v in wf.values())
    assert dk["loop"]["targets"]["route"]["investigate"] and dk["loop"]["targets"]["deliver"]["investigate"]
    assert di.KIND in line.GOVERNANCE_KINDS and di.KIND not in line.DESK_KINDS


def test_input_window_and_deny():
    c = {"id": 3, "product": "owner/product", "opened_by": "cs-1"}
    inp = di.investigate_input(c, {"question": "なぜ止まった", "window_days": 2, "refs": ["job:1"]}, "abc")
    s, e = (datetime.fromisoformat(x) for x in inp["window"])
    assert timedelta(days=2) - timedelta(seconds=5) < e - s < timedelta(days=2) + timedelta(seconds=5)
    assert inp["deny"] == ["cs-1", "owner", "owner/product", "product"]
    assert inp["limits"] == {"turns": di.PER_CASE_TURNS, "usd": di.PER_CASE_USD} and inp["engine_ref"] == "abc"
    one = di.investigate_input(c, {"question": "q"}, None)
    s, e = (datetime.fromisoformat(x) for x in one["window"])
    assert e - s >= timedelta(days=1) - timedelta(seconds=5)   # 時間帯の指定がなければ直近 1 日


def test_report_text():
    t = di.report_text({"outcome": "unidentified", "report": "記録が足りません", "fix_target": "none", "handoff": {"facts": []},
                        "money_loss": {"suspected": True}})
    assert "特定できません" in t and "記録が足りません" in t and "外から" in t and "事故" in t
    t = di.report_text({"outcome": "identified", "fix_target": "operation", "operation_proposal": "流量を 2 に"})
    assert "運用の値" in t and "流量を 2 に" in t


if URL:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb

    import desk_api
    import desk_line
    import inbox


@pytest.fixture()
def cur():
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets, desk_conversations, desk_tokens restart identity cascade")
    yield c
    conn.close()


def call(method, path, token=None, body=None):
    try:
        return desk_api.route(method, path, {}, body or {}, token, connect=lambda: psycopg.connect(URL, row_factory=dict_row, autocommit=True))
    except desk.DeskError as e:
        return e.status, {"error": str(e)}


def open_request(cur, body="なぜ昨日はマージが進まなかったのか調べて"):
    tok = desk.create_token(cur, "cs-1", "requester", [REPO], "test")
    st, out = call("POST", "/api/desk/conversations", tok, {"kind": "request", "product": REPO, "body": body})
    return tok, out["conversation"]["id"]


def consult_route(cur, route):
    jid = desk_line.enqueue(cur, "tier-small", [REPO])[0]
    cur.execute("update jobs set state='succeeded' where id=%s returning *", (jid,))
    desk_line.record(cur, cur.fetchone(), {"ok": True, "outcome": "route", "route": route, "confidence": 0.8, "cost_usd": 0.2}, "succeeded", None)


def state(cur, cid):
    cur.execute("select * from desk_conversations where id=%s", (cid,))
    return cur.fetchone()


def names(cur, cid):
    cur.execute("select name from events where correlation_id=%s order by id", (desk.corr(cid),))
    return [r["name"] for r in cur.fetchall()]


def delivered(cur, cid):
    cur.execute("select payload->>'body' as b from events where correlation_id=%s and name='desk.message' and payload->>'kind'='delivered' "
                "order by id desc limit 1", (desk.corr(cid),))
    return cur.fetchone()["b"]


def finish(cur, jid, result, st="succeeded", cost=None):
    res = {**result, **({"cost_usd": cost} if cost is not None else {})}
    cur.execute("update jobs set state=%s, finished_at=now(), result=%s where id=%s returning *", (st, Jsonb(res), jid))
    di.record(cur, cur.fetchone(), res, st, None if st == "succeeded" else "exit 1")


GOOD = {"ok": True, "outcome": "identified", "report": "検査の待ちで止まっていました。", "confidence": "medium", "fix_target": "engine",
        "engine_issue": {"title": "検査の待ちが長いと続きが積まれない", "body": "…"}, "issue_errors": [], "issue_held": False,
        "money_loss": {"suspected": False}, "handoff": {"facts": [{"ref": "job:1"}], "stop_reason": "needs_code", "not_looked_at": []},
        "handoff_errors": [], "num_turns": 10, "model": "m"}


@needs_db
def test_request_to_investigation_to_receipt(cur):
    tok, cid = open_request(cur)
    consult_route(cur, {"path": "investigate", "question": "昨日マージが進まなかった理由", "refs": ["ticket:12"], "window_days": 1,
                        "why": "工場の記録を見れば分かる"})
    c = state(cur, cid)
    assert (c["state"], c["path"]) == ("running", "investigate")              # 読むだけなので確認を飛ばす
    assert inbox.find(cur, f"task:desk_confirm:{cid}:1") is None
    jids = di.enqueue(cur, [REPO], engine_ref="abc")
    assert len(jids) == 1 and di.enqueue(cur, [REPO]) == []                    # 1 回に 1 本
    cur.execute("select * from jobs where id=%s", (jids[0],))
    j = cur.fetchone()
    assert (j["kind"], j["issue_number"], j["model_tier"]) == ("investigate", -cid, "tier-judge")
    cur.execute("select payload from events where name='job.enqueued' and job_id=%s", (jids[0],))
    inp = cur.fetchone()["payload"]["input"]
    assert inp["question"] == "昨日マージが進まなかった理由" and inp["refs"] == ["ticket:12"] and REPO in inp["deny"]
    assert line.claim_desk(cur) is None                                       # 窓口の枠では取り出さない
    assert line.claim_governance(cur)["id"] == jids[0]                        # governance の枠で取り出す
    finish(cur, jids[0], GOOD, cost=1.4)
    c = state(cur, cid)
    assert c["state"] == "delivered" and abs(float(c["cost_usd"]) - 1.6) < 1e-6
    cur.execute("select payload from artifacts where type='InvestigationResult'")
    a = cur.fetchone()["payload"]
    assert a["outcome"] == "identified" and a["conversation_id"] == cid and a["round"] == 1
    assert inbox.find(cur, f"task:desk_receipt:{cid}:1")
    assert "原因が分かりました" in delivered(cur, cid)
    assert call("POST", f"/api/desk/conversations/{cid}/messages", tok, {"answer": "accept"})[0] == 200
    ns = names(cur, cid)
    for a_, b_ in (("desk.routed", "desk.confirmed_run"), ("desk.confirmed_run", "investigate.enqueued"),
                   ("investigate.enqueued", "investigate.finished"), ("investigate.finished", "desk.delivered")):
        assert ns.index(a_) < ns.index(b_), (a_, b_)


@needs_db
def test_route_without_question_asks_requester(cur):
    _, cid = open_request(cur)
    consult_route(cur, {"path": "investigate", "question": " ", "why": ""})
    assert state(cur, cid)["state"] == "waiting_requester" and "desk.routed" not in names(cur, cid)


@needs_db
def test_daily_cap_defers(cur):
    cur.execute("insert into jobs(correlation_id, repo, issue_number, attempt, enqueued_by, model_tier, kind, state, finished_at, result) "
                "values ('x', %s, -999, 1, 'desk', 'tier-judge', 'investigate', 'succeeded', now(), %s)", (REPO, Jsonb({"cost_usd": di.PER_DAY_USD})))
    _, cid = open_request(cur)
    consult_route(cur, {"path": "investigate", "question": "q", "why": ""})
    assert di.enqueue(cur, [REPO]) == [] and di.enqueue(cur, [REPO]) == []
    assert names(cur, cid).count("investigate.deferred") == 1                  # 知らせるのは 1 日 1 回
    tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
    assert len(di.enqueue(cur, [REPO], now=tomorrow)) == 1                     # 次の日に積む


@needs_db
def test_not_a_factory_repo(cur):
    _, cid = open_request(cur)
    consult_route(cur, {"path": "investigate", "question": "q", "why": ""})
    assert di.enqueue(cur, ["other/repo"]) == [] and "investigate.skipped" in names(cur, cid)


@needs_db
def test_budget_stop_and_failure_are_delivered(cur):
    tok, cid = open_request(cur)
    consult_route(cur, {"path": "investigate", "question": "q", "why": ""})
    jid = di.enqueue(cur, [REPO])[0]
    hand = {"facts": [{"ref": "job:1"}], "stop_reason": "budget", "not_looked_at": ["ログの前半"]}
    finish(cur, jid, {**GOOD, "outcome": "unidentified", "fix_target": "none", "handoff": hand}, cost=3.0)
    assert state(cur, cid)["state"] == "delivered" and "desk.cap_hit" in names(cur, cid)
    assert "ログの前半" in delivered(cur, cid)
    # 続きを頼む → 2 回目の調査。失敗も提出する
    assert call("POST", f"/api/desk/conversations/{cid}/messages", tok, {"answer": "more", "body": "続きを見て"})[0] == 200
    consult_route(cur, {"path": "investigate", "question": "続き", "why": ""})
    jid2 = di.enqueue(cur, [REPO])[0]
    finish(cur, jid2, {"ok": False, "exit": 1}, st="failed")
    assert state(cur, cid)["state"] == "delivered" and "investigate.failed" in names(cur, cid)


@needs_db
def test_follow_up_limit(cur):
    tok, cid = open_request(cur)
    for i in range(1 + di.FOLLOW_UPS):
        consult_route(cur, {"path": "investigate", "question": f"q{i}", "why": ""})
        finish(cur, di.enqueue(cur, [REPO])[0], GOOD, cost=0.1)
        assert call("POST", f"/api/desk/conversations/{cid}/messages", tok, {"answer": "more", "body": "もっと"})[0] == 200
    consult_route(cur, {"path": "investigate", "question": "q", "why": ""})
    assert state(cur, cid)["state"] == "waiting_requester" and names(cur, cid).count("desk.routed") == 1 + di.FOLLOW_UPS


@needs_db
def test_withdrawn_conversation_is_not_delivered(cur):
    tok, cid = open_request(cur)
    consult_route(cur, {"path": "investigate", "question": "q", "why": ""})
    jid = di.enqueue(cur, [REPO])[0]
    cur.execute("update desk_conversations set state='closed' where id=%s", (cid,))
    finish(cur, jid, GOOD, cost=0.5)
    assert "investigate.finished" not in names(cur, cid)


def test_labels_and_stops_match_definition():
    assert di.ISSUE_LABEL == DEFS["labels"]["engine_issue"] and di.HANDOFF_LABEL == DEFS["labels"]["handoff"]
    assert set(di.STOP_JA) == set(DEFS["stop_reasons"])


def test_leaks():
    deny = ["owner/product", "product", "owner", "cs-1"]
    assert di.leaks("検査の待ちが上限の 3 倍を超えると続きが積まれない。ADR 0038 と関係する", deny) == []
    for bad in ("Product の停止", "https://example.com を見た", "#1031 が止まった", "a@b.example に送った", "job:12 で"):
        assert di.leaks(bad, deny), bad


def test_handoff_summary_has_no_free_text():
    p = {"id": "INV-2026-0001", "stop_reason": "needs_code", "errors": [],
         "dossier": {"question": "owner/product の #1031 はなぜ", "facts": [{"ref": "job:1", "what": "顧客の話"}], "hypotheses": [], "not_looked_at": ["x"]}}
    t = di.handoff_summary(p, "abc")
    assert "INV-2026-0001" in t and "needs_code" in t and "見た事実: 1 件" in t
    assert di.leaks(t.replace("`factoryctl investigation show INV-2026-0001`", ""), ["owner/product", "product", "owner"]) == []


class FakeGH:
    def __init__(self, open_issues=()):
        self.calls, self.open = [], list(open_issues)

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET":
            return self.open
        return {"html_url": f"https://github.com/x/engine/issues/{len(self.calls)}"}


@needs_db
def test_engine_issue_is_proposed_and_filed_once(cur):
    _, cid = open_request(cur)
    consult_route(cur, {"path": "investigate", "question": "q", "why": ""})
    finish(cur, di.enqueue(cur, [REPO])[0], GOOD, cost=1.0)
    assert "investigate.issue_proposed" in names(cur, cid) and "investigate.handoff" not in names(cur, cid)
    gh = FakeGH()
    assert len(di.file_issues(cur, gh, "x/engine", "abc")) == 1 and di.file_issues(cur, gh, "x/engine") == []
    post = [c for c in gh.calls if c[0] == "POST"]
    assert len(post) == 1 and post[0][2]["labels"] == [di.ISSUE_LABEL] and post[0][2]["title"] == GOOD["engine_issue"]["title"]


@needs_db
def test_same_open_issue_gets_a_comment(cur):
    _, cid = open_request(cur)
    consult_route(cur, {"path": "investigate", "question": "q", "why": ""})
    finish(cur, di.enqueue(cur, [REPO])[0], GOOD, cost=1.0)
    gh = FakeGH([{"title": GOOD["engine_issue"]["title"], "number": 9, "html_url": "u9"}])
    assert di.file_issues(cur, gh, "x/engine") == ["u9"]
    assert gh.calls[-1][1] == "repos/x/engine/issues/9/comments"


@needs_db
def test_dirty_issue_is_held_for_builder(cur):
    _, cid = open_request(cur)
    consult_route(cur, {"path": "investigate", "question": "q", "why": ""})
    dirty = {**GOOD, "engine_issue": {"title": "rally の停止", "body": "x"}}   # 調査役の検査をすり抜けた名前
    finish(cur, di.enqueue(cur, [REPO])[0], dirty, cost=1.0)
    assert "investigate.issue_held" in names(cur, cid) and "investigate.issue_proposed" not in names(cur, cid)
    it = inbox.find(cur, f"task:investigate_issue_held:{cid}:1")
    assert it["who"] == "builder" and "rally" not in it["title"]
    assert di.file_issues(cur, FakeGH(), "x/engine") == []


@needs_db
def test_unidentified_gets_inv_id_and_handoff_issue(cur):
    _, cid = open_request(cur)
    consult_route(cur, {"path": "investigate", "question": "なぜ止まった", "why": ""})
    hand = {"question": "なぜ止まった", "facts": [{"ref": "job:1", "what": "失敗", "how": "jobs"}], "hypotheses": [], "ruled_out": [],
            "not_looked_at": ["Engine のソース"], "stop_reason": "needs_code"}
    res = {**GOOD, "outcome": "unidentified", "fix_target": "none", "engine_issue": None, "handoff": hand,
           "money_loss": {"suspected": True, "note": "同じ検査を 3 回"}}
    finish(cur, di.enqueue(cur, [REPO])[0], res, cost=2.0)
    hs = di.handoffs(cur)
    assert len(hs) == 1 and hs[0]["id"].startswith("INV-") and hs[0]["id"].endswith("-0001")
    assert di.get_handoff(cur, hs[0]["id"])["dossier"]["question"] == "なぜ止まった"
    assert hs[0]["id"] in delivered(cur, cid)
    cur.execute("select payload from events where name='fincident.opened'")
    case = cur.fetchone()["payload"]
    assert case["by"] == "investigate" and case["facts"]["conversation_id"] == cid
    gh = FakeGH()
    assert len(di.file_issues(cur, gh, "x/engine", "abc")) == 1
    body = gh.calls[-1][2]
    assert body["labels"] == [di.HANDOFF_LABEL] and "なぜ止まった" not in body["body"] and "job:1" not in body["body"]
    assert di.handoffs(cur)[0]["issue"] and di.file_issues(cur, gh, "x/engine") == []
