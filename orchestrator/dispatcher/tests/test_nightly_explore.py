"""夜間の探索（ADR 0028 の nightly_explore）: 夜の名前とチャーター、supervisor の積み込み、記録と Defect の起票、日報の行"""
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

if URL:
    os.environ.setdefault("DATABASE_URL", URL)
    os.environ.setdefault("GH_TOKEN", "x")
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb

    import supervisor


def test_defs_copy_matches_definitions():
    xd = yaml.safe_load((ROOT / "definitions" / "qa.yaml").read_text(encoding="utf-8"))["explore"]
    for k in ("hours", "through_pause", "priority", "defects_per_night_max", "file_severities"):
        assert xd[k] == qa_line.DEFS["explore"][k], k
    assert [c["id"] for c in xd["charters"]] == qa_line.DEFS["explore"]["charters"]
    events = yaml.safe_load((ROOT / "definitions" / "events.yaml").read_text(encoding="utf-8"))
    names = events.get("events") or events
    assert "qa.explore" in str(names) and "supervisor.nightly_explore_skipped" in str(names)


def test_night_and_charter_rotate():
    assert qa_line.night_of(datetime(2026, 11, 9, 2, tzinfo=JST)) == "2026-11-08"
    assert qa_line.night_of(datetime(2026, 11, 8, 23, tzinfo=JST)) == "2026-11-08"
    assert qa_line.night_of(datetime(2026, 11, 8, 17, 30, tzinfo=timezone.utc)) == "2026-11-08"   # 9 日 2:30 JST
    seen = [qa_line.charter_for(f"2026-11-{d:02d}") for d in range(1, 6)]
    assert sorted(seen) == sorted(qa_line.DEFS["explore"]["charters"])   # 5 晩で全部を 1 回ずつ
    # supervisor は definitions/qa.yaml を読む（チャーターは {id, viewpoint, ja}）。積む入力は id にそろえる（11/8 の夜に dict のまま渡り、ワーカーが先頭の detour に倒れた）
    full = {"explore": yaml.safe_load((ROOT / "definitions" / "qa.yaml").read_text(encoding="utf-8"))["explore"]}
    assert [qa_line.charter_for(f"2026-11-{d:02d}", full) for d in range(1, 6)] == seen


class FakeGH:
    def __init__(self, defects=None):
        self.calls, self.n, self.defects = [], 100, defects or []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET" and "factory:qa-defect" in path:
            return self.defects
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


def explore_result(findings):
    return {"kind": "qa", "ok": True, "mode": "explore", "sha": "abc", "env": "staging", "release_id": "r9", "model": "m", "mobile": True,
            "charter": "detour", "night": "2026-11-08",
            "explore": {"charter": "detour", "confidence": 0.6, "interpretation": "戻るで崩れる",
                        "tried": [{"actor": "student", "where": "予約", "did": "取り消して戻る", "saw": "残っている"}],
                        "findings": findings, "flags": [{"viewpoint": "aesthetic", "where": "ホーム", "note": "余白が詰まっている"}]}}


def F(title, sev="s2", **kw):
    return {"title": title, "severity": sev, "viewpoint": "flow", "actual": "x", "reproduce": ["開く"], **kw}


