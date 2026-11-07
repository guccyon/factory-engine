"""窓口の依頼の壁打ちと、タスクのまとまりの投入（ADR 0030 の順番 2）: 相談役のジョブを積む・結果を会話に書く・確認・起票・依存の解放、窓口の枠。

空の Postgres に postgres/init/*.sql を当て、TEST_DATABASE_URL を向けて動かす（なければ飛ばす）。GitHub は偽物で受ける。
"""
import os
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL がない")
REPO = "guccyon/rally"
DEFS = Path(__file__).resolve().parents[3] / "definitions" / "desk.yaml"

if URL:
    import psycopg
    from psycopg.rows import dict_row

    import desk
    import desk_api
    import desk_line
    import line
    import ops


@pytest.fixture()
def cur():
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets, desk_conversations, desk_tokens restart identity cascade")
    yield c
    conn.close()


class FakeGH:
    """Issue の起票・取得・一覧だけ受ける偽物の GitHub"""

    def __init__(self, fail_after: int | None = None):
        self.issues: dict[int, dict] = {}
        self.next = 100
        self.fail_after = fail_after
        self.labels: list[tuple] = []
        self.comments: list[tuple] = []

    def __call__(self, method, path, body=None):
        if method == "POST" and path.endswith("/issues"):
            if self.fail_after is not None and len(self.issues) >= self.fail_after:
                raise RuntimeError("GitHub が落ちた")
            n = self.next
            self.next += 1
            self.issues[n] = {"number": n, "html_url": f"https://github.com/{REPO}/issues/{n}", "state": "open", **body}
            return self.issues[n]
        if method == "GET" and "/issues?" in path:
            label = path.split("labels=")[1].split("&")[0].replace("%3A", ":")
            return [i for i in self.issues.values() if i["state"] == "open" and label in i["labels"]]
        if method == "GET" and "/issues/" in path:
            return self.issues.get(int(path.rsplit("/", 1)[1]), {"state": "open"})
        raise AssertionError((method, path))

    def swap_label(self, repo, n, remove, add):
        self.labels.append((n, remove, add))
        it = self.issues[n]
        it["labels"] = [l for l in it["labels"] if l not in remove] + add

    def comment(self, repo, n, text):
        self.comments.append((n, text))


def requester(cur, name="cs-1"):
    desk.create_token(cur, name, "requester", [REPO], "test")
    return {"name": name, "role": "requester", "products": [REPO]}


def open_request(cur, who, body="予約まわりの通知を先に進めて", level=None):
    return desk.open_conversation(cur, who, "request", REPO, body, level=level)


def run(cur, jid, result, state="succeeded", err=None):
    cur.execute("update jobs set state=%s where id=%s returning *", (state, jid))
    job = cur.fetchone()
    desk_line.record(cur, job, result, state, err)
    return job


def task(key, kind="implement", deps=(), title=None):
    return {"key": key, "title": title or f"{key} の題", "kind": kind, "summary": "s", "do": ["やる"], "acceptance": ["通る"],
            "depends_on": list(deps), "effort": "standard", "related": ["#812"], "expected_files": ["a.rs"]}


DRAFT = {"kind": "desk_consult", "ok": True, "outcome": "draft", "status": "clarified", "cost_usd": 0.4,
         "request": {"what": "予約の通知", "why": "取りこぼし", "by_when": "今月中", "precision": "主要な場面"},
         "unresolved": [], "overlaps": [{"ref": "#812", "how": "同じ画面"}], "questions_asked": 1,
         "tasks": [task("t1", "requirements"), task("t2", "contract", ["t1"]), task("t3", "implement", ["t2"])]}
QUESTION = {"kind": "desk_consult", "ok": True, "outcome": "question", "round": 1, "max_rounds": 3, "cost_usd": 0.2,
            "question": {"question": "どの通知ですか", "why": "決まると切れる", "options": ["予約の確定", "前日の知らせ"], "recommended": 1}}


def test_definitions_match(cur):
    d = yaml.safe_load(DEFS.read_text(encoding="utf-8"))
    assert d["labels"] == {"waiting": desk_line.WAITING_LABEL, "trigger": desk_line.TRIGGER_LABEL}
    assert d["consult"]["max_rounds"] == desk_line.MAX_ROUNDS
    assert set(d["task_kinds"]) == set(desk_line.KIND_JA)
    assert set(d["efforts"]) == set(desk_line.EFFORT_JA)


