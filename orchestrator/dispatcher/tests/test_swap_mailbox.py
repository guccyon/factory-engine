"""工場の更新の依頼の郵便受け（ADR 0031 の順番 3）: factory:swap の Issue を拾う相手・読み方・待ち・返し方・取り消し。

GitHub は偽物に差し替える。DB を使うものは空の Postgres に postgres/init/*.sql を当て、TEST_DATABASE_URL を向けて動かす（なければ飛ばす）。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
URL = os.environ.get("TEST_DATABASE_URL")
for k, v in {"DATABASE_URL": URL or "postgresql://x@localhost:1/x", "NOTIFIER": "log"}.items():
    os.environ.setdefault(k, v)

import swap  # noqa: E402
import swap_mailbox as mb  # noqa: E402

needs_db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL がない")
RP = "o/factory-engine"


# ------------------------------------------------------------------ 読み方（DB なし）
def test_parse_reads_only_the_fixed_lines():
    body = "main に #250 を入れたい。\n\n```\ntarget: 9981a21\nkind: control_plane\n```\n\n- urgency: `urgent`\n"
    assert mb.parse(body) == {"target": "9981a21", "kind": "control_plane", "urgency": "urgent", "deadline": None}
    assert mb.parse("") == {"target": "main", "kind": "code", "urgency": "normal", "deadline": None}
    assert mb.parse("deadline: 2026-11-06T18:00+09:00")["deadline"] == "2026-11-06T18:00+09:00"


def test_the_help_text_itself_is_readable():
    assert mb.parse(mb.FORMAT_HELP)["deadline"] == "2026-11-06T18:00+09:00"


@pytest.mark.parametrize("body", ["target: main; rm -rf /", "target: $(whoami)", "kind: everything", "urgency: now", "target: origin/main"])
def test_parse_rejects_values_outside_the_shape(body):
    with pytest.raises(mb.Unreadable):
        mb.parse(body)


def test_requesters_default_to_the_repo_owner(monkeypatch):
    monkeypatch.setenv("SWAP_MAILBOX_REPO", RP)
    monkeypatch.delenv("SWAP_REQUESTERS", raising=False)
    assert mb.requesters() == {"o"}
    monkeypatch.setenv("SWAP_REQUESTERS", "Alice, bob")
    assert mb.requesters() == {"alice", "bob"}


def test_redact_hides_machine_paths_and_hostnames():
    s = mb.redact("git -C /Users/someone/src/fe failed on box-1.local")
    assert "someone" not in s and "box-1" not in s and "~/src/fe" in s


# ------------------------------------------------------------------ 拾う・待つ・返す（DB）
class GH:
    def __init__(self, issues=None, labeler="o"):
        self.issues = {i["number"]: {"state": "open", "body": "", "title": "更新", **i} for i in (issues or [])}
        self.labeler = labeler
        self.comments, self.labels, self.patches = [], [], []

    def __call__(self, method, path, body=None):
        if method == "GET" and "/issues?labels=" in path:
            return [i for i in self.issues.values() if i["state"] == "open" and i.get("labeled", True)]
        if method == "GET" and path.endswith("/events?per_page=100"):
            return [{"event": "labeled", "label": {"name": "factory:swap"}, "actor": {"login": self.labeler}}]
        if method == "GET":
            return self.issues[int(path.rsplit("/", 1)[1])]
        if method == "PATCH":
            self.patches.append((path, body))
            self.issues[int(path.rsplit("/", 1)[1])]["state"] = body["state"]
            return {}
        raise AssertionError((method, path))

    def comment(self, repo, n, text):
        self.comments.append((n, text))

    def set_labels(self, repo, n, remove, add):
        self.labels.append((n, tuple(remove), tuple(add)))
        if "factory:swap" in remove:
            self.issues[n]["labeled"] = False


@pytest.fixture()
def cur(monkeypatch):
    import psycopg
    from psycopg.rows import dict_row
    monkeypatch.setenv("SWAP_MAILBOX_REPO", RP)
    monkeypatch.delenv("SWAP_REQUESTERS", raising=False)
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


def poll(cur, gh, unattended=False):
    mb.poll(cur, gh, gh.comment, gh.set_labels, lambda *a: None, unattended=lambda: unattended)


@needs_db
def test_request_from_an_allowed_labeler_is_confirmed_now_and_reported(cur):
    gh = GH([{"number": 5, "body": "target: main"}])
    poll(cur, gh)
    s = swap.active(cur)
    assert s["status"] == "confirmed" and s["source"] == f"{RP}#5" and s["by"] == "o"
    assert (5, ("factory:swap", "factory:swap-failed"), ("factory:swapping",)) in gh.labels and "受けました" in gh.comments[-1][1]
    poll(cur, gh)   # 2 回目は拾い直さない
    assert len(swap.recent(cur)) == 1 and len(gh.comments) == 1
    swap.record(cur, s["id"], "swap.launched", {"container": "x"})
    swap.record(cur, s["id"], "swap.completed", {"target_sha": "bbbbbbb", "prev": "aaaaaaa", "kind": "code", "kind_declared": "code", "stopped_sec": None})
    poll(cur, gh)
    poll(cur, gh)   # ステップごとに 1 回だけ
    texts = [t for _, t in gh.comments]
    assert len(texts) == 3 and "更新役起動済" in texts[1] and "反映済" in texts[2] and "`aaaaaaa` → `bbbbbbb`" in texts[2]
    assert gh.issues[5]["state"] == "closed" and (5, ("factory:swapping",), ("factory:swapped",)) in gh.labels


@needs_db
def test_labels_from_other_people_are_ignored_silently(cur):
    gh = GH([{"number": 6}], labeler="stranger")
    poll(cur, gh)
    assert swap.active(cur) is None and gh.comments == [] and gh.labels == []


@needs_db
def test_unreadable_body_is_answered_once_and_not_requested(cur):
    gh = GH([{"number": 7, "body": "target: $(curl evil)"}])
    poll(cur, gh)
    poll(cur, gh)
    assert swap.active(cur) is None and len(gh.comments) == 1 and "読めませんでした" in gh.comments[0][1]
    assert (7, ("factory:swap",), ("factory:swap-failed",)) in gh.labels


@needs_db
def test_one_at_a_time_waits_and_notes_once(cur):
    other = swap.request(cur, "main", "manual-builder")
    gh = GH([{"number": 8}])
    poll(cur, gh)
    poll(cur, gh)
    assert len(gh.comments) == 1 and f"更新 {other}" in gh.comments[0][1] and len(swap.recent(cur)) == 1
    swap.cancel(cur, other, "t")
    poll(cur, gh)
    assert swap.active(cur)["source"] == f"{RP}#8"


@needs_db
def test_drain_kinds_wait_for_attended_hours(cur):
    gh = GH([{"number": 9, "body": "kind: control_plane"}])
    poll(cur, gh, unattended=True)
    poll(cur, gh, unattended=True)
    assert swap.active(cur) is None and len(gh.comments) == 1 and "attended" in gh.comments[0][1]
    poll(cur, gh, unattended=False)
    assert swap.active(cur)["kind"] == "control_plane"


@needs_db
def test_swapper_deferral_is_retried_in_attended_hours(cur):
    gh = GH([{"number": 10}])
    poll(cur, gh)
    sid = swap.active(cur)["id"]
    swap.record(cur, sid, "swap.launched", {})
    swap.record(cur, sid, "swap.preflight_failed", {"error": "…", "deferred": "unattended", "kind": "control_plane", "judged": "control_plane"})
    poll(cur, gh, unattended=True)
    assert swap.active(cur) is None and "依頼し直します" in gh.comments[-1][1]
    assert not any(l[2] == ("factory:swap-failed",) for l in gh.labels)   # 失敗の印は付けない
    poll(cur, gh, unattended=False)
    s = swap.active(cur)
    assert s["id"] != sid and s["kind"] == "control_plane" and s["source"] == f"{RP}#10"


@needs_db
def test_closing_the_issue_before_launch_cancels(cur):
    gh = GH([{"number": 11}])
    poll(cur, gh)
    sid = swap.active(cur)["id"]
    gh.issues[11]["state"] = "closed"
    poll(cur, gh)
    assert swap.get(cur, sid)["status"] == "cancelled" and "取り消し済" in gh.comments[-1][1]


@needs_db
def test_failure_keeps_the_issue_open_and_redacts_paths(cur):
    gh = GH([{"number": 12}])
    poll(cur, gh)
    sid = swap.active(cur)["id"]
    swap.record(cur, sid, "swap.launched", {})
    swap.record(cur, sid, "swap.failed", {"error": "docker failed in /Users/someone/src/fe"})
    poll(cur, gh)
    assert gh.issues[12]["state"] == "open" and "someone" not in gh.comments[-1][1] and "人の確認が要ります" in gh.comments[-1][1]
    assert (12, ("factory:swapping",), ("factory:swap-failed",)) in gh.labels
