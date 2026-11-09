"""障害の重要度とエラーバジェット、QA 役の差し止め（ADR 0043 の O5、incident.py）。GitHub は差し替え、DB は TEST_DATABASE_URL の Postgres（なければ飛ばす）。"""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
URL = os.environ.get("TEST_DATABASE_URL")
if URL:   # dispatcher を読むための値（test_drain.py と同じ）
    for k, v in {"DATABASE_URL": URL, "GH_TOKEN": "x", "CLAUDE_CODE_OAUTH_TOKEN": "x", "OBJECTSTORE_ENDPOINT": "localhost:1",
                 "OBJECTSTORE_ACCESS_KEY": "x", "OBJECTSTORE_SECRET_KEY": "x", "NOTIFIER": "log", "JOB_DB_ENABLED": "0"}.items():
        os.environ.setdefault(k, v)

import incident  # noqa: E402
import qa_line  # noqa: E402

REPO = "o/app"
ROOT = Path(__file__).resolve().parents[3]
QA_CORR = f"github:{REPO}#5"
POLICY = {"provisional": True, "error_budget": {"period_days": 7, "allowed": {"s1": 0, "s2": 1, "s3": 5, "s4": 20}, "on_exceed": "知らせる"}}


def test_definitions_name_the_events_and_fields():
    ev = yaml.safe_load((ROOT / "definitions" / "events.yaml").read_text(encoding="utf-8"))
    text = yaml.safe_dump(ev, allow_unicode=True)
    for name in ("incident.opened", "incident.closed", "incident.budget_exceeded", "incident.drill_started", "incident.drill_ended",
                 incident.HOLD, incident.RELEASED):
        assert f"{name}:" in text, name
    qp = yaml.safe_load((ROOT / "definitions" / "quality_policy.yaml").read_text(encoding="utf-8"))
    assert {"severities", "error_budget", "s1"} <= set(qp["fields"])
    qa = yaml.safe_load((ROOT / "definitions" / "qa.yaml").read_text(encoding="utf-8"))
    assert tuple(qa["severities"]) == incident.SEVERITIES


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


def queue(cur, corr, priority=50, kind="implement"):
    cur.execute("insert into jobs(correlation_id, repo, issue_number, enqueued_by, kind, priority) values (%s,%s,%s,'t',%s,%s) returning id",
                (corr, REPO, int(corr.rsplit("#", 1)[1]), kind, priority))
    return cur.fetchone()["id"]


def test_open_incident_once_and_drill_mark(cur):
    a = incident.open_incident(cur, REPO, "u1", "s2", "一覧が古い", issue="u1")
    assert a and a["severity"] == "s2" and not a["drill"]
    assert incident.open_incident(cur, REPO, "u1", "s2", "一覧が古い") is None   # 同じ Defect は 1 件
    from psycopg.types.json import Jsonb
    cur.execute("insert into events(name, payload) values ('incident.drill_started', %s)", (Jsonb({"repo": REPO, "drill": "s1_data_leak", "by": "t"}),))
    b = incident.open_incident(cur, REPO, "u2", "s1", "他人のメールが見える")
    assert b["drill"] and b["drill_id"] == "s1_data_leak"
    cur.execute("select count(*) as n from artifacts where type='Incident'")
    assert cur.fetchone()["n"] == 2
    assert {i["incident_id"] for i in incident.open_incidents(cur, REPO)} == {f"{REPO}:u1", f"{REPO}:u2"}
    assert incident.close_incident(cur, f"{REPO}:u1", "人", "直った")
    assert not incident.close_incident(cur, f"{REPO}:u1", "人", "二度目")
    assert [i["incident_id"] for i in incident.open_incidents(cur, REPO)] == [f"{REPO}:u2"]


def test_hold_lets_only_s1_fix_and_qa_through_until_a_human_releases(cur, monkeypatch):
    normal = queue(cur, f"github:{REPO}#9")
    fix = queue(cur, f"github:{REPO}#101")
    qa = queue(cur, QA_CORR, kind="qa")
    urgent = queue(cur, f"github:{REPO}#7", priority=100)
    inc = incident.open_incident(cur, REPO, "u", "s1", "他人のメールが見える")
    assert incident.hold(cur, inc, qa_corr=QA_CORR, defect_corr=f"github:{REPO}#101")
    assert not incident.hold(cur, inc, qa_corr=QA_CORR, defect_corr=None)   # 同じ Incident で出し直さない
    st = incident.hold_status(cur)
    assert st["allowed"] == [f"github:{REPO}#101", QA_CORR] and st["incidents"][0]["severity"] == "s1"
    cur.execute("select count(*) as n from stoppages where key like 'qa_hold:%%' and ended_at is null")
    assert cur.fetchone()["n"] == 1
    monkeypatch.setenv("MAX_PARALLEL", "10")
    got = []
    while (j := incident.claim(cur)) is not None:
        got.append(j["id"])
    assert sorted(got) == sorted([fix, qa, urgent]) and normal not in got
    import dispatcher
    assert dispatcher._claim_line(cur) is None   # dispatcher も差し止めの間はふつうのジョブを取り出さない
    for by in ("qa", "supervisor:x", ""):
        with pytest.raises(ValueError):
            incident.release(cur, by, "直した")
    with pytest.raises(ValueError):
        incident.release(cur, "console:人", "  ")
    assert incident.release(cur, "console:人", "演習を終えた")
    assert not incident.held(cur) and not incident.release(cur, "console:人", "二度目")
    cur.execute("select count(*) as n from stoppages where key like 'qa_hold:%%' and ended_at is null")
    assert cur.fetchone()["n"] == 0
    assert dispatcher._claim_line(cur)["id"] == normal