def test_desk_slot_is_separate_from_line(cur):
    c = open_request(cur, requester(cur))
    [jid] = desk_line.enqueue(cur, "tier-small", [REPO])
    assert line.claim_line(cur) is None                     # ラインの枠は相談のジョブを取らない
    assert line.claim_governance(cur) is None
    j = line.claim_desk(cur)
    assert j["id"] == jid and j["kind"] == "desk_consult" and j["issue_number"] == -c["id"]
    assert line.running(cur) == {"workers": 0, "governance": 0, "desk": 1}
    line.scale(cur, "test", desk=0)
    assert line.spec(cur)["desk"] == 0


def test_enqueue_once_and_again_after_reply(cur):
    who = requester(cur)
    cur.execute("insert into tickets(source, source_ref, repo, title, state) values ('github',%s,%s,'済んだお休み','closed'), "
                "('github','other/x#1','other/x','ほかの製品','closed')", (f"{REPO}#455", REPO))
    c = open_request(cur, who)
    [jid] = desk_line.enqueue(cur, "tier-small", [REPO])
    assert desk_line.enqueue(cur, "tier-small", [REPO]) == []    # 動いている間は積まない
    cur.execute("select payload from events where name='job.enqueued' and job_id=%s", (jid,))
    inp = cur.fetchone()["payload"]["input"]
    assert inp["messages"][0]["body"] == "予約まわりの通知を先に進めて" and inp["questions_asked"] == 0 and inp["previous_draft"] is None
    assert [t["ref"] for t in inp["recently_closed"]] == [f"{REPO}#455"]   # 済んだ仕事を「待ち」に挙げないための材料（製品ごと）
    run(cur, jid, QUESTION)
    cur.execute("select state from desk_conversations where id=%s", (c["id"],))
    assert cur.fetchone()["state"] == "waiting_requester"
    assert desk_line.enqueue(cur, "tier-small", [REPO]) == []    # 依頼者の返事を待つ
    desk.post_message(cur, who, c["id"], "前日の知らせ")
    [jid2] = desk_line.enqueue(cur, "tier-small", [REPO])
    cur.execute("select payload from events where name='job.enqueued' and job_id=%s", (jid2,))
    inp = cur.fetchone()["payload"]["input"]
    assert inp["questions_asked"] == 1 and [m["role"] for m in inp["messages"]] == ["requester", "planner", "requester"]
    cur.execute("select rounds, cost_usd from desk_conversations where id=%s", (c["id"],))
    r = cur.fetchone()
    assert r["rounds"] == 1 and float(r["cost_usd"]) == pytest.approx(0.2)


def test_inquiry_is_not_consulted(cur):
    desk.open_conversation(cur, requester(cur), "inquiry", REPO, "今週どこまで進んだ？")
    assert desk_line.enqueue(cur, "tier-small", [REPO]) == []


def test_not_a_factory_repo_and_cost_limit(cur, monkeypatch):
    who = requester(cur)
    c = open_request(cur, who)
    assert desk_line.enqueue(cur, "tier-small", ["guccyon/other"]) == []
    out = desk.read_conversation(cur, who, c["id"])
    assert out["messages"][-1]["kind"] == "error" and out["conversation"]["state"] == "waiting_requester"
    desk.post_message(cur, who, c["id"], "rally です")
    cur.execute("update desk_conversations set cost_usd=5 where id=%s", (c["id"],))
    assert desk_line.enqueue(cur, "tier-small", [REPO]) == []
    out = desk.read_conversation(cur, who, c["id"])
    assert out["messages"][-1]["kind"] == "limit"