def test_enqueue_explore_and_record(cur):
    gh = FakeGH()
    got = qa_line.enqueue_explore(cur, gh, REPO, "2026-11-08")
    assert got["charter"] == qa_line.charter_for("2026-11-08")
    cur.execute("select payload from events where name='job.enqueued'")
    inp = cur.fetchone()["payload"]["input"]
    assert inp["mode"] == "explore" and inp["night"] == "2026-11-08" and inp["unattended"] is True
    cur.execute("select priority from jobs")
    assert cur.fetchone()["priority"] == qa_line.ops.LEVELS["p2"][0]
    jid = got["job_id"]
    findings = [F("取り消した予約が戻ると残る"), F("小さな食い違い", "s4"), F("経緯が出ない", known_gap=True),
                F("二度押しで 2 件"), F("他人の予約が見える", "s1"), F("4 件目は上限で起票しない", "s3")]
    existing = qa_line.explore_defect_body(REPO, {"charter": "come-back", "night": "2026-11-07"}, F("二度押しで 2 件"), 1, 7)
    gh = FakeGH(defects=[{"number": 50, "title": "QA: 二度押しで 2 件", "html_url": f"https://github.com/{REPO}/issues/50", "body": existing}])
    said = []
    out = qa_line.record_explore(cur, gh, lambda r, n, t: said.append((n, t)), REPO, 7, got["corr"], jid, explore_result(findings), {"01-a.png": "qa/1/01-a.png"})
    titles = [c[2]["title"] for c in gh.calls if c[0] == "POST" and c[1].endswith("/issues")]
    assert titles == ["QA: 取り消した予約が戻ると残る", "QA: 他人の予約が見える", "QA: 4 件目は上限で起票しない"]
    assert out["seen"] == [f"https://github.com/{REPO}/issues/50"] and out["skipped"] == []
    # 50 は再現のコメントとメジャー不具合（S2）の直しの知らせ（ADR 0053）
    assert [n for n, _ in said if n in (50, 7)] == [50, 50, 7] and any("また見つかりました" in t for n, t in said if n == 50)
    # S1 は Incident になり、QA 役がラインを差し止める（ADR 0043 の O5）
    import incident
    assert incident.held(cur) and any("差し止め" in t for _, t in said)
    summary = said[-1][1]
    assert "夜間の探索" in summary and "余白が詰まっている" in summary and "（起票しない）" in summary and "iOS あり" in summary
    cur.execute("select payload from events where name='qa.explore'")
    ev = cur.fetchone()["payload"]
    assert ev["unattended"] is True and ev["night"] == "2026-11-08" and ev["findings"] == 6 and ev["flags"][0]["note"] == "余白が詰まっている"
    assert [d.get("issue") is not None for d in ev["defects"]] == [True, False, False, True, True, True]
    cur.execute("select count(*) as n from events where name='defect.recorded' and payload->>'found_in'='qa' and payload->>'source'='qa_explore'")
    assert cur.fetchone()["n"] == 6   # Defect の型（ADR 0028 の決定 5）にも 1 件ずつ
    # 本文の印から、同じ題の Defect は次の夜に起票し直さない
    body = [c[2]["body"] for c in gh.calls if c[0] == "POST" and c[1].endswith("/issues")][0]
    assert qa_line.open_defects(lambda *a: [{"title": "QA: 取り消した予約が戻ると残る", "html_url": "u", "body": body}], REPO, "x") == \
        {qa_line.explore_key("取り消した予約が戻ると残る"): "u"}
    # 上限
    cur.execute("insert into jobs(correlation_id, repo, issue_number, enqueued_by, kind) values ('c', %s, 8, 't', 'qa') returning id", (REPO,))
    j2 = cur.fetchone()["id"]
    out2 = qa_line.record_explore(cur, FakeGH(), lambda *a: None, REPO, 7, "c", j2, explore_result([F(f"d{i}") for i in range(5)]), {})
    assert len(out2["filed"]) == 3 and len(out2["skipped"]) == 2
    nights = qa_line.explore_nights(cur, REPO)
    assert len(nights) == 2 and nights[0]["unattended"] is False   # 積んだ入力がないジョブは無人に数えない
    cur.execute("select count(distinct payload->>'night') from events where name = 'qa.explore' and (payload->>'unattended')::boolean")
    assert cur.fetchone()["count"] == 1   # roadmap の nightly_explore の数え方


