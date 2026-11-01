"""デザインのレビューページ（ADR 0026）: パッケージの描画、外部の文のエスケープ、判断できる状態だけ操作を出すこと、受信トレイへの積み下ろし。"""
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
pytest.importorskip("psycopg")
import design_line as dl  # noqa: E402
import review_page as rp  # noqa: E402

URL = os.environ.get("TEST_DATABASE_URL")
REPO, ISSUE, CORR = "o/r", 5, "github:o/r#5"
SPEC = {"ticket": "o/r#5",
        "prd": {"goal": "本部が生徒に一斉にお知らせを届けたい", "success": "誤送信なく届く", "scope": "送信と確認", "source": "o/r#5"},
        "user_flow": [{"from": "お知らせ一覧", "action": "新規作成", "to": "送信画面"}, {"from": "送信画面", "action": "送る", "to": "確認"}],
        "rationale": {"why": "取り消せない操作なので確認を挟む", "rejected": [{"idea": "即送信", "reason": "誤送信に弱い"}],
                      "requirement_map": [{"requirement": "誤送信を防ぐ", "how": "確認の段"}]},
        "screens": [{"id": "send", "purpose": "お知らせを送る", "new": True, "transitions": {"entries": ["一覧"], "exits": ["送信後は一覧へ"]},
                     "elements": ["題", "本文"], "behaviors": ["送る前に確認を挟む"],
                     "microinteractions": [{"trigger": "送る", "rules": "二重押しを防ぐ", "feedback": "送信中の表示", "loops": "なし"}]}],
        "states": ["読み込み中", "失敗"], "design_system_constraints": ["specs/design/ の部品"], "design_system_gaps": [],
        "open_questions": ["予約送信は次の版"], "brand_impact": False, "recommended": "a", "recommended_reason": "誤送信に強い",
        "variants": [{"id": "a", "name": "確認の段", "tradeoff": "1 回余計に押す", "viewport": "desktop"},
                     {"id": "b", "name": "<script>alert(1)</script>", "tradeoff": "届く姿が見える"}]}


def ctx(choice_status="open", handed_off=None):
    return {"design": {"id": 7, "repo": REPO, "issue": ISSUE, "round": 1, "uxspec": SPEC, "files": {"a.png": "design/1/a.png", "a.html": "design/1/a.html"}},
            "verdict": {"id": 8, "inspector_decision": "human", "chosen_variant": "a", "score": {"self_reported": 0.75}, "human_reasons": ["new_screen"],
                        "interpretation": "新しい画面", "findings": [{"severity": "must", "viewpoint": "states", "variant": "b", "evidence": "空がない", "fix": "足す"}]},
            "choice": {"id": 3, "status": choice_status}, "handed_off": handed_off}


# ------------------------------------------------------------------ 描画（DB なし）
def test_render_shows_the_package_and_actions_when_open():
    page = rp.render(ctx(), "tok")
    for text in ("本部が生徒に一斉にお知らせを届けたい", "お知らせ一覧", "取り消せない操作なので確認を挟む", "即送信", "7 マイクロインタラクション",
                 "3 画面遷移", "予約送信は次の版", "判断待ち", "新しい画面", "この案で承認", "直しを頼む", "/review/7/image/a.png"):
        assert text in page, text
    assert "const CHOICE=3" in page and '"tok"' in page
    assert "a.html" not in page   # 案の HTML はこの origin では開かない


def test_render_escapes_designer_text():
    page = rp.render(ctx(), "tok")
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page


def test_render_is_read_only_once_decided():
    page = rp.render(ctx("chosen", {"variant": "a", "decided_by": "human"}), "tok")
    assert "決定済み" in page and "この案で承認" not in page and "const CHOICE=null" in page


def test_fmt_handles_nested_and_empty():
    assert rp.fmt([]) == '<span class="m">（なし）</span>'
    assert "<dt>a</dt><dd><ul><li>x</li></ul></dd>" in rp.fmt({"a": ["x"]})


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