def test_draft_confirm_and_file(cur):
    who = requester(cur)
    c = open_request(cur, who, level="p1")
    [jid] = desk_line.enqueue(cur, "tier-small", [REPO])
    run(cur, jid, DRAFT)
    out = desk.read_conversation(cur, who, c["id"])
    assert out["conversation"]["state"] == "drafted"
    d = out["draft"]
    assert [t["key"] for t in d["tasks"]] == ["t1", "t2", "t3"] and d["estimate"]["level"] == "p1"
    assert "下書き" in out["messages"][-1]["body"] and "#812" in out["messages"][-1]["body"]
    cur.execute("select payload from artifacts where type='Request' and correlation_id=%s", (desk.corr(c["id"]),))
    assert cur.fetchone()["payload"]["what"] == "予約の通知"
    with pytest.raises(desk.DeskError) as e:
        desk.confirm_draft(cur, who, c["id"], d["draft_id"] + 99)
    assert e.value.status == 409
    other = requester(cur, "cs-2")
    with pytest.raises(desk.DeskError):
        desk.confirm_draft(cur, other, c["id"], d["draft_id"])           # ほかの依頼者の相談は見えない
    desk.confirm_draft(cur, who, c["id"], d["draft_id"])
    with pytest.raises(desk.DeskError):
        desk.post_message(cur, who, c["id"], "やっぱり")                  # 確認した相談には書けない
    gh = FakeGH()
    assert desk_line.file_confirmed(cur, gh, [REPO]) == 1
    assert desk_line.file_confirmed(cur, gh, [REPO]) == 0              # 二重に起票しない
    assert sorted(gh.issues) == [100, 101, 102]
    i1, i2, i3 = gh.issues[100], gh.issues[101], gh.issues[102]
    assert i1["labels"] == ["factory:plan", "factory:priority:p1"]
    assert i2["labels"][0] == "factory:desk-waiting" and "依存: #100" in i2["body"] and "依存: #101" in i3["body"]
    assert "<!-- factory:desk conversation=" in i1["body"] and "## 受け入れ条件" in i1["body"] and "#812" in i1["body"]
    cur.execute("select source_ref, priority, priority_set_by, desk_conversation_id, waiting_on from tickets order by id")
    ts = cur.fetchall()
    assert [t["source_ref"] for t in ts] == [f"{REPO}#100", f"{REPO}#101", f"{REPO}#102"]
    assert all(t["priority"] == 75 and t["priority_set_by"] == "desk:cs-1" and t["desk_conversation_id"] == c["id"] for t in ts)
    assert ts[1]["waiting_on"] == [f"{REPO}#100"]
    out = desk.read_conversation(cur, who, c["id"])
    assert len(out["tickets"]) == 3 and [m["event"] for m in out["messages"][-3:]] == ["desk.message", "desk.handed_off", "desk.closed"]
    assert out["messages"][-3]["kind"] == "tickets"
    # チケット化は起票で製品のラインに渡し、会話は handed_off で終える（ADR 0039 の決定 1）
    cv = out["conversation"]
    assert cv["state"] == "handed_off" and cv["close_reason"] == "handed_off" and cv["path"] == "ticket"
    assert out["messages"][-1]["record"]["outcome"] == "handed_off" and out["messages"][-1]["record"]["sec_routed"] is not None
    # 依頼者は自分の依頼から作ったチケットの優先度を変えられる
    st = desk.set_priority(cur, who, f"{REPO}#102", level="p0")
    assert st["level"] == "p0"
    # 依存が閉じたら、次のタスクをプランナーへ
    assert desk_line.advance_waiting(cur, gh, gh.swap_label, gh.comment, [REPO]) == 0
    gh.issues[100]["state"] = "closed"
    assert desk_line.advance_waiting(cur, gh, gh.swap_label, gh.comment, [REPO]) == 1
    assert gh.issues[101]["labels"][-1] == "factory:plan" and "factory:desk-waiting" not in gh.issues[101]["labels"]
    assert "factory:desk-waiting" in gh.issues[102]["labels"]
    cur.execute("select waiting_on from tickets where source_ref=%s", (f"{REPO}#101",))
    assert cur.fetchone()["waiting_on"] == []


def test_file_resumes_after_failure(cur):
    who = requester(cur)
    c = open_request(cur, who)
    [jid] = desk_line.enqueue(cur, "tier-small", [REPO])
    run(cur, jid, DRAFT)
    desk.confirm_draft(cur, who, c["id"], desk.latest_draft(cur, c["id"])["id"])
    gh = FakeGH(fail_after=1)
    assert desk_line.file_confirmed(cur, gh, [REPO]) == 0
    assert len(gh.issues) == 1
    gh.fail_after = None
    assert desk_line.file_confirmed(cur, gh, [REPO]) == 1
    assert sorted(gh.issues) == [100, 101, 102] and "依存: #100" in gh.issues[101]["body"]