# rally #1373 と #1402: 出荷の関門の探索が、題の末尾だけ違う同じ不具合を 2 回起票した
T1373 = "ホームの「まだ月額契約はありません」から月額契約の画面へ進めない（R-2026-W46-1 がまだ起きる）"
T1402 = "ホームの「まだ月額契約はありません」から月額契約の画面へ進めない（R-2026-W46-1 が直っていない）"
SUB = "student-subscription-persists"


def test_same_issue_by_similar_title_and_same_as():
    assert qa_line.title_similarity(T1373, T1402) >= qa_line.SIMILAR_SAME_STORY
    assert qa_line.title_similarity("予約を取り消すと一覧に残る", "予約を変更すると一覧に残る") < qa_line.SIMILAR_SAME_STORY
    assert qa_line.title_similarity("Ａ Ｂ", "ab") == 1.0   # 全角半角・空白は見ない
    it = {"number": 1373, "url": "u1373", "title": T1373, "story_id": SUB, "severity": "s2"}
    other = {"number": 9, "url": "u9", "title": "予約の一覧が空のまま", "story_id": "booking", "severity": "s2"}
    d = {"title": T1402, "story_id": SUB, "severity": "s2"}
    assert qa_line.same_issue(d, [other, it]) == (it, "similar")
    assert qa_line.same_issue({**d, "story_id": "booking"}, [it]) == (None, None)   # 別のストーリーと分かっているものにはまとめない
    assert qa_line.same_issue({**d, "story_id": None}, [it]) == (it, "similar")      # ストーリーが分からなければ高い線で
    assert qa_line.same_issue({**d, "title": "まったく違う言い方", "same_as": 1373}, [other, it]) == (it, "same_as")
    assert qa_line.same_issue({**d, "title": "まったく違う言い方", "same_as": 1}, [it]) == (None, None)   # 一覧にない番号は使わない
    assert qa_line.same_issue({**d, "severity": "s1"}, [it]) == (None, None)   # S1 は軽い Issue に吸わせない
    assert qa_line.same_issue({**d, "severity": "s1"}, [{**it, "severity": "s1"}]) == ({**it, "severity": "s1"}, "similar")


def test_explore_does_not_refile_same_defect(cur):
    """開いている Defect を探索の入力に渡し、言い方の違う同じ不具合は既存の Issue に「また見つかった」と書く"""
    body = qa_line.explore_defect_body(REPO, {"charter": "other-user", "gate": "o/app@0.16.0"}, F(T1373, story_id=SUB), 1, 7)
    gh = FakeGH(defects=[{"number": 1373, "title": f"QA: {T1373}", "html_url": f"https://github.com/{REPO}/issues/1373", "body": body}])
    got = qa_line.enqueue_explore(cur, gh, REPO, "2026-11-08")
    cur.execute("select payload from events where name='job.enqueued'")
    assert cur.fetchone()["payload"]["input"]["open_defects"] == [{"number": 1373, "title": T1373, "story_id": SUB, "severity": "s2"}]
    findings = [F(T1402, story_id=SUB), F("言い方がまったく違う", same_as=1373), F("予約の一覧で取り消した予約が残る", story_id=SUB), F("予約の一覧に取り消した予約が残る", story_id=SUB)]
    said = []
    out = qa_line.record_explore(cur, gh, lambda r, n, t: said.append((n, t)), REPO, 7, got["corr"], got["job_id"], explore_result(findings), {})
    titles = [c[2]["title"] for c in gh.calls if c[0] == "POST" and c[1].endswith("/issues")]
    assert titles == ["QA: 予約の一覧で取り消した予約が残る"]   # 同じ実行の中で起票したものにも当てる
    assert out["seen"] == [f"https://github.com/{REPO}/issues/1373"] * 2 + out["filed"]
    assert sum("また見つかりました" in t for n, t in said if n == 1373) == 2
    cur.execute("select payload from events where name='qa.explore'")
    assert [d.get("matched_by") for d in cur.fetchone()["payload"]["defects"]] == ["similar", "same_as", None, "similar"]


