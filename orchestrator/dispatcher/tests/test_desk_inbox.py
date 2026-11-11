"""窓口から受信トレイを読み、答える（ADR 0034 の順番 2）。TEST_DATABASE_URL の Postgres で確かめる（なければ飛ばす）。

11-07: 受信トレイにボタンと一言の欄があるだけでは、判断の材料が足りないときに聞き返せない。手元の Claude が窓口の MCP で件を読み、
PR の差分などを調べて本人の問いに答え、本人の答えを返せるようにする。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL がない")

if URL:
    os.environ.setdefault("DATABASE_URL", URL)
    os.environ.setdefault("GH_TOKEN", "x")
    os.environ.setdefault("NOTIFIER", "log")
    import psycopg
    from psycopg.rows import dict_row

    import desk
    import desk_api
    import inbox
    import stoppage
    from test_inbox_judgment import DEFECT, ESC, PR, REPO, judge


@pytest.fixture()
def cur():
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets, desk_conversations, desk_tokens restart identity cascade")
    yield c
    conn.close()


def call(method, path, token=None, body=None, query=None):
    try:
        return desk_api.route(method, path, query or {}, body or {}, token, connect=lambda: psycopg.connect(URL, row_factory=dict_row, autocommit=True))
    except desk.DeskError as e:
        return e.status, {"error": str(e)}


def path(key, tail=""):
    from urllib.parse import quote
    return "/api/desk/inbox/" + quote(key, safe="") + tail


def two_questions(cur):
    judge(cur, {"いつから通話を始められるか": ESC,
                "開始前の文言": {**ESC, "recommend": "replace", "replacement": "2 文にする", "interpretation": "基準は 2 文"}}, [DEFECT])
    return {i["title"].split(": ", 1)[1]: i["key"] for i in inbox.items(cur)}


def test_operator_lists_reads_detail_and_answers_once(cur):
    tok = desk.create_token(cur, "owner", "operator", [], "test")
    keys = two_questions(cur)
    st, out = call("GET", "/api/desk/inbox", tok)
    assert st == 200 and out["count"] == 3 and {i["kind"] for i in out["items"]} == {"judgment"}
    it = next(i for i in out["items"] if i["key"] == keys["開始前の文言"])
    assert it["product"] == REPO and it["url"] == PR and it["answerable"] and it["recommended"] == "おすすめの案に直す"
    assert "detail" not in it   # 一覧は短く。中身は inbox_read で

    st, one = call("GET", path(keys["開始前の文言"]), tok)
    assert st == 200
    d = {x["label"]: x["text"] for x in one["detail"]}
    assert d["今の案"] == "1 文" and d["おすすめの案"] == "2 文にする" and "基準は 2 文" in d["見立て"]
    a = one["answer"]
    assert [o["id"] for o in a["options"]] == ["keep", "replace"] and a["recommended"] == "replace" and a["extra"][0]["note_required"]
    assert "target" not in a and a["effect"]

    assert call("POST", path(keys["開始前の文言"], "/answer"), tok, {"option": "nope"})[0] == 400
    assert call("POST", path(keys["開始前の文言"], "/answer"), tok, {"option": "change"})[0] == 400   # 別の答えには一言が要る
    st, res = call("POST", path(keys["開始前の文言"], "/answer"), tok, {"option": "replace", "raw": "judge の案で"})
    assert st == 200 and res["result"] == "answered"
    cur.execute("select payload from events where name='inbox.answered'")
    p = cur.fetchone()["payload"]
    assert p["via"] == "mcp" and p["by"] == "desk:owner" and p["raw"] == "judge の案で" and p["explicit"] is True

    # 後から別の入口で答えても先勝ち。窓口からの 2 度目も、誰が何と答えたかを返す
    assert inbox.answer(cur, keys["開始前の文言"], "keep", "console", "console")[0] == "already"
    st, again = call("POST", path(keys["開始前の文言"], "/answer"), tok, {"option": "keep"})
    assert st == 409 and "desk:owner" in again["error"] and "Claude（窓口）" in again["error"]
    assert call("GET", "/api/desk/inbox", tok)[1]["count"] == 2   # 決まった判断は一覧から消える


def test_tokens_see_only_items_they_may_answer(cur):
    owner = desk.create_token(cur, "owner", "operator", [], "test")
    other_op = desk.create_token(cur, "op-x", "operator", ["x/y"], "test")
    req = desk.create_token(cur, "cs-1", "requester", [REPO], "test")
    req_other = desk.create_token(cur, "cs-2", "requester", ["x/y"], "test")
    keys = two_questions(cur)
    stoppage.open_stoppage(cur, "drain:line", "human", "ラインを一時停止中", waiting_for="再開", detected_by="test", quiet=True)
    inbox.request(cur, "swap:o-r-1", "swap_rolled_back", "requester", "更新を戻した", correlation_id=f"github:{REPO}#5")

    assert {i["key"] for i in call("GET", "/api/desk/inbox", owner)[1]["items"]} == {*keys.values(), "stoppage:drain:line", "task:swap:o-r-1"}
    assert call("GET", "/api/desk/inbox", other_op)[1]["items"] == []          # 製品を限った運用主には、ほかの製品と工場全体の件は出ない
    assert [i["key"] for i in call("GET", "/api/desk/inbox", req)[1]["items"]] == ["task:swap:o-r-1"]   # 依頼者は起票者の件だけ
    assert call("GET", "/api/desk/inbox", req_other)[1]["items"] == []

    k = keys["いつから通話を始められるか"]
    assert call("GET", path(k), req)[0] == 404 and call("POST", path(k, "/answer"), req, {"option": "keep"})[0] == 404
    assert call("GET", path("task:swap:o-r-1"), req_other)[0] == 404
    st, res = call("POST", path("task:swap:o-r-1", "/answer"), req, {"option": "seen"})
    assert st == 200 and res["result"] == "answered"
    assert call("GET", path("task:swap:o-r-1"), owner)[0] == 409   # 閉じた件は、運用主には誰が答えたかを返す
    assert call("GET", path("task:swap:o-r-1"), req)[0] == 404
    assert call("GET", "/api/desk/inbox", None)[0] == 401
    assert call("GET", path("bad key!"), owner)[0] == 400


def test_irreversible_answer_needs_explicit_confirm(cur, monkeypatch):
    tok = desk.create_token(cur, "owner", "operator", [], "test")
    applied = []
    item = {"key": "release:o/r@1.2.0", "kind": "release", "who": "owner", "title": "o/r の 1.2.0 が本番待ち", "action": "", "url": "/#ship",
            "since": None, "ref": REPO, "answered": None,
            "answer": {"type": "approve", "options": [{"id": "approve", "label": "本番に出す"}], "extra": [], "effect": "本番に出す",
                       "irreversible": True, "allowed": ["owner"], "target": ("release", "o/r@1.2.0")}}
    monkeypatch.setattr(inbox, "find", lambda cur, key, now=None: dict(item) if key == item["key"] else None)
    monkeypatch.setattr(inbox, "_apply", lambda *a: applied.append(a) or (True, "本番に出す承認を受けた"))
    st, out = call("POST", path(item["key"], "/answer"), tok, {"option": "approve"})
    assert st == 400 and "confirm" in out["error"] and not applied
    st, out = call("POST", path(item["key"], "/answer"), tok, {"option": "approve", "confirm": True})
    assert st == 200 and out["result"] == "answered" and applied[0][4] == "mcp"