class FakeInbox:
    def __init__(self):
        self.open, self.log = {}, []

    def request(self, cur, key, kind, who, title, *, url="", action="", correlation_id=None, by="factory"):
        self.log.append(("request", key, who, url))
        if key in self.open:
            return False
        self.open[key] = url
        return True

    def resolve(self, cur, key, by, outcome=""):
        self.log.append(("resolve", key, outcome))
        return self.open.pop(key, None) is not None


class GH:
    def __init__(self):
        self.body, self.comments = "## 受け入れ条件\n", []

    def __call__(self, method, path, body=None):
        if method == "GET":
            return {"body": self.body}
        if method == "PATCH":
            self.body = body["body"]
        return {}

    def comment(self, repo, n, text):
        self.comments.append(text)


def human_review(cur, gh):
    cur.execute("insert into jobs(correlation_id, repo, issue_number, enqueued_by, kind, state) values (%s,%s,%s,'test','ui_design','succeeded') returning id",
                (CORR, REPO, ISSUE))
    jid = cur.fetchone()["id"]
    store = {}
    keys = dl.store_files(lambda k, d, ct: store.__setitem__(k, d), jid, {"a.html": b"<p>a</p>", "a.png": b"png", "b.html": b"<p>b</p>"})
    aid = dl.record_draft(cur, REPO, ISSUE, CORR, jid, {"uxspec": SPEC, "round": 1}, keys)
    inp = dl.review_input(cur, store.__getitem__, {"design_artifact_id": aid})
    res = {"decision": "human", "human_reasons": ["new_screen"],
           "verdict": {"decision": "human", "chosen_variant": "a", "confidence": 0.75, "findings": [], "evidence": [], "interpretation": "i"}}
    vid = dl.record_review(cur, REPO, ISSUE, CORR, jid, inp, res)
    dl.act_on_review(cur, REPO, ISSUE, CORR, jid, inp, res, vid, gh, gh.comment, "tier-large", store.__getitem__)
    return aid, store


@needs_db
def test_human_route_puts_the_review_page_in_the_inbox_and_choice_clears_it(cur, monkeypatch):
    fake = FakeInbox()
    monkeypatch.setattr(dl, "inbox", fake)
    gh = GH()
    aid, store = human_review(cur, gh)
    assert fake.open == {f"design_review:{aid}": f"/review/{aid}"} and fake.log[0][2] == "po"
    assert f"/review/{aid}" in gh.comments[-1]
    c = rp.load(cur, aid)
    assert rp.status(c) == "open" and c["choice"]["status"] == "open" and c["verdict"]["inspector_decision"] == "human"
    page = rp.render(c, "tok")
    assert f"const CHOICE={c['choice']['id']}" in page and "本部が生徒に一斉に" in page
    dl.choose(cur, REPO, ISSUE, "a", "console:test")
    dl.apply_choices(cur, gh, gh.comment, store.__getitem__)
    assert fake.open == {} and fake.log[-1] == ("resolve", f"design_review:{aid}", "chosen:a")
    assert rp.status(rp.load(cur, aid)) == "decided"


@needs_db
def test_return_clears_the_inbox(cur, monkeypatch):
    fake = FakeInbox()
    monkeypatch.setattr(dl, "inbox", fake)
    aid, _ = human_review(cur, GH())
    dl.return_to_designer(cur, REPO, ISSUE, CORR, "どれも違う", "tier-large")
    assert fake.open == {} and fake.log[-1][2] == "returned"


@needs_db
def test_without_inbox_the_line_still_works(cur, monkeypatch):
    monkeypatch.setattr(dl, "inbox", None)
    aid, _ = human_review(cur, GH())
    assert rp.load(cur, aid)["choice"]["status"] == "open"


@needs_db
def test_load_returns_none_for_unknown(cur):
    assert rp.load(cur, 999) is None
