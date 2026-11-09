"""実装後の画面のキャプチャを PR と違和感の印につなぐ（ADR 0025 の 4）。

形の読み取りは DB なしで、記録と周回は TEST_DATABASE_URL の Postgres で確かめる（なければ飛ばす）。
"""
import io
import json
import os
import sys
import urllib.error
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
pytest.importorskip("psycopg")

from psycopg.types.json import Jsonb  # noqa: E402

import choice  # noqa: E402
import exit_capture as ec  # noqa: E402
import inbox  # noqa: E402
import learn  # noqa: E402
import notify_inbox  # noqa: E402

URL = os.environ.get("TEST_DATABASE_URL")
REPO, PR, SHA = "o/r", 42, "abcdef1234567890"
PR_URL = f"https://github.com/{REPO}/pull/{PR}"
ART = f"https://github.com/{REPO}/actions/runs/99/artifacts/777"
MANIFEST = {
    "app": "admin-web",
    "changed": [{"id": "hq-home", "title": "本部のホーム", "diff_ratio": 0.012, "base": "images/hq-home.base.png", "head": "images/hq-home.head.png",
                 "diff": "images/hq-home.diff.png"},
                {"id": "hq-coach-reviews-max-rating-empty", "title": "評価の絞り込み（空）", "diff_ratio": 0.3,
                 "base": "images/hq-coach-reviews-max-rating-empty.base.png", "head": "images/hq-coach-reviews-max-rating-empty.head.png"}],
    "added": [{"id": "hq-students", "title": "生徒の一覧", "head": "images/hq-students.head.png"}],
    "removed": [],
    "failed": [{"id": "coach-live", "title": "ライブ"}],
    "unchanged": [{"id": "login", "title": "ログイン"}],
}