def test_supervisor_step(cur, monkeypatch):
    monkeypatch.setattr(supervisor, "NIGHTLY_EXPLORE", [REPO])
    monkeypatch.setattr(supervisor, "QA_DEFS", str(ROOT / "definitions" / "qa.yaml"))
    monkeypatch.setattr(supervisor, "gh", FakeGH())
    monkeypatch.setattr(supervisor, "over_budget", lambda c: None)
    supervisor.nightly_explore_step(cur, datetime(2026, 11, 8, 23, tzinfo=JST))   # 夜の時間の前
    cur.execute("select count(*) as n from jobs")
    assert cur.fetchone()["n"] == 0
    # 一時停止中でも、このジョブだけ流す
    cur.execute("insert into events(name, payload) values ('factory.drain_requested', %s)", (Jsonb({"by": "t"}),))
    supervisor.nightly_explore_step(cur, datetime(2026, 11, 9, 1, 5, tzinfo=JST))
    supervisor.nightly_explore_step(cur, datetime(2026, 11, 9, 1, 6, tzinfo=JST))   # 同じ夜は 1 回
    cur.execute("select count(*) as n from jobs")
    assert cur.fetchone()["n"] == 1
    import drain
    assert drain.allowed(cur) == [f"github:{REPO}#7"]
    cur.execute("select payload from events where name='supervisor.nightly_explore_enqueued'")
    p = cur.fetchone()["payload"]
    assert p["night"] == "2026-11-08" and p["through_pause"] is True
    # 予算の枠を使い切っていれば積まず、理由を 1 回だけ残す
    monkeypatch.setattr(supervisor, "over_budget", lambda c: {"budget_usd": 1, "spent_usd": 2, "window_start": "x"})
    supervisor.nightly_explore_step(cur, datetime(2026, 11, 10, 2, tzinfo=JST))
    supervisor.nightly_explore_step(cur, datetime(2026, 11, 10, 3, tzinfo=JST))
    cur.execute("select payload from events where name='supervisor.nightly_explore_skipped'")
    rows = cur.fetchall()
    assert len(rows) == 1 and rows[0]["payload"]["reason"] == "over_budget" and rows[0]["payload"]["night"] == "2026-11-09"


def test_daily_report_lines(cur):
    sys.path.insert(0, str(ROOT / "health" / "daily"))
    import status_report
    now = datetime(2026, 11, 9, 5, 30, tzinfo=JST)
    cur.execute("insert into events(name, payload, occurred_at) values ('qa.explore', %s, %s)",
                (Jsonb({"repo": "guccyon/rally", "night": "2026-11-08", "unattended": True, "charter": "detour", "release_id": "r9", "mobile": True,
                        "tried": 4, "findings": 2, "issue": 7, "flags": [{"viewpoint": "tone", "where": "予約", "note": "冷たい"}],
                        "defects": [{"title": "他人の予約が見える", "severity": "s1", "viewpoint": "authz", "issue": "https://x/1"},
                                    {"title": "小さい", "severity": "s4", "viewpoint": "flow", "issue": None}]}), now - timedelta(hours=3)))
    cur.execute("insert into events(name, payload, occurred_at) values ('supervisor.nightly_explore_skipped', %s, %s)",
                (Jsonb({"repo": "guccyon/other", "night": "2026-11-08", "reason": "over_budget"}), now - timedelta(hours=4)))
    with psycopg.connect(URL, autocommit=True) as plain:   # 日報は行のタプルで読む
        view = status_report.explore_view(plain, now)
    lines = "\n".join(status_report.fmt_explore(view))
    assert "**[s1/authz] 他人の予約が見える** → https://x/1" in lines and "（起票しない）" in lines
    assert "印 [tone] 予約: 冷たい" in lines and "other: 昨夜は回らなかった" in lines and "累計 1" in lines
    assert status_report.fmt_explore(None) == ["- 昨夜の探索の記録はない"]
