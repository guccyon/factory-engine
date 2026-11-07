"""受信トレイの答えの口（ADR 0034 の順番 1）。TEST_DATABASE_URL の Postgres で確かめる（なければ飛ばす）。"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
pytest.importorskip("psycopg")
os.environ.setdefault("NOTIFIER", "log")

from psycopg.types.json import Jsonb  # noqa: E402

import choice  # noqa: E402
import drain  # noqa: E402
import inbox  # noqa: E402
import notify_inbox  # noqa: E402
import ship  # noqa: E402
import stoppage  # noqa: E402

URL = os.environ.get("TEST_DATABASE_URL")
REPO = "o/app"


@pytest.fixture()
def cur():
    if not URL:
        pytest.skip("TEST_DATABASE_URL がない")
    import psycopg
    from psycopg.rows import dict_row
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


def names(cur, name):
    cur.execute("select payload from events where name=%s order by id", (name,))
    return [r["payload"] for r in cur.fetchall()]


def opts(*ids):
    return [{"id": i, "label": f"案 {i}"} for i in ids]


def test_choose_first_answer_wins(cur):
    aid = choice.request(cur, "taste_sample", "github:o/r#7", "どちらが読みやすいか", opts("A", "B"), recommended="A")
    [i] = inbox.items(cur)
    assert i["answer"]["type"] == "choose" and [o["id"] for o in i["answer"]["options"]] == ["A", "B"] and i["answer"]["effect"]
    assert inbox.answer(cur, f"choice:{aid}", "C", "console:t", "console")[0] == "invalid"
    assert inbox.answer(cur, f"choice:{aid}", "B", "console:t", "console", note="余白が広い") == ("answered", "chosen")
    res, msg = inbox.answer(cur, f"choice:{aid}", "A", "slack:U1", "slack")
    assert res == "already" and "console:t" in msg and "『案 B』" in msg
    assert choice.get(cur, aid)["chosen"] == "B"
    [a] = names(cur, "inbox.answered")
    assert a["key"] == f"choice:{aid}" and a["option"] == "B" and a["via"] == "console" and a["explicit"] is True and a["closes"] is True
    assert inbox.items(cur) == []


def test_return_needs_a_note_and_flag_keeps_it_open(cur):
    aid = choice.request(cur, "taste_sample", "github:o/r#7", "q", opts("A", "B"))
    assert inbox.answer(cur, f"choice:{aid}", "return", "console:t", "console")[0] == "invalid"
    assert inbox.answer(cur, f"choice:{aid}", "flag", "console:t", "console", note="文字が詰まる", on="A") == ("noted", "flagged")
    assert [i["key"] for i in inbox.items(cur)] == [f"choice:{aid}"]          # 印は件を閉じない
    assert names(cur, "taste.flagged")[0]["option"] == "A"
    assert inbox.answer(cur, f"choice:{aid}", "return", "console:t", "console", note="どれも重い")[0] == "answered"
    assert choice.get(cur, aid)["status"] == "returned"


def test_review_task_answers_the_same_choice(cur):
    aid = choice.request(cur, "design_variants", "github:o/r#9", "予約の画面", opts("A", "B"), recommended="B", subject={"uxspec": "41"})
    inbox.request(cur, "design_review:41", "review_page", "po", "o/r#9 の画面の体験を判断する", url="/review/41")
    [t] = inbox.items(cur)                                                    # 人の選択はレビューページの件に任せて隠す
    assert t["key"] == "task:design_review:41" and t["answer"]["type"] == "choose" and t["answer"]["recommended"] == "B"
    assert inbox.answer(cur, "task:design_review:41", "B", "console:t", "console")[0] == "answered"
    assert choice.get(cur, aid)["status"] == "chosen"
    assert inbox.items(cur) == []                                             # 答え済み（引き渡し待ち）は人の出番ではない
    [v] = inbox.view(cur)["items"]
    assert v["key"] == "task:design_review:41" and v["answered"]["option"] == "B" and v["answered"]["via"] == "console"
    assert inbox.view(cur)["count"] == 0
    assert inbox.answer(cur, f"choice:{aid}", "A", "review", "review_page")[0] == "already"   # 別名の入口からでも答え済み
    inbox.resolve(cur, "design_review:41", "design_line", "handed_off")
    inbox.request(cur, "design_review:41", "review_page", "po", "直したあとにもう一度")    # 積み直した依頼は前の答えを引きずらない
    [t2] = inbox.items(cur)
    assert t2["answered"] is None


def test_exit_captures_ack_all(cur):
    for n in (1, 2):
        choice.request(cur, "exit_capture", f"github:o/r#{n}", "画面", [{"id": "s1", "label": "一覧"}], subject={"pr_url": f"https://github.com/o/r/pull/{n}"})
    [i] = inbox.items(cur)
    assert i["key"] == "choice:exit_capture" and i["answer"]["type"] == "ack"
    assert inbox.answer(cur, "choice:exit_capture", "seen", "console:t", "console") == ("answered", "2 件を見たとして閉じた")
    assert len(names(cur, "choice.seen")) == 2 and inbox.items(cur) == []


def test_resume_line_and_unanswerable_stoppage(cur):
    drain.set_drain(cur, True, "t", "更新")
    stoppage.open_stoppage(cur, "drain:20261105T000000", "human", "ラインが一時停止のまま", quiet=True)
    stoppage.open_stoppage(cur, "merge:12", "human", "マージ待ちの PR", quiet=True)
    xs = {i["key"]: i for i in inbox.items(cur)}
    assert xs["stoppage:merge:12"]["answer"] is None
    assert inbox.answer(cur, "stoppage:merge:12", "approve", "console:t", "console")[0] == "invalid"
    assert inbox.answer(cur, "stoppage:drain:20261105T000000", "resume", "console:t", "console")[0] == "answered"
    assert drain.active(cur) is False


def test_release_needs_explicit_answer_and_reopens_after_failure(cur):
    ship.observe_stage(cur, REPO, "launch")
    rid = f"{REPO}@1.2.0"
    cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values ('release.created',%s,%s,%s)",
                (f"release:{rid}", Jsonb({"release_id": rid, "repo": REPO, "app_version": "1.2.0", "prs": []}), f"release.created:{rid}"))
    ship.record(cur, "release.deployed", rid, {"repo": REPO, "env": "staging", "ok": True, "request_id": 0})
    [i] = inbox.items(cur)
    assert i["answer"]["irreversible"] is True
    assert inbox.answer(cur, f"release:{rid}", "approve", "slack:U1", "slack", explicit=False)[0] == "invalid"   # 自由文の「OK」では出さない
    assert inbox.answer(cur, f"release:{rid}", "approve", "console:t", "console")[0] == "answered"
    assert inbox.answer(cur, f"release:{rid}", "approve", "console:t", "console")[0] == "already"                # 二重に承認しない
    assert len(names(cur, "release.approved")) == 1
    cur.execute("select id from events where name='release.approved'")
    req = cur.fetchone()["id"]
    ship.record(cur, "release.ship_failed", rid, {"repo": REPO, "env": "prod", "request_id": req, "error": "x"})
    [again] = inbox.items(cur)                                                # 出荷に失敗したらまた答えられる
    assert again["key"] == f"release:{rid}" and again["answered"] is None


def test_role_must_be_allowed(cur):
    aid = choice.request(cur, "design_variants", "github:o/r#9", "q", opts("A", "B"))
    res, msg = inbox.answer(cur, f"choice:{aid}", "A", "gh:x", "github", role="requester")
    assert res == "invalid" and "プロダクトオーナー" in msg
    assert inbox.answer(cur, f"choice:{aid}", "A", "gh:x", "github", role="po")[0] == "answered"


def test_failed_effect_leaves_it_open(cur):
    # デザインの線がまだ人の選択を待っていない（ui_design.awaiting_choice がない）ので、選んでも受けない。何も残さず件は開いたまま
    aid = choice.request(cur, "design_variants", "github:o/r#9", "q", opts("A", "B"), subject={"repo": "o/r", "ticket": "9"})
    res, msg = inbox.answer(cur, f"choice:{aid}", "A", "console:t", "console")
    assert res == "failed" and "待っている案がない" in msg
    assert choice.get(cur, aid)["status"] == "open" and names(cur, "inbox.answered") == []
    inbox.request(cur, "swap:s1:rolled_back", "swap_rolled_back", "builder", "更新を戻した")
    inbox.resolve(cur, "swap:s1:rolled_back", "t")
    assert inbox.answer(cur, "task:swap:s1:rolled_back", "seen", "console:t", "console")[0] == "not_found"


def test_write_back_to_where_it_was_notified(cur):
    T = datetime.now(timezone.utc)
    inbox.request(cur, "swap:s1:rolled_back", "swap_rolled_back", "builder", "更新 s1 を前の版に戻した")
    assert notify_inbox.tick(cur, T, ["log"], lambda ch, b: True)["sent"] == 1
    assert inbox.answer(cur, "task:swap:s1:rolled_back", "seen", "console:t", "console")[0] == "answered"
    posted = []
    r = notify_inbox.tick(cur, T + timedelta(minutes=1), ["log"], lambda ch, b: True, poster=lambda name, text: posted.append((name, text)) or True)
    assert r["written_back"] == 1 and r["cleared"] == 1
    [(name, text)] = posted
    assert name == "log" and "更新 s1 を前の版に戻した" in text and "console:t が操業コンソールで『見た（閉じる）』と回答済み" in text
    r = notify_inbox.tick(cur, T + timedelta(minutes=2), ["log"], lambda ch, b: True, poster=lambda name, text: posted.append(1) or True)
    assert r["written_back"] == 0 and len(posted) == 1                      # 1 つの答え・1 宛先に 1 回


def test_write_back_skips_destinations_that_were_never_told(cur):
    inbox.request(cur, "swap:s2:rolled_back", "swap_rolled_back", "builder", "知らせる前に答えた")
    inbox.answer(cur, "task:swap:s2:rolled_back", "seen", "console:t", "console")
    posted = []
    r = notify_inbox.tick(cur, datetime.now(timezone.utc), ["log"], lambda ch, b: True, poster=lambda n, t: posted.append(t) or True)
    assert r["written_back"] == 0 and posted == []


def test_console_routes_go_through_the_answer(cur, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", URL)
    import console
    aid = choice.request(cur, "taste_sample", "github:o/r#7", "q", opts("A", "B"))
    code, out = console.act(cur, f"/api/choices/{aid}/choose", {"option": "A"})
    assert code == 200 and out["result"] == "answered"
    code, out = console.act(cur, f"/api/choices/{aid}/choose", {"option": "B"})
    assert code == 409 and out["result"] == "already"
    code, out = console.act(cur, f"/api/choices/{aid}/flag", {"note": "閉じた後の印", "option": "A"})   # 閉じた依頼にも印は付く
    assert code == 200
    bid = choice.request(cur, "taste_sample", "github:o/r#8", "q", opts("A", "B"))
    code, out = console.act(cur, "/api/inbox/answer", {"key": f"choice:{bid}", "option": "B", "note": "読みやすい"})
    assert code == 200 and choice.get(cur, bid)["chosen"] == "B"
    assert console.act(cur, "/api/inbox/answer", {"key": "task:nothing", "option": "x"})[0] == 404
    assert [a["via"] for a in names(cur, "inbox.answered")] == ["console", "console"]
