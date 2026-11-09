"""依頼者の好みを書き込む口（ADR 0040 の決定 6）: 窓口の API で見る・足す・直す・消す、会話から覚える。

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

    import desk
    import desk_answer
    import desk_api
    import desk_line
    import requester_prefs as rp


@pytest.fixture()
def cur():
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets, desk_conversations, desk_tokens, requester_preferences restart identity cascade")
    yield c
    conn.close()


def call(method, path, token=None, body=None, query=None):
    try:
        return desk_api.route(method, path, query or {}, body or {}, token, connect=lambda: psycopg.connect(URL, row_factory=dict_row, autocommit=True))
    except (desk.DeskError, rp.PrefError) as e:
        return e.status, {"error": str(e)}


def test_requester_adds_lists_updates_and_deletes(cur):
    tok = desk.create_token(cur, "cs-1", "requester", [REPO], "test")
    st, out = call("POST", "/api/desk/preferences", tok, {"product": REPO, "statement": "  数字は  表で ", "work": ["desk.converse"]})
    assert st == 201 and out["preference"]["id"] == "P-0001" and out["preference"]["statement"] == "数字は 表で"
    assert out["preference"]["confidence"] == "confirmed" and out["preference"]["created_by"] == "requester"
    st, again = call("POST", "/api/desk/preferences", tok, {"product": REPO, "statement": "数字は 表で"})
    assert st == 201 and again["preference"]["id"] == "P-0001"                     # 同じ文は増やさない
    assert call("POST", "/api/desk/preferences", tok, {"product": "other/x", "statement": "x"})[0] == 404
    assert call("POST", "/api/desk/preferences", tok, {"product": REPO, "statement": "x", "work": ["nope"]})[0] == 400
    assert call("POST", "/api/desk/preferences", tok, {"product": REPO, "statement": ""})[0] == 400
    st, lst = call("GET", "/api/desk/preferences", tok)
    assert [p["id"] for p in lst["preferences"]] == ["P-0001"]
    st, up = call("POST", "/api/desk/preferences/P-0001", tok, {"statement": "数字は税抜きの表で", "work": []})
    assert st == 200 and up["preference"]["statement"] == "数字は税抜きの表で" and up["preference"]["work"] == []
    assert call("POST", "/api/desk/preferences/P-0001/delete", tok)[1] == {"id": "P-0001", "deleted": True}
    cur.execute("select count(*) as n from requester_preferences")
    assert cur.fetchone()["n"] == 0                                                   # 行ごと消す
    cur.execute("select name, payload from events where name like 'requester_preference.%%' order by id")
    evs = cur.fetchall()
    assert [e["name"] for e in evs] == ["requester_preference.added", "requester_preference.confirmed",
                                        "requester_preference.updated", "requester_preference.deleted"]
    assert all("表" not in str(e["payload"]) for e in evs)                              # 中身は events に書かない
    assert call("POST", "/api/desk/preferences/P-0001", tok, {"statement": "x"})[0] == 404
    assert call("POST", "/api/desk/preferences/x", tok, {"statement": "x"})[0] == 400


def test_others_cannot_see_and_operator_only_fixes(cur):
    a = desk.create_token(cur, "cs-1", "requester", [REPO], "test")
    b = desk.create_token(cur, "cs-2", "requester", [REPO], "test")
    op = desk.create_token(cur, "owner", "operator", [], "test")
    call("POST", "/api/desk/preferences", a, {"product": REPO, "statement": "箇条書きで"})
    assert call("GET", "/api/desk/preferences", b)[1]["preferences"] == []
    assert call("POST", "/api/desk/preferences/P-0001", b, {"statement": "乗っ取り"})[0] == 404
    assert call("POST", "/api/desk/preferences/P-0001/delete", b)[0] == 404
    assert call("POST", "/api/desk/preferences", op, {"product": REPO, "statement": "x"})[0] == 403
    assert [p["id"] for p in call("GET", "/api/desk/preferences", op, query={"requester": "cs-1"})[1]["preferences"]] == ["P-0001"]
    assert call("POST", "/api/desk/preferences/P-0001/delete", op)[0] == 200


def test_operator_fix_keeps_hypothesis_but_requester_fix_confirms(cur):
    a = desk.create_token(cur, "cs-1", "requester", [REPO], "test")
    op = desk.create_token(cur, "owner", "operator", [], "test")
    cur.execute("insert into requester_preferences(product, requester, statement, created_by) values (%s,'cs-1','朝にまとめて','factory')", (REPO,))
    assert call("GET", "/api/desk/preferences", a)[1]["preferences"][0]["confidence"] == "hypothesis"   # 推し量りも一覧には出る
    assert call("POST", "/api/desk/preferences/P-0001", op, {"statement": "朝 9 時にまとめて"})[1]["preference"]["confidence"] == "hypothesis"
    assert call("POST", "/api/desk/preferences/P-0001", a, {})[1]["preference"]["confidence"] == "confirmed"   # 本人が認めた


def requester(cur, name="cs-1"):
    desk.create_token(cur, name, "requester", [REPO], "test")
    return {"name": name, "role": "requester", "products": [REPO]}


def test_consult_remembers_only_quoted_statements(cur):
    """相談役が拾った好みは、引用が依頼者の発言にそのままあるときだけ確認済みで残し、会話で知らせる"""
    c = desk.open_conversation(cur, requester(cur), "request", REPO, "予約の通知を足して。あと、今後は返事を箇条書きでお願いします")
    [jid] = desk_line.enqueue(cur, "tier-small", [REPO])
    cur.execute("update jobs set state='succeeded' where id=%s returning *", (jid,))
    job = cur.fetchone()
    res = {"ok": True, "outcome": "question", "round": 1, "max_rounds": 2, "cost_usd": 0.1,
           "question": {"question": "いつまでに？", "why": "w", "options": ["今週", "来週"], "recommended": 0},
           "stated_preferences": [
               {"quote": "今後は返事を箇条書きでお願いします", "statement": "返事は箇条書きにする", "work": ["desk.converse"]},
               {"quote": "数字は税抜きで", "statement": "数字は税抜き", "work": []},                  # 言っていない
               {"quote": "返事を箇条書き", "statement": "x", "work": ["no.such"]}]}                # 業務の語彙の外
    desk_line.record(cur, job, res, "succeeded", None)
    cur.execute("select id, statement, work, confidence, evidence, created_by from requester_preferences")
    [p] = cur.fetchall()
    assert p["statement"] == "返事は箇条書きにする" and p["work"] == ["desk.converse"] and p["confidence"] == "confirmed"
    assert p["created_by"] == "requester" and len(p["evidence"]) == 1
    msgs = desk_line.messages(cur, c["id"])
    note = [m for m in msgs if m.get("kind") == "preference"]
    assert len(note) == 1 and "P-0001" in note[0]["body"] and note[0]["ids"] == ["P-0001"]
    assert msgs[-1]["kind"] == "question"                                            # 本題の返事が最後
    cur.execute("select state from desk_conversations where id=%s", (c["id"],))
    assert cur.fetchone()["state"] == "waiting_requester"
    # 次の回の相談に、覚えた好みが渡る
    assert [x["id"] for x in rp.for_job(cur, REPO, "cs-1", desk_line.CONSULT_WORK)] == ["P-0001"]


def test_answer_remembers_too(cur):
    c = desk.open_conversation(cur, requester(cur), "inquiry", REPO, "今週の進み具合は？ 数字はいつも表でください")
    [jid] = desk_answer.enqueue(cur, "tier-small", [REPO])
    cur.execute("update jobs set state='succeeded' where id=%s returning *", (jid,))
    job = cur.fetchone()
    res = {"kind": "desk_answer", "ok": True, "outcome": "answer", "stance": "planner", "cost_usd": 0.1, "answer": "| 済 | 3 |",
           "evidence": [{"ref": "activity:7d", "what": "w"}], "unknowns": [], "switch_to_request": False, "questions_asked": 0,
           "stated_preferences": [{"quote": "数字はいつも表でください", "statement": "数字は表で出す", "work": []}]}
    desk_answer.record(cur, job, res, "succeeded", None)
    assert [p["id"] for p in rp.list_for(cur, {"name": "cs-1", "role": "requester", "products": [REPO]})] == ["P-0001"]
    assert desk_line.messages(cur, c["id"])[-1]["kind"] == "answer"