def make_zip(manifest=MANIFEST, extra=None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        for kind in ("changed", "added", "removed"):
            for i in manifest.get(kind) or []:
                for k in ("base", "head", "diff"):
                    if i.get(k):
                        z.writestr(i[k], b"\x89PNG-" + i["id"].encode() + k.encode())
        for n, d in (extra or {}).items():
            z.writestr(n, d)
    return buf.getvalue()


# ------------------------------------------------------------------ DB なし
def test_capture_statuses_takes_newest_per_app():
    calls = []

    def gh(method, path, body=None):
        calls.append(path)
        return [{"context": "画面のキャプチャ / admin-web", "target_url": ART, "description": "見た目が変わった画面 3"},
                {"context": "画面のキャプチャ / admin-web", "target_url": "https://old", "description": "古い"},
                {"context": "ci / test", "target_url": "x"},
                {"context": "画面のキャプチャ / ../etc", "target_url": "x"}]
    assert ec.capture_statuses(gh, REPO, SHA) == [{"app": "admin-web", "target_url": ART, "description": "見た目が変わった画面 3"}]
    assert calls == [f"repos/{REPO}/commits/{SHA}/statuses?per_page=100"]


def test_artifact_id_only_from_same_repo():
    assert ec.artifact_id(REPO, ART) == 777
    assert ec.artifact_id("x/y", ART) is None
    assert ec.artifact_id(REPO, "https://evil.example/actions/runs/1/artifacts/2") is None


def test_read_zip_ignores_unexpected_files():
    m, images = ec.read_zip(make_zip(extra={"../evil.png": b"x", "images/a b.head.png": b"x", "index.html": b"<html>"}))
    assert m["app"] == "admin-web"
    assert "images/hq-home.head.png" in images and "../evil.png" not in images and "images/a b.head.png" not in images and "index.html" not in images
    with pytest.raises(ValueError):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("index.html", "x")
        ec.read_zip(buf.getvalue())


def test_screens_order_changed_by_ratio_then_added():
    s = ec.screens(MANIFEST)
    assert [(x["id"], x["kind"]) for x in s] == [("hq-coach-reviews-max-rating-empty", "changed"), ("hq-home", "changed"), ("hq-students", "added")]
    assert s[2]["diff_ratio"] is None and s[2]["image"] == "images/hq-students.head.png"


def test_pr_section_replaces_only_its_part():
    results = [{"app": "admin-web", "artifact_url": ART, "unchanged": 1, "failed": [{"id": "coach-live", "title": "ライブ"}],
                "screens": [{"id": "hq-home", "title": "本部|ホーム", "kind": "changed", "diff_ratio": 0.012}]}]
    sec = ec.pr_section(results, SHA)
    assert "変わった 1・撮れなかった 1・変わらない 1" in sec and "1.20%" in sec and "本部\\|ホーム" in sec and ART in sec
    body = ec.replace_section("## 概要\n人が書いた", sec)
    assert body.startswith("## 概要\n人が書いた\n\n" + ec.MARK)
    again = ec.replace_section(body, ec.pr_section([], "1111111aaaa"))
    assert again.count(ec.MARK) == 1 and "1111111" in again and "hq-home" not in again and again.startswith("## 概要\n人が書いた")


# ------------------------------------------------------------------ DB あり
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


def events(cur, name):
    cur.execute("select payload from events where name=%s order by id", (name,))
    return [r["payload"] for r in cur.fetchall()]


def factory_pr(cur, sha=SHA, corr=f"github:{REPO}#40"):
    cur.execute("insert into artifacts(type, correlation_id, produced_by, payload) values ('PR', %s, 'implement', %s)", (corr, Jsonb({"pr_url": PR_URL})))
    cur.execute("insert into events(name, correlation_id, payload) values ('pr.head_updated', %s, %s)", (corr, Jsonb({"pr_url": PR_URL, "head_sha": sha})))


class FakeGH:
    def __init__(self, statuses, body="## 概要\n実装役が書いた"):
        self.statuses, self.body, self.patched = statuses, body, []

    def __call__(self, method, path, body=None):
        if path.startswith(f"repos/{REPO}/commits/"):
            return self.statuses
        if method == "GET" and path == f"repos/{REPO}/pulls/{PR}":
            return {"body": self.body}
        if method == "PATCH":
            self.body = body["body"]
            self.patched.append(body["body"])
            return {}
        raise AssertionError(path)


def test_step_collects_once_writes_pr_and_inbox_then_flag_reaches_learn(cur):
    factory_pr(cur)
    gh = FakeGH([{"context": "画面のキャプチャ / admin-web", "target_url": ART, "description": "見た目が変わった画面 3・撮れなかった画面 1"}])
    stored, downloads = {}, []

    def download(repo, aid):
        downloads.append((repo, aid))
        return make_zip()
    assert ec.step(cur, gh, download, lambda k, d, t: stored.__setitem__(k, d), [REPO], log=lambda *a: None) == 1
    assert downloads == [(REPO, 777)]
    # 画像は choices/ の下（操業コンソールが中継できる場所）
    assert sorted(stored) == sorted(ec.image_key(REPO, PR, SHA, "admin-web", i) for i in ("hq-home", "hq-coach-reviews-max-rating-empty", "hq-students"))
    assert all(choice.image_key_ok(k) for k in stored)
    # PR の本文に節が付き、人の書いた部分は残る
    assert gh.body.startswith("## 概要\n実装役が書いた") and ec.MARK in gh.body and "hq-students" in gh.body and "coach-live" in gh.body
    # 受信トレイに「実装後の画面」としてデザインのチーフの出番で出る
    [item] = [i for i in inbox.items(cur) if i["kind"] == "choice"]
    assert item["key"] == "choice:exit_capture" and item["who"] == "design_chief" and item["title"] == f"実装後の画面: 1 件（#{PR}）" and "見た" in item["action"]
    [c] = choice.open_choices(cur)
    assert c["kind"] == "exit_capture" and [o["id"] for o in c["options"]] == ["s1", "s2", "s3"]
    assert c["subject"]["pr_url"] == PR_URL and c["subject"]["app"] == "admin-web" and c["subject"]["artifact_url"] == ART
    assert choice.choose(cur, c["id"], "s1", "t")[0] is False   # 選ぶものではない
    # 同じ head はもう取りに行かない
    assert ec.step(cur, gh, download, lambda *a: None, [REPO], log=lambda *a: None) == 0 and len(downloads) == 1
    # 違和感の印 → learn の教師信号 taste_flag
    assert choice.flag(cur, c["id"], "評価の空の表示が寂しい", "console:t", "s1") == (True, "flagged")
    [f] = events(cur, "taste.flagged")
    assert f["option_label"] == "評価の絞り込み（空）" and f["subject"]["pr_url"] == PR_URL
    now = datetime.now(timezone.utc) + timedelta(minutes=1)
    [sig] = [s for s in learn.collect_signals(cur, now - timedelta(days=1), now) if s["kind"] == "taste_flag"]
    assert sig["signature"] == "taste:dev.design.screen:unclassified" and sig["defect"]["evidence"] == "評価の空の表示が寂しい" and sig["pr_url"] == PR_URL and "評価の絞り込み（空）" in sig["defect"]["location"]
    retro = learn.retrospect(cur, now=now)
    [cl] = [x for x in retro["clusters"] if x["signature"].startswith("taste:")]
    assert cl["by_kind"] == {"taste_flag": 1} and cl["evidence"][0]["evidence"] == "評価の空の表示が寂しい"
    # 見たで閉じる。印のない画面が負例の材料
    assert choice.mark_seen(cur, c["id"], "console:t") == (True, "seen")
    [seen] = events(cur, "choice.seen")
    assert seen["flagged"] == ["s1"] and seen["unflagged"] == ["s2", "s3"]
    assert choice.open_choices(cur) == []


def test_new_head_supersedes_previous_choice(cur):
    factory_pr(cur)
    gh = FakeGH([{"context": "画面のキャプチャ / admin-web", "target_url": ART, "description": "d"}])
    ec.step(cur, gh, lambda r, a: make_zip(), lambda *a: None, [REPO], log=lambda *a: None)
    [old] = choice.open_choices(cur)
    cur.execute("insert into events(name, correlation_id, payload) values ('pr.head_updated', %s, %s)",
                (f"github:{REPO}#40", Jsonb({"pr_url": PR_URL, "head_sha": "ffff000011112222"})))
    ec.step(cur, gh, lambda r, a: make_zip(), lambda *a: None, [REPO], log=lambda *a: None)
    [new] = choice.open_choices(cur)
    assert new["id"] != old["id"] and new["subject"]["head_sha"] == "ffff000011112222"
    assert events(cur, "choice.closed")[0]["status"] == "superseded"
    assert "ffff000" in gh.body and gh.body.count(ec.MARK) == 1


def test_no_changed_screens_writes_pr_but_no_inbox(cur):
    factory_pr(cur)
    gh = FakeGH([{"context": "画面のキャプチャ / student-app", "target_url": ART, "description": "見た目が変わった画面なし"}])
    m = {"app": "student-app", "changed": [], "added": [], "removed": [], "failed": [], "unchanged": [{"id": "home", "title": "ホーム"}]}
    assert ec.step(cur, gh, lambda r, a: make_zip(m), lambda *a: None, [REPO], log=lambda *a: None) == 1
    assert choice.open_choices(cur) == [] and "見た目が変わった画面なし" in gh.body


def test_expired_artifact_is_recorded_and_not_retried(cur):
    factory_pr(cur)
    gh = FakeGH([{"context": "画面のキャプチャ / admin-web", "target_url": ART, "description": "d"}])
    n = []

    def gone(repo, aid):
        n.append(aid)
        raise urllib.error.HTTPError("u", 410, "Gone", {}, None)
    assert ec.step(cur, gh, gone, lambda *a: None, [REPO], log=lambda *a: None) == 0
    assert ec.step(cur, gh, gone, lambda *a: None, [REPO], log=lambda *a: None) == 0
    assert n == [777] and "410" in events(cur, "capture.collected")[0]["error"] and gh.patched == []


def test_transient_failure_is_retried(cur):
    factory_pr(cur)
    gh = FakeGH([{"context": "画面のキャプチャ / admin-web", "target_url": ART, "description": "d"}])

    def flaky(repo, aid):
        raise urllib.error.HTTPError("u", 502, "Bad Gateway", {}, None)
    ec.step(cur, gh, flaky, lambda *a: None, [REPO], log=lambda *a: None)
    assert events(cur, "capture.collected") == []
    assert ec.step(cur, gh, lambda r, a: make_zip(), lambda *a: None, [REPO], log=lambda *a: None) == 1


def test_old_merged_and_unmerged_prs_are_not_watched(cur):
    factory_pr(cur)
    cur.execute("insert into events(name, payload, occurred_at) values ('pr.merged', %s, now() - interval '3 days')", (Jsonb({"pr_url": PR_URL}),))
    assert ec.candidates(cur, [REPO]) == []
    cur.execute("truncate events restart identity")
    factory_pr(cur)
    cur.execute("insert into events(name, payload) values ('pr.merged', %s)", (Jsonb({"pr_url": PR_URL}),))
    assert [p["sha"] for p in ec.candidates(cur, [REPO])] == [SHA]
    assert ec.candidates(cur, ["x/y"]) == []


def test_unseen_choice_expires(cur):
    aid = choice.request(cur, "exit_capture", "c", "q", [{"id": "s1", "label": "a"}], subject={"pr_url": PR_URL, "app": "admin-web"})
    cur.execute("update artifacts set created_at = now() - interval '8 days' where id=%s", (aid,))
    assert ec.expire(cur) == 1 and choice.open_choices(cur) == []
    assert choice.flag(cur, aid, "閉じた後でも印は付く", "t", "s1") == (True, "flagged")


def test_many_captures_are_one_inbox_item_notified_once(cur):
    for n in range(5):
        choice.request(cur, "exit_capture", f"c{n}", "q", [{"id": "s1", "label": "a"}],
                       subject={"pr_url": f"https://github.com/{REPO}/pull/{100 + n}", "app": "admin-web"})
    choice.request(cur, "taste_sample", "t", "q", [{"id": "A", "label": "a"}])
    xs = [i for i in inbox.items(cur) if i["kind"] == "choice"]
    assert "choice:exit_capture" in {i["key"] for i in xs} and len(xs) == 2
    [cap] = [i for i in xs if i["key"] == "choice:exit_capture"]
    assert cap["title"] == "実装後の画面: 5 件（#102・#103・#104 ほか 2 件）" and cap["url"] == "/#choices"
    now = datetime.now(timezone.utc)
    hist = {"choice:exit_capture": {"count": 1, "last": now - timedelta(days=2), "escalated": False}}
    cap["age_sec"] = 3 * 86400
    assert notify_inbox.due([cap], hist, now) == []          # 知らせ直さず、代理にも回さない
    assert notify_inbox.due([cap], {}, now) == [(cap, "new")]
    assert "once" not in inbox.view(cur)["items"][0]


def test_idle_open_pr_is_not_watched(cur):
    factory_pr(cur)
    cur.execute("update events set occurred_at = now() - interval '3 days' where name='pr.head_updated'")
    assert ec.candidates(cur, [REPO]) == []
