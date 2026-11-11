"""窓口の問い合わせに答える Answer ライン（ADR 0030 の順番 4）: 回答役のジョブを積む・材料（記録の id つき）・結果を会話に書く・続けて聞く、窓口の枠。

空の Postgres に postgres/init/*.sql を当て、TEST_DATABASE_URL を向けて動かす（なければ飛ばす）。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL がない")
REPO = "guccyon/rally"

if URL:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb

    import desk
    import desk_answer
    import desk_line
    import line
    import stoppage


@pytest.fixture()
def cur():
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets, desk_conversations, desk_tokens restart identity cascade")
    yield c
    conn.close()


def requester(cur, name="cs-1"):
    desk.create_token(cur, name, "requester", [REPO], "test")
    return {"name": name, "role": "requester", "products": [REPO]}


def run(cur, jid, result, state="succeeded", err=None):
    cur.execute("update jobs set state=%s where id=%s returning *", (state, jid))
    job = cur.fetchone()
    desk_answer.record(cur, job, result, state, err)
    return job


def job_input(cur, jid):
    cur.execute("select payload from events where name='job.enqueued' and job_id=%s", (jid,))
    return cur.fetchone()["payload"]["input"]


QUESTION = {"kind": "desk_answer", "ok": True, "outcome": "question", "round": 1, "max_rounds": 1, "cost_usd": 0.1,
            "question": {"question": "どの週ですか", "why": "決まると数えられる", "options": ["今週", "先週"], "recommended": 0}}
ANSWER = {"kind": "desk_answer", "ok": True, "outcome": "answer", "stance": "planner", "cost_usd": 0.3,
          "answer": "#12 は CI の失敗で止まっています。", "evidence": [{"ref": "job:1", "what": "実装のジョブが落ちた"}],
          "unknowns": ["いつ直るか"], "switch_to_request": False, "dropped_refs": ["job:999"], "questions_asked": 1, "model": "m"}


def test_inquiry_goes_to_answer_not_consult(cur):
    c = desk.open_conversation(cur, requester(cur), "inquiry", REPO, "今週どこまで進んだ？")
    assert desk_line.enqueue(cur, "tier-small", [REPO]) == []           # 相談役は依頼だけ
    [jid] = desk_answer.enqueue(cur, "tier-small", [REPO])
    assert desk_answer.enqueue(cur, "tier-small", [REPO]) == []         # 動いている間は積まない
    assert line.claim_line(cur) is None                                 # ラインの枠は取らない
    j = line.claim_desk(cur)
    assert j["id"] == jid and j["kind"] == "desk_answer" and j["issue_number"] == -c["id"]
    assert j["correlation_id"] == desk.corr(c["id"])


def test_request_is_not_answered(cur):
    desk.open_conversation(cur, requester(cur), "request", REPO, "予約まわりの通知を先に進めて")
    assert desk_answer.enqueue(cur, "tier-small", [REPO]) == []


def test_input_carries_records_with_ids(cur):
    who = requester(cur)
    cur.execute("insert into tickets(source, source_ref, repo, title, state, priority) values "
                "('github',%s,%s,'予約の通知','active',70), ('github',%s,%s,'済んだ','closed',50), ('github','other/x#1','other/x','ほか','active',50)",
                (f"{REPO}#12", REPO, f"{REPO}#9", REPO))
    cur.execute("insert into jobs(correlation_id, repo, issue_number, kind, state, error) values (%s,%s,12,'implement','failed','exit=1') returning id",
                (f"github:{REPO}#12", REPO))
    job_id = cur.fetchone()["id"]
    stoppage.open_stoppage(cur, "ci:rally#12", "ticket", "CI が落ちた", correlation_id=f"github:{REPO}#12", quiet=True)
    c = desk.open_conversation(cur, who, "inquiry", REPO, "#12 はなぜ止まっている？ other/x#1 と guccyon/rally#12 も")
    [jid] = desk_answer.enqueue(cur, "tier-small", [REPO])
    inp = job_input(cur, jid)
    assert inp["conversation_id"] == c["id"] and inp["questions_asked"] == 0 and inp["max_rounds"] == 1 and not inp["force_answer"]
    assert set(inp["stances"]) == {"planner", "factory_head"}
    r = inp["records"]
    assert [t["id"] for t in r["tickets"]] == [f"ticket:{REPO}#12"]                 # 製品のものだけ
    assert [t["id"] for t in r["recently_closed"]] == [f"ticket:{REPO}#9"]
    assert r["line"]["id"] == "line:status" and r["activity_7d"]["id"] == "activity:7d"
    assert [s["id"] for s in r["open_stoppages"]] == ["stoppage:ci:rally#12"]
    [ref] = r["referenced"]                                                         # 製品の外は見ない・同じものは 1 つ
    assert ref["ref"] == f"{REPO}#12" and ref["ticket"]["id"] == f"ticket:{REPO}#12"
    assert ref["jobs"][0]["id"] == f"job:{job_id}" and ref["jobs"][0]["error"] == "exit=1"
    assert ref["open_stoppages"][0]["id"] == "stoppage:ci:rally#12"
    assert any(e["name"] == "stoppage.opened" and e["id"].startswith("event:") for e in ref["events"])


def test_input_carries_roadmap_values_from_the_daily_report(cur):
    """ロードマップの各項目の現在値と項目の数は、最新の建設日報（report.daily）から渡す（#321）"""
    who = requester(cur)
    desk.open_conversation(cur, who, "inquiry", REPO, "P5a の残りは？")
    [jid] = desk_answer.enqueue(cur, "tier-small", [REPO])
    assert job_input(cur, jid)["records"]["roadmap"] is None                     # 日報がなければ None
    cur.execute("update jobs set state='succeeded' where id=%s", (jid,))
    for day, v in (("2026-11-09", "0 / 3"), ("2026-11-10", "1 / 3")):
        cur.execute("insert into events(name, payload, dedupe_key) values ('report.daily', %s, %s)",
                    (Jsonb({"date": day, "overall": 0.8, "phases": [{"id": "P5a", "ja": "実行環境と QA", "progress": 0.6, "item_count": 2,
                                                                      "items": [{"id": "mac_runner", "ja": "Mac ランナー", "progress": 1.0, "value": "5 / 5"},
                                                                                {"id": "nightly_explore", "ja": "夜間の探索", "progress": 0.333, "value": v}]}]}),
                     f"report.daily:{day}"))
    c2 = desk.open_conversation(cur, who, "inquiry", REPO, "P5a の残りは？（2 回目）")
    jid2 = [j for j in desk_answer.enqueue(cur, "tier-small", [REPO])][0]
    rm = job_input(cur, jid2)["records"]["roadmap"]
    assert c2 and rm["id"] == "roadmap:2026-11-10" and rm["phases"][0]["item_count"] == 2
    assert [i["value"] for i in rm["phases"][0]["items"]] == ["5 / 5", "1 / 3"]


