"""受信トレイと通知（ADR 0026 の決定 7）。

時刻の判定は DB なしで、集め方と通知の記録は TEST_DATABASE_URL の Postgres で確かめる（なければ飛ばす）。
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
pytest.importorskip("psycopg")
os.environ.setdefault("NOTIFIER", "log")

import choice  # noqa: E402
import inbox  # noqa: E402
import notify_inbox  # noqa: E402
import stoppage  # noqa: E402

URL = os.environ.get("TEST_DATABASE_URL")
NOW = datetime(2026, 11, 1, 3, 0, tzinfo=timezone.utc)   # JST 12:00


def test_quiet_hours_wrap_midnight():
    jst = timezone(timedelta(hours=9))
    assert notify_inbox.quiet(datetime(2026, 11, 1, 23, 0, tzinfo=jst), "22-7")
    assert notify_inbox.quiet(datetime(2026, 11, 1, 6, 59, tzinfo=jst), "22-7")
    assert not notify_inbox.quiet(datetime(2026, 11, 1, 7, 0, tzinfo=jst), "22-7")
    assert not notify_inbox.quiet(datetime(2026, 11, 1, 23, 0, tzinfo=jst), "")
    assert notify_inbox.quiet(datetime(2026, 11, 1, 13, 0, tzinfo=jst), "12-14")


def test_due_new_renotify_escalate():
    it = {"key": "k", "kind": "task", "who": "po", "age_sec": 60}
    assert notify_inbox.due([it], {}, NOW) == [(it, "new")]
    recent = {"k": {"count": 1, "last": NOW - timedelta(minutes=10), "escalated": False}}
    assert notify_inbox.due([it], recent, NOW) == []
    old = {"k": {"count": 1, "last": NOW - timedelta(minutes=notify_inbox.RENOTIFY_MIN), "escalated": False}}
    assert notify_inbox.due([it], old, NOW) == [(it, "renotify")]
    long_wait = dict(it, age_sec=notify_inbox.ESCALATE_MIN * 60)
    assert notify_inbox.due([long_wait], recent, NOW) == [(long_wait, "escalate")]
    assert notify_inbox.due([dict(long_wait, who="builder")], recent, NOW) == []          # 代理そのものの件は切り替えない
    many = {"k": {"count": notify_inbox.MAX_RENOTIFY + 1, "last": NOW - timedelta(days=1), "escalated": True}}
    assert notify_inbox.due([it], many, NOW) == []                                         # 知らせ直すのは上限まで


def test_render_links_and_mentions(monkeypatch):
    monkeypatch.setattr(notify_inbox, "CONSOLE_URL", "http://localhost:8790")
    monkeypatch.setattr(notify_inbox, "MENTIONS", {"po": "<@U1>"})
    b = [({"key": "task:r", "kind": "task", "who": "po", "title": "予約の画面", "url": "https://example.com/review/1", "age_sec": 7200,
           "action": "体験を承認する"}, "renotify"),
         ({"key": "choice:3", "kind": "choice", "who": "design_chief", "title": "案の選択", "url": "/#choice-3", "age_sec": None,
           "action": "案を選ぶ"}, "new")]
    md = notify_inbox.render(b, "github-issue")
    assert "[予約の画面](https://example.com/review/1)（2 時間 0 分待ち）" in md and "[案の選択](http://localhost:8790/#choice-3)" in md
    assert "<@U1>" not in md
    sl = notify_inbox.render(b, "slack")
    assert "<@U1> プロダクトオーナー: <https://example.com/review/1|予約の画面>" in sl


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


def test_request_and_resolve(cur):
    assert inbox.request(cur, "review:o/r#12", "review_page", "po", "予約の画面の体験", url="https://example.com/r/12", action="体験を承認する")
    assert not inbox.request(cur, "review:o/r#12", "review_page", "po", "二重")
    with pytest.raises(ValueError):
        inbox.request(cur, "bad key!", "x", "po", "t")
    with pytest.raises(ValueError):
        inbox.request(cur, "k2", "x", "someone", "t")
    with pytest.raises(ValueError):
        inbox.request(cur, "k3", "x", "po", "t", url="javascript:alert(1)")
    [i] = inbox.items(cur)
    assert i["key"] == "task:review:o/r#12" and i["who"] == "po" and i["task_kind"] == "review_page"
    assert inbox.resolve(cur, "review:o/r#12", "console:t", "approved")
    assert not inbox.resolve(cur, "review:o/r#12", "console:t")
    assert inbox.items(cur) == []
    assert inbox.request(cur, "review:o/r#12", "review_page", "po", "直したあとにもう一度")   # 済んだあとは積み直せる
    assert len(inbox.items(cur)) == 1


def test_collects_stoppages_and_choices(cur):
    stoppage.open_stoppage(cur, "merge:12", "human", "マージ待ちの PR", waiting_for="マージ役のマージ", correlation_id="github:o/r#5", quiet=True)
    stoppage.open_stoppage(cur, "drain:20261101T000000", "human", "ラインが一時停止のまま", quiet=True)
    stoppage.open_stoppage(cur, "idle:x", "idle", "ラインが空", quiet=True)                      # 人の出番ではない
    cur.execute("insert into events(name, correlation_id, payload) values ('supervisor.merge_decided','github:o/r#5', %s)",
                (__import__("psycopg").types.json.Jsonb({"pr_url": "https://github.com/o/r/pull/12"}),))
    aid = choice.request(cur, "design_variants", "github:o/r#7", "送る前の確かめ方", [{"id": "A", "label": "a"}, {"id": "B", "label": "b"}],
                         recommended="A")
    xs = {i["key"]: i for i in inbox.items(cur)}
    assert set(xs) == {"stoppage:merge:12", "stoppage:drain:20261101T000000", f"choice:{aid}"}
    assert xs["stoppage:merge:12"]["url"] == "https://github.com/o/r/pull/12" and xs["stoppage:merge:12"]["who"] == "owner"
    assert xs["stoppage:drain:20261101T000000"]["who"] == "builder"
    assert xs[f"choice:{aid}"]["who"] == "po" and xs[f"choice:{aid}"]["url"] == f"/#choice-{aid}"
    v = inbox.view(cur)
    assert v["count"] == 3 and v["by_who"] == {"owner": 1, "builder": 1, "po": 1}
    assert all("announced" not in i for i in v["items"])


def test_tick_notifies_once_then_clears(cur):
    sent = []
    T = datetime.now(timezone.utc)
    inbox.request(cur, "review:1", "review_page", "po", "画面 1")
    r = notify_inbox.tick(cur, T, ["log"], lambda ch, b: sent.append((ch, [w for _, w in b])) or True)
    assert r["sent"] == 1 and sent == [("log", ["new"])]
    assert notify_inbox.tick(cur, T + timedelta(minutes=5), ["log"], lambda ch, b: sent.append(1) or True)["sent"] == 0
    [n] = names(cur, "inbox.notified")
    assert n["key"] == "task:review:1" and n["reason"] == "new" and n["count"] == 1 and n["channels"] == ["log"]
    inbox.resolve(cur, "review:1", "t", "approved")
    r = notify_inbox.tick(cur, T + timedelta(minutes=30), ["log"], lambda ch, b: True)
    [c] = names(cur, "inbox.cleared")
    assert r["cleared"] == 1 and c["key"] == "task:review:1" and c["notified"] == 1 and c["who"] == "po"
    assert notify_inbox.tick(cur, T + timedelta(minutes=31), ["log"], lambda ch, b: True)["cleared"] == 0   # 二度は消さない


def test_tick_retries_when_every_channel_fails(cur):
    T = datetime.now(timezone.utc)
    inbox.request(cur, "q:1", "ready_question", "builder", "着手判定の問い")
    r = notify_inbox.tick(cur, T, ["slack"], lambda ch, b: (_ for _ in ()).throw(OSError("down")))
    assert r.get("failed") and names(cur, "inbox.notified") == []
    assert notify_inbox.tick(cur, T, ["log"], lambda ch, b: True)["sent"] == 1


def test_tick_holds_during_quiet_hours(cur, monkeypatch):
    monkeypatch.setattr(notify_inbox, "QUIET", "22-7")
    inbox.request(cur, "q:2", "ready_question", "owner", "夜の問い")
    night = datetime(2026, 11, 1, 14, 0, tzinfo=timezone.utc)   # JST 23:00
    assert notify_inbox.tick(cur, night, ["log"], lambda ch, b: True)["quiet"]
    assert names(cur, "inbox.notified") == []
    assert notify_inbox.tick(cur, night + timedelta(hours=8), ["log"], lambda ch, b: True)["sent"] == 1   # 明けた最初の周回で送る


def test_github_issue_skips_first_notice_of_stoppages(monkeypatch):
    calls = []
    monkeypatch.setattr(stoppage, "STATUS_REPO", "o/r")
    monkeypatch.setenv("GH_TOKEN", "x")
    monkeypatch.delenv("WORKER_DRY_RUN", raising=False)
    monkeypatch.setattr(stoppage, "_status_issue", lambda: 1)
    monkeypatch.setattr(stoppage, "_gh", lambda *a: calls.append(a))
    st = {"key": "stoppage:merge:1", "kind": "stoppage", "who": "owner", "title": "t", "url": "", "age_sec": 1, "action": "a", "announced": True}
    assert notify_inbox._send("github-issue", [(st, "new")]) and calls == []
    assert notify_inbox._send("github-issue", [(st, "renotify")]) and len(calls) == 1


def test_design_choice_defers_to_review_page(cur):
    opts = [{"id": "A", "label": "a"}, {"id": "B", "label": "b"}]
    aid = choice.request(cur, "design_variants", "github:o/r#9", "q", opts, subject={"repo": "o/r", "ticket": "9", "uxspec": "ux-9"})
    other = choice.request(cur, "design_variants", "github:o/r#10", "q", opts, subject={"repo": "o/r", "ticket": "10", "uxspec": "ux-10"})
    assert {i["key"] for i in inbox.items(cur)} == {f"choice:{aid}", f"choice:{other}"}   # レビューページがまだなければ選択をそのまま出す
    inbox.request(cur, "design_review:ux-9", "review_page", "po", "画面 9 のレビュー", url="/review/ux-9")
    xs = inbox.items(cur)
    assert {i["key"] for i in xs} == {"task:design_review:ux-9", f"choice:{other}"} and all("uxspec" not in i for i in xs)
    # 見た目の案はデザインのチーフ宛て。体験のレビュー依頼では隠れず、見た目の依頼（design_look:<id>）があるときだけ任せる
    look = choice.request(cur, "design_look", "github:o/r#9", "q", opts, subject={"repo": "o/r", "ticket": "9", "uxspec": "ux-9"})
    xs = {i["key"]: i for i in inbox.items(cur)}
    assert xs[f"choice:{look}"]["who"] == "design_chief"
    inbox.request(cur, "design_look:ux-9", "review_page", "design_chief", "画面 9 の見た目", url="/review/ux-9")
    assert f"choice:{look}" not in {i["key"] for i in inbox.items(cur)}
