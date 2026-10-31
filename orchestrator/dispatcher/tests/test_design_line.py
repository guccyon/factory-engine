"""デザインの線の制御プレーン側（ADR 0025）: 案のファイルの受け取り、検査役の判断に従った作用、人の選択、Issue への引き渡し。"""
import base64
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
pytest.importorskip("psycopg")
import design_line as dl  # noqa: E402

URL = os.environ.get("TEST_DATABASE_URL")
REPO, ISSUE, CORR = "o/r", 5, "github:o/r#5"
SPEC = {"ticket": "o/r#5", "screens": [{"id": "s", "purpose": "p", "new": False, "elements": ["題"], "behaviors": ["送る前に確認"]}],
        "states": ["失敗"], "design_system_constraints": [], "design_system_gaps": [], "brand_impact": False,
        "variants": [{"id": "a", "name": "確認の段", "tradeoff": "誤送信に強い"}, {"id": "b", "name": "並べる", "tradeoff": "見える"}],
        "recommended": "a", "recommended_reason": "取り消せない操作"}


# ------------------------------------------------------------------ DB の要らないもの
def test_extract_files_keeps_only_valid_names():
    b = base64.b64encode(b"<p>").decode()
    files, rest = dl.extract_files([f"FACTORY_FILE:a.html:{b}\n", "FACTORY_FILE:../x.html:AAAA\n", "FACTORY_FILE:b.png:!!\n",
                                    "[ui_design] hello\n", "FACTORY_RESULT:{}\n"])
    assert files == {"a.html": b"<p>"} and rest == ["[ui_design] hello\n", "FACTORY_RESULT:{}\n"]


def test_replace_section_is_idempotent_and_keeps_human_text():
    sec = dl.issue_section(ISSUE, SPEC, "b", "human", 9, "<p>案</p>")
    assert "依存: #9" in sec and "specs/ux/issue-5.yaml" in sec and "id: b" in sec and "recommended" not in sec and "<p>案</p>" in sec
    body = dl.replace_section("人の本文\n", sec)
    assert body.startswith("人の本文\n\n" + dl.MARK)
    again = dl.replace_section(body, dl.issue_section(ISSUE, SPEC, "a", "design_inspector", None, None))
    assert again.count(dl.MARK) == 1 and "id: a" in again and "依存" not in again and again.startswith("人の本文")
    assert dl.replace_section("", sec) == sec + "\n"


def test_large_mock_is_not_inlined():
    assert "<details>" not in dl.issue_section(ISSUE, SPEC, "a", "human", None, "x" * (dl.MAX_HTML_INLINE + 1))


# ------------------------------------------------------------------ DB を使うもの
needs_db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL がない")


@pytest.fixture()
def cur():
    import psycopg
    from psycopg.rows import dict_row
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


class GH:
    def __init__(self, body="## 受け入れ条件\n- 送る前に確認\n"):
        self.calls, self.body, self.comments = [], body, []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET":
            return {"body": self.body}
        if method == "PATCH":
            self.body = body["body"]
        if method == "POST" and path.endswith("/issues"):
            return {"number": 9}
        return {}

    def comment(self, repo, n, text):
        self.comments.append(text)

    def labels(self):
        return [(m, p.rsplit("/", 1)[-1] if m == "DELETE" else (b or {}).get("labels")) for m, p, b in self.calls if "/labels" in p]


def job(cur, kind):
    cur.execute("insert into jobs(correlation_id, repo, issue_number, enqueued_by, kind, state) values (%s,%s,%s,'test',%s,'succeeded') returning id",
                (CORR, REPO, ISSUE, kind))
    return cur.fetchone()["id"]


def draft(cur, spec=SPEC, round_=1):
    store = {}
    jid = job(cur, "ui_design")
    keys = dl.store_files(lambda k, d, ct: store.__setitem__(k, d), jid, {"a.html": b"<p>a</p>", "a.png": b"png", "b.html": b"<p>b</p>"})
    aid = dl.record_draft(cur, REPO, ISSUE, CORR, jid, {"uxspec": spec, "round": round_, "cost_usd": 0.4}, keys)
    return aid, store


def review(cur, gh, aid, decision, store, **verdict):
    inp = dl.review_input(cur, store.__getitem__, {"design_artifact_id": aid})
    res = {"decision": decision, "human_reasons": verdict.pop("reasons", []),
           "verdict": {"decision": decision, "chosen_variant": "a", "confidence": 0.9, "findings": [], "evidence": ["mocks/a.png"],
                       "interpretation": "i", **verdict}}
    jid = job(cur, "ui_design_review")
    vid = dl.record_review(cur, REPO, ISSUE, CORR, jid, inp, res)
    dl.act_on_review(cur, REPO, ISSUE, CORR, jid, inp, res, vid, gh, gh.comment, "tier-large", store.__getitem__)
    return inp, vid


