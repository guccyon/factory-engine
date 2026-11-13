"""QA 役（ADR 0028 の決定 2）の積み込み・記録・作用（qa_line.py）。GitHub は差し替え、DB は TEST_DATABASE_URL の Postgres（なければ飛ばす）。"""
import os
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
URL = os.environ.get("TEST_DATABASE_URL")

import qa_line  # noqa: E402  DB がなくても読める

REPO = "o/app"
ROOT = Path(__file__).resolve().parents[3]


def test_defs_copy_matches_definitions():
    defs = yaml.safe_load((ROOT / "definitions" / "qa.yaml").read_text(encoding="utf-8"))
    for k, v in qa_line.DEFS["act"].items():
        assert defs["act"][k] == v, k
    assert defs["worker"]["env_host"] == qa_line.DEFS["worker"]["env_host"]
    assert defs["clock"] == qa_line.DEFS["clock"]
    assert set(qa_line.DECISION_JA) == set(defs["decisions"])


def test_dispatcher_and_factoryctl_know_the_kind():
    src = (ROOT / "orchestrator" / "dispatcher" / "dispatcher.py").read_text(encoding="utf-8")
    assert '"/usr/local/bin/qa.sh"' in src and "qa_line.record(" in src
    import factoryctl  # noqa: F401  PIPED_KINDS（積み直しで入力を引き継ぐ）
    assert "qa" in factoryctl.PIPED_KINDS
    assert "qa.sh" in (ROOT / "orchestrator" / "worker" / "Dockerfile").read_text(encoding="utf-8")
    agents = yaml.safe_load((ROOT / "definitions" / "agents.yaml").read_text(encoding="utf-8"))
    assert agents["steps"]["qa"]["mode"] == "agentic"


class FakeGH:
    def __init__(self, existing=None, defects=None):
        self.calls, self.n, self.existing, self.defects = [], 100, existing, defects or []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET" and "factory:qa-defect" in path:
            return self.defects
        if method == "GET":
            return [{"number": self.existing}] if self.existing else []
        self.n += 1
        return {"number": self.n, "html_url": f"https://github.com/{REPO}/issues/{self.n}"}


def result():
    return {"kind": "qa", "ok": True, "sha": "abc123", "env": "staging", "release_id": "r7", "model": "m",
            "verdicts": [
                {"story_id": "s-pass", "decision": "pass", "confidence": 0.9, "steps": [{"n": 1, "result": "ok"}],
                 "evidence": ["01-a.png"], "interpretation": "通った", "defects": [],
                 "flags": [{"viewpoint": "tone", "where": "ホーム", "note": "硬い"}]},
                {"story_id": "s-hold", "decision": "hold", "confidence": 0.7, "steps": [{"n": 1, "result": "ng", "saw": "出ない"}],
                 "evidence": ["一覧が古い"], "interpretation": "止めた",
                 "defects": [{"title": "一覧が古い", "step": 1, "viewpoint": "flow", "severity": "s2", "actual": "出ない", "reproduce": ["開く"]},
                             {"title": "経緯が出ない", "step": 1, "viewpoint": "flow", "severity": "s3", "actual": "x", "known_gap": True}]},
                {"story_id": "s-blocked", "decision": "blocked", "confidence": 0.5, "steps": [{"n": 1, "result": "skipped"}],
                 "evidence": [], "interpretation": "翌日をまたげない", "defects": []}]}


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


def test_enqueue_makes_fixed_issue_and_job(cur):
    from psycopg.types.json import Jsonb
    cur.execute("insert into events(name, payload) values ('release.touchable', %s)",
                (Jsonb({"release_id": "r7", "env": "staging", "accounts": [{"label": "本部", "value": "mock.headquarters.x"}]}),))
    cur.execute("insert into events(name, payload) values ('release.deployed', %s)", (Jsonb({"repo": REPO, "env": "staging", "release_id": "r7", "ok": True}),))
    cur.execute("insert into events(name, payload) values ('release.built', %s)", (Jsonb({"repo": REPO, "release_id": "r7", "commit": "c0ffee1"}),))
    gh = FakeGH()
    got = qa_line.enqueue(cur, gh, REPO, stories=["s-pass"], priority="p0", env_host="h")
    assert got["issue"] == 101 and got["release_id"] == "r7"
    assert gh.calls[1][2]["labels"] == ["factory:qa"]
    cur.execute("select kind, priority, issue_number, correlation_id from jobs")
    j = cur.fetchone()
    assert j["kind"] == "qa" and j["issue_number"] == 101 and j["priority"] == qa_line.ops.LEVELS["p0"][0]
    cur.execute("select payload from events where name='job.enqueued'")
    inp = cur.fetchone()["payload"]["input"]
    assert inp == {"env": "staging", "env_host": "h", "release_id": "r7", "commit": "c0ffee1", "stories": ["s-pass"],
                   "touchable": {"accounts": [{"label": "本部", "value": "mock.headquarters.x"}], "note": None}}
    import psycopg
    with pytest.raises(psycopg.errors.UniqueViolation):   # 同じ固定 Issue に動いているジョブがあれば積まない
        qa_line.enqueue(cur, FakeGH(existing=101), REPO)


def job(cur, n):
    for _ in range(n):
        cur.execute("insert into jobs(correlation_id, repo, issue_number, enqueued_by, kind) values ('c', %s, 5, 't', 'qa') returning id", (REPO,))
        cur.execute("update jobs set state='succeeded' where id=%s", (cur.fetchone()["id"],))