def test_question_then_answer_then_follow_up(cur):
    who = requester(cur)
    c = desk.open_conversation(cur, who, "inquiry", REPO, "進み具合は？")
    [jid] = desk_answer.enqueue(cur, "tier-small", [REPO])
    run(cur, jid, QUESTION)
    out = desk.read_conversation(cur, who, c["id"])
    assert out["conversation"]["state"] == "waiting_requester"
    assert out["messages"][-1]["role"] == "answer" and out["messages"][-1]["kind"] == "question" and "（1/1）" in out["messages"][-1]["body"]
    assert desk_answer.enqueue(cur, "tier-small", [REPO]) == []       # 返事を待つ
    desk.post_message(cur, who, c["id"], "今週")
    [jid2] = desk_answer.enqueue(cur, "tier-small", [REPO])
    inp = job_input(cur, jid2)
    assert inp["questions_asked"] == 1 and inp["force_answer"] is True     # 問い返しは使い切った
    run(cur, jid2, ANSWER)
    out = desk.read_conversation(cur, who, c["id"])
    assert out["conversation"]["state"] == "answered"
    m = out["messages"][-1]
    assert m["kind"] == "answer" and m["stance"] == "planner" and m["evidence"] == ANSWER["evidence"] and m["unknowns"] == ["いつ直るか"]
    assert "計画の立場" in m["body"] and "job:1" in m["body"] and "いつ直るか" in m["body"]
    cur.execute("select count(*) as n from events where name='desk.answered' and correlation_id=%s", (desk.corr(c["id"]),))
    assert cur.fetchone()["n"] == 1
    cur.execute("select cost_usd from desk_conversations where id=%s", (c["id"],))
    assert float(cur.fetchone()["cost_usd"]) == pytest.approx(0.4)
    # 続けて聞くと、もう一度回答役が動く（answered → open）
    desk.post_message(cur, who, c["id"], "では来週は？")
    [jid3] = desk_answer.enqueue(cur, "tier-small", [REPO])
    assert [m["role"] for m in job_input(cur, jid3)["messages"]] == ["requester", "answer", "requester", "answer", "requester"]


def test_switch_to_request_is_suggested(cur):
    who = requester(cur)
    c = desk.open_conversation(cur, who, "inquiry", REPO, "#12 を先にやって")
    [jid] = desk_answer.enqueue(cur, "tier-small", [REPO])
    run(cur, jid, {**ANSWER, "switch_to_request": True})
    m = desk.read_conversation(cur, who, c["id"])["messages"][-1]
    assert m["switch_to_request"] is True and "kind=request" in m["body"]


def test_failures_are_told_in_the_conversation(cur):
    who = requester(cur)
    c = desk.open_conversation(cur, who, "inquiry", REPO, "進み具合は？")
    [jid] = desk_answer.enqueue(cur, "tier-small", [REPO])
    run(cur, jid, {"kind": "desk_answer", "ok": True, "outcome": "failed", "errors": ["根拠が 1 つもない"]})
    out = desk.read_conversation(cur, who, c["id"])
    assert out["conversation"]["state"] == "waiting_requester" and "根拠が 1 つもない" in out["messages"][-1]["body"]
    desk.post_message(cur, who, c["id"], "今週の分だけで")
    [jid2] = desk_answer.enqueue(cur, "tier-small", [REPO])
    run(cur, jid2, None, state="failed", err="timeout")
    out = desk.read_conversation(cur, who, c["id"])
    assert out["messages"][-1]["kind"] == "error" and "timeout" in out["messages"][-1]["body"]


def test_not_a_factory_repo_and_cost_limit(cur):
    who = requester(cur)
    c = desk.open_conversation(cur, who, "inquiry", REPO, "進み具合は？")
    assert desk_answer.enqueue(cur, "tier-small", ["guccyon/other"]) == []
    assert desk.read_conversation(cur, who, c["id"])["messages"][-1]["kind"] == "error"
    desk.post_message(cur, who, c["id"], "rally です")
    cur.execute("update desk_conversations set cost_usd=5 where id=%s", (c["id"],))
    assert desk_answer.enqueue(cur, "tier-small", [REPO]) == []
    assert desk.read_conversation(cur, who, c["id"])["messages"][-1]["kind"] == "limit"