@needs_db
def test_review_input_carries_files(cur):
    aid, store = draft(cur)
    inp = dl.review_input(cur, store.__getitem__, {"design_artifact_id": aid})
    assert inp["round"] == 1 and inp["uxspec"]["recommended"] == "a"
    assert base64.b64decode(inp["files"]["a.png"]) == b"png" and set(inp["files"]) == {"a.html", "a.png", "b.html"}
    assert dl.enqueue(cur, REPO, ISSUE, CORR, "ui_design_review", "tier-judge", {"design_artifact_id": aid})
    assert dl.enqueue(cur, REPO, ISSUE, CORR, "ui_design_review", "tier-judge", {}) is None   # 同じ段のジョブは 1 本


@needs_db
def test_approve_hands_off_to_planned(cur):
    aid, store = draft(cur)
    gh = GH()
    review(cur, gh, aid, "approve", store)
    assert gh.body.startswith("## 受け入れ条件") and dl.MARK in gh.body and "<p>a</p>" in gh.body
    assert ("POST", ["factory:planned"]) in gh.labels() and ("DELETE", "factory%3Arunning") in gh.labels()
    cur.execute("select payload from events where name='ui_design.handed_off'")
    p = cur.fetchone()["payload"]
    assert p["variant"] == "a" and p["decided_by"] == "design_inspector" and p["gaps_issue"] is None
    cur.execute("select payload from artifacts where type='DesignVerdict'")
    v = cur.fetchone()["payload"]
    assert v["evidence"] == ["mocks/a.png"] and v["score"]["self_reported"] == 0.9 and v["decided_by"] == "design_inspector"
    n = len(gh.calls)
    dl.handoff(cur, gh, gh.comment, REPO, ISSUE, CORR, dl.load_design(cur, aid), "a", "human", None)
    assert len(gh.calls) == n   # 同じ案は 1 度だけ


@needs_db
def test_gaps_become_a_dependency(cur):
    aid, store = draft(cur, {**SPEC, "design_system_gaps": ["一覧の空の表示"]})
    gh = GH()
    review(cur, gh, aid, "approve", store)
    created = [b for m, p, b in gh.calls if m == "POST" and p == "repos/o/r/issues"]
    assert len(created) == 1 and "一覧の空の表示" in created[0]["body"] and created[0]["labels"] == ["factory:planned"]
    assert "依存: #9" in gh.body


@needs_db
def test_revise_requeues_designer_with_findings(cur):
    aid, store = draft(cur)
    gh = GH()
    f = {"severity": "must", "viewpoint": "states", "variant": "a", "evidence": "失敗がない", "fix": "足す"}
    review(cur, gh, aid, "revise", store, findings=[f])
    cur.execute("select j.kind, j.attempt, e.payload->'input' as inp from jobs j join events e on e.job_id=j.id and e.name='job.enqueued' where j.state='queued'")
    j = cur.fetchone()
    assert j["kind"] == "ui_design" and j["attempt"] == 2 and j["inp"]["round"] == 2 and j["inp"]["findings"] == [f]
    assert j["inp"]["previous_uxspec"]["recommended"] == "a"
    assert "直しを求めました" in gh.comments[0] and not gh.labels()


@needs_db
def test_human_choice_flow(cur):
    aid, store = draft(cur, {**SPEC, "brand_impact": True})
    gh = GH()
    review(cur, gh, aid, "human", store, reasons=["brand_impact"], human_reason="生徒が見る画面")
    assert ("POST", ["factory:design-choice"]) in gh.labels()
    assert "ブランドと印象に効く画面" in gh.comments[0] and "factoryctl design pick o/r#5" in gh.comments[0]
    with pytest.raises(ValueError):
        dl.choose(cur, REPO, ISSUE, "z", "builder")
    c = dl.choose(cur, REPO, ISSUE, "b", "builder", "余白が好き")
    assert c["agree"] is False and c["recommended"] == "a"
    with pytest.raises(ValueError):
        dl.choose(cur, REPO, ISSUE, "a", "builder")   # 1 度だけ
    assert dl.apply_choices(cur, gh, gh.comment, store.__getitem__) == 1
    assert "id: b" in gh.body and "<p>b</p>" in gh.body and "人が選んだ" in gh.body
    assert ("DELETE", "factory%3Adesign-choice") in gh.labels()
    assert dl.apply_choices(cur, gh, gh.comment, store.__getitem__) == 0