def test_budget_counts_without_drills_and_tells_once(cur):
    incident.open_incident(cur, REPO, "a", "s2", "a")
    assert incident.check_budget(cur, REPO, POLICY) == []
    incident.open_incident(cur, REPO, "b", "s2", "b")
    from psycopg.types.json import Jsonb
    cur.execute("insert into events(name, payload) values ('incident.drill_started', %s)", (Jsonb({"repo": REPO, "drill": "d", "by": "t"}),))
    incident.open_incident(cur, REPO, "c", "s1", "演習")
    b = incident.budget(cur, REPO, POLICY)
    assert b["severities"]["s2"] == {"count": 2, "allowed": 1} and b["severities"]["s1"]["count"] == 0 and b["over"] == ["s2"]
    assert incident.check_budget(cur, REPO, POLICY) == ["s2"]
    assert incident.check_budget(cur, REPO, POLICY) == []   # 期間に 1 回
    assert incident.budget(cur, REPO, {}) is None
    later = datetime(2099, 1, 1, tzinfo=timezone.utc)
    assert incident.budget(cur, REPO, POLICY, later)["over"] == []   # 期間の外は数えない


class FakeGH:
    def __init__(self):
        self.calls, self.n = [], 100

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET":
            return []
        self.n += 1
        return {"number": self.n, "html_url": f"https://github.com/{REPO}/issues/{self.n}"}


def verdicts(severity):
    return {"kind": "qa", "ok": True, "sha": "abc", "env": "staging", "release_id": "r7", "model": "m",
            "verdicts": [{"story_id": "s", "decision": "hold", "confidence": 0.8, "steps": [{"n": 1, "result": "ng", "saw": "x"}],
                          "evidence": ["x"], "interpretation": "止めた",
                          "defects": [{"title": "他人のメールが見える", "step": 1, "viewpoint": "authz", "severity": severity, "actual": "x"},
                                      {"title": "既知", "step": 2, "viewpoint": "flow", "severity": "s1", "actual": "x", "known_gap": True}]}]}


def test_qa_s1_defect_opens_incident_holds_line_and_marks_p0(cur):
    cur.execute("insert into jobs(correlation_id, repo, issue_number, enqueued_by, kind) values (%s, %s, 5, 't', 'qa')", (QA_CORR, REPO))
    gh, said = FakeGH(), []
    res = verdicts("s1")
    recorded = qa_line.record(cur, REPO, QA_CORR, 1, res, {})
    qa_line.act(cur, gh, lambda r, n, t: said.append((n, t)), REPO, 5, QA_CORR, 1, res, recorded, {})
    st = incident.hold_status(cur)
    assert st and st["incidents"][0]["issue"] == f"https://github.com/{REPO}/issues/101"   # known_gap の S1 は Incident にしない
    assert f"github:{REPO}#101" in st["allowed"] and QA_CORR in st["allowed"]
    assert ("POST", f"repos/{REPO}/issues/101/labels", {"labels": ["factory:priority:p0"]}) in gh.calls
    assert any(n == 101 and "差し止め" in t for n, t in said)
    cur.execute("select payload from events where name='incident.opened'")
    rows = [r["payload"] for r in cur.fetchall()]
    assert len(rows) == 1 and rows[0]["severity"] == "s1" and rows[0]["release_id"] == "r7" and rows[0]["detected_by"] == "qa"


def test_qa_s2_defect_is_incident_without_hold(cur):
    cur.execute("insert into jobs(correlation_id, repo, issue_number, enqueued_by, kind) values (%s, %s, 5, 't', 'qa')", (QA_CORR, REPO))
    gh = FakeGH()
    res = verdicts("s2")
    recorded = qa_line.record(cur, REPO, QA_CORR, 1, res, {})
    qa_line.act(cur, gh, lambda *a: None, REPO, 5, QA_CORR, 1, res, recorded, {})
    assert not incident.held(cur)
    assert [i["severity"] for i in incident.open_incidents(cur, REPO)] == ["s2"]
    assert not any(c[1].endswith("/labels") for c in gh.calls)


def test_drill_does_not_plant_while_qa_is_busy(cur, capsys):
    """QA のジョブは製品ごとに 1 本。動いていれば仕込まずに断る（仕込んだのに探索を積めない、を避ける）"""
    import factoryctl
    queue(cur, QA_CORR, kind="qa")
    assert factoryctl.incident_cmd(cur, ["factoryctl", "incident", "drill", REPO]) == 1
    assert "仕込みはしていない" in capsys.readouterr().out
    cur.execute("select count(*) as n from events where name in ('release.drill_requested','incident.drill_started')")
    assert cur.fetchone()["n"] == 0