def test_revise_after_draft(cur):
    who = requester(cur)
    c = open_request(cur, who)
    [jid] = desk_line.enqueue(cur, "tier-small", [REPO])
    run(cur, jid, DRAFT)
    first = desk.latest_draft(cur, c["id"])["id"]
    desk.post_message(cur, who, c["id"], "契約のタスクは要らない")
    [jid2] = desk_line.enqueue(cur, "tier-small", [REPO])
    cur.execute("select payload from events where name='job.enqueued' and job_id=%s", (jid2,))
    assert [t["key"] for t in cur.fetchone()["payload"]["input"]["previous_draft"]["tasks"]] == ["t1", "t2", "t3"]
    with pytest.raises(desk.DeskError):
        desk.confirm_draft(cur, who, c["id"], first)                    # 言い足し中は確認できない
    run(cur, jid2, {**DRAFT, "tasks": [task("t1", "requirements"), task("t2", "implement", ["t1"])]})
    with pytest.raises(desk.DeskError):
        desk.confirm_draft(cur, who, c["id"], first)                    # 古い下書きは確認できない
    desk.confirm_draft(cur, who, c["id"], desk.latest_draft(cur, c["id"])["id"], level="p3")


def test_failures_are_told_to_requester(cur):
    who = requester(cur)
    c = open_request(cur, who)
    [jid] = desk_line.enqueue(cur, "tier-small", [REPO])
    run(cur, jid, None, state="failed", err="exit=1")
    out = desk.read_conversation(cur, who, c["id"])
    assert out["messages"][-1]["kind"] == "error" and out["conversation"]["state"] == "waiting_requester"
    desk.post_message(cur, who, c["id"], "もう一度")
    [jid2] = desk_line.enqueue(cur, "tier-small", [REPO])
    run(cur, jid2, {"kind": "desk_consult", "ok": True, "outcome": "failed", "errors": ["依存が輪になっている"]})
    assert "依存が輪" in desk.read_conversation(cur, who, c["id"])["messages"][-1]["body"]


def test_api_confirm(cur):
    tok = desk.create_token(cur, "cs-1", "requester", [REPO], "test")
    who = {"name": "cs-1", "role": "requester", "products": [REPO]}
    c = open_request(cur, who)
    [jid] = desk_line.enqueue(cur, "tier-small", [REPO])
    run(cur, jid, DRAFT)
    did = desk.latest_draft(cur, c["id"])["id"]
    connect = lambda: psycopg.connect(URL, row_factory=dict_row, autocommit=True)  # noqa: E731
    st, out = desk_api.route("GET", f"/api/desk/conversations/{c['id']}", {}, {}, tok, connect=connect)
    assert st == 200 and out["draft"]["draft_id"] == did
    st, out = desk_api.route("POST", f"/api/desk/conversations/{c['id']}/confirm", {}, {"draft_id": did, "level": "p1"}, tok, connect=connect)
    assert st == 200 and out["conversation"]["state"] == "confirmed"


def test_estimate(cur):
    ops.upsert_ticket(cur, "github", f"{REPO}#1", REPO, title="高い", priority=75, state="planned")
    ops.upsert_ticket(cur, "github", f"{REPO}#2", REPO, title="低い", priority=25, state="planned")
    cur.execute("insert into events(name) select 'pr.merged' from generate_series(1, 14)")
    e = desk_line.estimate(cur, 3, "p1")
    assert e["ahead"] == 1 and e["per_day"] == 2.0 and e["start_in_days"] == 0.5
    assert [p["ref"] for p in e["pushed_back"]] == [f"{REPO}#2"]


def test_open_from_issue(cur):
    gh = FakeGH()
    gh.issues[448] = {"number": 448, "title": "最近テニスの生徒の反応が鈍い気がする", "body": "本文", "state": "open", "labels": [],
                      "html_url": f"https://github.com/{REPO}/issues/448"}
    cur.execute("insert into artifacts(type, correlation_id, produced_by, payload) values ('Request', %s, 'clarify', %s)",
                (f"github:{REPO}#448", psycopg.types.json.Jsonb({"status": "clarified", "what": "反応の数字", "why": "w", "by_when": "b", "precision": "p"})))
    op = {"name": "factoryctl", "role": "operator", "products": []}
    c = desk_line.open_from_issue(cur, gh, op, REPO, 448)
    body = desk.read_conversation(cur, op, c["id"])["messages"][1]["body"]
    assert "反応の数字" in body and f"{REPO}#448" in body
    assert c["source_ref"] == f"https://github.com/{REPO}/issues/448"   # 依頼の来た口（ADR 0039 の決定 1）
    assert [j for j in desk_line.enqueue(cur, "tier-small", [REPO])]
