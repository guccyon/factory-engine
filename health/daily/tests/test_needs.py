"""日報の「頼みたいこと」に載せる承認待ちの PR（health/daily/status_report.py）。GitHub は差し替えて、通信しない"""
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
import status_report as sr  # noqa: E402

NOW = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)


def pr(n, title, created, draft=False):
    return {"number": n, "title": title, "html_url": f"https://github.com/x/pull/{n}", "created_at": created,
            "draft": draft, "pull_request": {}}


def fake(monkeypatch, labeled, open_):
    monkeypatch.setattr(sr, "REPOS", ["o/app", "o/engine"])
    monkeypatch.setattr(sr, "HUMAN_MERGE_REPOS", ["o/engine"])
    monkeypatch.setattr(sr, "labeled_open_prs", lambda r, label: labeled.get(r, []))
    monkeypatch.setattr(sr, "open_prs", lambda r: open_.get(r, []))


def test_lists_label_and_human_merge_repo_prs_oldest_first(monkeypatch):
    fake(monkeypatch,
         labeled={"o/app": [pr(5, "表の判定で人", "2026-10-31T12:00:00Z")]},
         open_={"o/engine": [pr(9, "工場の変更", "2026-10-29T00:00:00Z"),
                             pr(10, "下書き", "2026-10-30T00:00:00Z", draft=True)]})
    got = sr.approval_prs(NOW)
    assert [(p["repo"], p["number"], p["why"]) for p in got] == [("o/engine", 9, "repo"), ("o/app", 5, "label")]
    assert got[0]["days"] == 3.0 and got[1]["days"] == 0.5


def test_labeled_pr_in_human_merge_repo_is_listed_once(monkeypatch):
    p = pr(7, "両方に当たる", "2026-10-31T00:00:00Z")
    fake(monkeypatch, labeled={"o/engine": [p]}, open_={"o/engine": [p]})
    got = sr.approval_prs(NOW)
    assert len(got) == 1 and got[0]["why"] == "label"


def test_fetch_error_is_reported_not_raised(monkeypatch):
    def boom(r, label):
        raise OSError("down")
    fake(monkeypatch, labeled={}, open_={})
    monkeypatch.setattr(sr, "labeled_open_prs", boom)
    nd = sr.needs({"phases": []}, NOW)
    assert nd["approval_prs_error"] == "OSError"
    assert any("取得できなかった" in line for line in sr.fmt_needs(nd))


def test_fmt_shows_days_waiting():
    nd = {"approval_prs": [{"repo": "o/engine", "number": 9, "title": "工場の変更", "url": "u", "days": 3.0}]}
    assert sr.fmt_needs(nd) == ["- 承認待ちの PR: [o/engine#9](u) 工場の変更（3 日）"]