def test_record_and_act(cur):
    job(cur, 9)
    put = {}
    keys = qa_line.store_files(lambda k, d, t: put.__setitem__(k, d), 9, {"01-a.png": b"x", "../evil.png": b"y"})
    assert keys == {"01-a.png": "qa/9/01-a.png"} and list(put) == ["qa/9/01-a.png"]
    recorded = qa_line.record(cur, REPO, "github:o/app#5", 9, result(), keys)
    assert [r["verdict"]["decision"] for r in recorded] == ["pass", "hold", "blocked"]
    assert recorded[0]["verdict"]["files"] == {"01-a.png": "qa/9/01-a.png"}
    qa_line.record(cur, REPO, "github:o/app#5", 9, result(), keys)   # 同じジョブの記録し直しで qa.verdict は増えない
    cur.execute("select count(*) as n from events where name='qa.verdict'")
    assert cur.fetchone()["n"] == 3
    gh, said = FakeGH(), []
    out = qa_line.act(cur, gh, lambda repo, issue, text: said.append((issue, text)), REPO, 5, "github:o/app#5", 9, result(), recorded, keys)
    posts = [c for c in gh.calls if c[0] == "POST" and c[1].endswith("/issues")]
    assert len(posts) == 1 and posts[0][2]["title"] == "QA: 一覧が古い"   # known_gap の Defect は起票しない
    assert posts[0][2]["labels"] == ["factory:qa-defect", "factory:fix-proposed"] and "s-hold" in posts[0][2]["body"]
    assert out["filed"] == [f"https://github.com/{REPO}/issues/101"]
    issue, text = [x for x in said if x[0] == 5][0]   # 101 にはメジャー不具合（S2）の直しの知らせ（ADR 0053）
    assert issue == 5 and "🛑 `s-hold`" in text and "known_gaps" in text and "硬い" in text and "⏸ `s-blocked`" in text
    cur.execute("select payload from events where name='qa.defect_filed'")
    assert cur.fetchone()["payload"]["severity"] == "s2"
    assert [r["story_id"] for r in qa_line.latest(cur, REPO)] == ["s-blocked", "s-hold", "s-pass"]
    # Defect の型（ADR 0028 の決定 5）: 起票したものも known_gaps のものも found_in=qa で残す。もう 1 度 act しても増えない
    cur.execute("select payload from events where name='defect.recorded' order by id")
    ds = [r["payload"] for r in cur.fetchall()]
    assert [(d["found_in"], d["title"], d["issue"]) for d in ds] == [("qa", "一覧が古い", f"https://github.com/{REPO}/issues/101"),
                                                                      ("qa", "経緯が出ない", None)]
    assert ds[0]["release_id"] == "r7" and ds[0]["viewpoint"] == "flow" and ds[0]["change"] is None and ds[0]["reproduce"] == ["開く"]


def test_act_caps_defects(cur):
    job(cur, 1)
    r = result()
    r["verdicts"][1]["defects"] = [{"title": f"d{i}", "viewpoint": "flow", "severity": "s3", "actual": "x"} for i in range(7)]
    recorded = qa_line.record(cur, REPO, "c", 1, r, {})
    gh, said = FakeGH(), []
    out = qa_line.act(cur, gh, lambda *a: said.append(a), REPO, 5, "c", 1, r, recorded, {})
    assert len(out["filed"]) == 5 and len(out["skipped"]) == 2 and "上限" in said[0][2]


def test_act_does_not_refile_same_story_and_step(cur):
    job(cur, 2)
    r = result()
    body = qa_line.defect_body(REPO, {**r["verdicts"][1], "env": "staging", "release_id": "r7"}, r["verdicts"][1]["defects"][0], 1, 5)
    assert qa_line.STORY_RE.search(body).group(1) == "s-hold" and qa_line.STEP_RE.search(body).group(1) == "1"
    r["verdicts"][1]["defects"].append({"title": "言い方が違う同じ不具合", "step": 1, "viewpoint": "flow", "severity": "s2", "actual": "y"})
    recorded = qa_line.record(cur, REPO, "c", 2, r, {})
    gh, said = FakeGH(defects=[{"number": 50, "html_url": f"https://github.com/{REPO}/issues/50", "body": body}]), []
    out = qa_line.act(cur, gh, lambda *a: said.append(a), REPO, 5, "c", 2, r, recorded, {})
    assert out["filed"] == [] and out["seen"] == [f"https://github.com/{REPO}/issues/50"] * 2
    assert not [c for c in gh.calls if c[0] == "POST" and c[1].endswith("/issues")]   # 起票し直さない
    assert [a[1] for a in said if a[1] == 5] == [5] and any("また止まりました" in a[2] for a in said if a[1] == 50)
    cur.execute("select count(*) as n from events where name='qa.defect_seen'")
    assert cur.fetchone()["n"] == 2
    cur.execute("select count(*) as n from events where name='defect.recorded' and payload->>'issue' like '%/issues/50'")
    assert cur.fetchone()["n"] == 1   # 同じ Issue に当たった Defect は 1 件として数える
    # 同じ回の中でも、同じ歩の 2 件目は 1 件目にまとめる
    recorded2 = qa_line.record(cur, REPO, "c", 1, r, {})
    gh2 = FakeGH()
    out2 = qa_line.act(cur, gh2, lambda *a: None, REPO, 5, "c", 1, r, recorded2, {})
    assert len(out2["filed"]) == 1 and len(out2["seen"]) == 1
