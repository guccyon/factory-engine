"""流さなかった差し戻しの停止（sendback:<元 PR>）を、人が差し戻しの Issue を直しに回した時点で閉じる（sendback.py）。

2026-11-09 rally #1228/#1234・#1251/#1254: 差し戻しの Issue（factory:fix-proposed）のラベルを factory:planned に替えても、
停止が開いたままなので元 PR の衝突を解かず、差し戻しの Issue は「依存: #元 Issue」で元 PR のマージを待ち、19 時間以上止まった。

  TEST_DATABASE_URL=postgresql://factory@localhost:5432/factory_test python -m pytest orchestrator/dispatcher/tests
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL がない")

if URL:
    for k, v in {"DATABASE_URL": URL, "GH_TOKEN": "x", "NOTIFIER": "log"}.items():
        os.environ.setdefault(k, v)
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb

    import inbox
    import sendback
    import stoppage
    import supervisor

REPO = "o/r"
ORIGIN_ISSUE, ORIGIN_PR, FIX_ISSUE = 1149, 1228, 1234
PR_URL = f"https://github.com/{REPO}/pull/{ORIGIN_PR}"
CORR = f"github:{REPO}#{ORIGIN_ISSUE}"
KEY = f"sendback:{PR_URL}"


@pytest.fixture(autouse=True)
def _no_plan_gate(monkeypatch):
    """ここで見るのは差し戻し・並べ方の流れ。プランナーを通す関所（ADR 0038 の追記）は test_workflow_guard と下の 1 本で見る"""
    if URL:
        monkeypatch.setattr(supervisor.workflow_guard, "PLAN_GATE", False)


@pytest.fixture()
def cur():
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


class FakeGitHub:
    """元 PR・元 Issue・差し戻しの Issue だけの GitHub"""

    def __init__(self):
        self.pr = {"state": "open", "draft": False, "mergeable_state": "dirty", "labels": [], "merged_at": None,
                   "head": {"sha": "a" * 40, "ref": f"factory/issue-{ORIGIN_ISSUE}-job-1", "repo": {"full_name": REPO}}}
        self.origin_state = "open"
        self.fix_labels = ["factory:sendback", "factory:fix-proposed"]
        self.calls = []

    def gh(self, method, path, body=None):
        self.calls.append((method, path))
        if method == "GET" and path == f"repos/{REPO}/pulls/{ORIGIN_PR}":
            return self.pr
        if method == "GET" and "issues?state=open" in path:
            if "factory:planned" not in self.fix_labels:
                return []
            return [{"number": FIX_ISSUE, "title": "差し戻し: x", "body": f"## 直すこと\n\n依存: #{ORIGIN_ISSUE}", "created_at": "1",
                     "html_url": f"https://github.com/{REPO}/issues/{FIX_ISSUE}", "labels": [{"name": n} for n in self.fix_labels]}]
        if method == "GET" and path == f"repos/{REPO}/issues/{ORIGIN_ISSUE}":
            return {"state": self.origin_state}
        if method == "POST" and path == f"repos/{REPO}/issues/{FIX_ISSUE}/labels":
            self.fix_labels += [n for n in body["labels"] if n not in self.fix_labels]
        if method == "DELETE" and path.startswith(f"repos/{REPO}/issues/{FIX_ISSUE}/labels/"):
            self.fix_labels = [n for n in self.fix_labels if n not in path.replace("%3A", ":")]
        if method == "PUT" and path.endswith(f"/pulls/{ORIGIN_PR}/merge"):
            self.pr.update(state="closed", merged_at="2026-11-09T12:00:00Z")
            self.origin_state = "closed"
        return {}


@pytest.fixture()
def gh(monkeypatch):
    f = FakeGitHub()
    monkeypatch.setattr(supervisor, "gh", f.gh)
    monkeypatch.setattr(supervisor, "REPOS", [REPO])
    monkeypatch.setattr(supervisor, "factory_prs", lambda cur: [{"repo": REPO, "number": ORIGIN_PR, "url": PR_URL, "corr": CORR}])
    monkeypatch.setattr(supervisor, "pr_files", lambda repo, n: [])
    monkeypatch.setattr(supervisor, "classify", lambda files, rules: [])
    monkeypatch.setattr(supervisor, "decide", lambda types, manifest: {"table": "auto", "factory_grant": "auto", "change_types": [], "execute": True})
    monkeypatch.setattr(supervisor, "DRY_RUN", False)
    monkeypatch.setattr(supervisor, "INSPECT_ENABLED", False)
    monkeypatch.setattr(supervisor, "CONFLICT_FIX", True)
    monkeypatch.setattr(supervisor, "over_budget", lambda cur: None)
    return f


def sendback_stuck(cur, filed=True):
    """judge が差し戻しの Issue を起票し、次の judge で流さなかった差し戻しの停止が開いた元 PR"""
    if filed:
        cur.execute("insert into events(name, correlation_id, payload) values ('sendback.filed', %s, %s)",
                    (CORR, Jsonb({"origin_pr": PR_URL, "fix_issue": f"https://github.com/{REPO}/issues/{FIX_ISSUE}",
                                  "fix_corr": f"github:{REPO}#{FIX_ISSUE}", "mode": "attended"})))
    stoppage.open_stoppage(cur, KEY, "human", "差し戻しを流さなかった（per_pr_limit）。指摘 1 件が残っている",
                           waiting_for="人の判断（直すか、受け入れるか）", correlation_id=CORR, quiet=True)


def jobs(cur):
    cur.execute("select issue_number, kind, state from jobs order by id")
    return [(r["issue_number"], r["kind"]) for r in cur.fetchall()]


def tick(cur):
    open_prs = supervisor.merge_step(cur, {}, {REPO: {}})
    supervisor.enqueue_step(cur, open_prs)


def test_label_flip_closes_stoppage_then_conflict_merge_and_fix_flows(cur, gh):
    # 受け入れの確認: 衝突した元 PR で、ラベル替え → 停止が閉じる → 衝突を解く → マージ → 差し戻しが着手
    sendback_stuck(cur)
    tick(cur)
    assert stoppage.open_keys(cur, KEY) == [KEY] and jobs(cur) == []   # 停止の間は衝突を解かない（ここまでは今までどおり）

    gh.fix_labels = ["factory:sendback", "factory:planned"]   # 人が差し戻しの Issue を流す
    tick(cur)   # この周期で差し戻しの Issue が台帳に写る（依存の元 Issue が開いているので投入はしない）
    cur.execute("select state, waiting_on from tickets where source_ref=%s", (f"{REPO}#{FIX_ISSUE}",))
    assert cur.fetchone() == {"state": "planned", "waiting_on": [f"{REPO}#{ORIGIN_ISSUE}"]}
    tick(cur)
    assert stoppage.open_keys(cur, KEY) == []
    cur.execute("select payload from events where name='stoppage.closed'")
    assert "#1234 を直しに回した" in cur.fetchone()["payload"]["remedy"]
    assert jobs(cur) == [(ORIGIN_ISSUE, "implement")]   # 元 PR の衝突を解く続きのジョブ
    cur.execute("select payload->'continue'->>'reason' as r from events where name='job.enqueued'")
    assert cur.fetchone()["r"] == "conflict"

    cur.execute("update jobs set state='succeeded', finished_at=now()")   # 衝突が解けた
    gh.pr.update(mergeable_state="clean", head={**gh.pr["head"], "sha": "b" * 40})
    tick(cur)
    assert ("PUT", f"repos/{REPO}/pulls/{ORIGIN_PR}/merge") in gh.calls   # マージした（元 Issue も閉じる）
    tick(cur)
    cur.execute("select 1 from events where dedupe_key=%s", (f"supervisor.enqueued:{REPO}#{FIX_ISSUE}",))
    assert cur.fetchone() is not None   # 差し戻しが着手された（依存の元 Issue が閉じたので投入した）
    assert "factory:planned" not in gh.fix_labels


def test_stoppage_stays_while_fix_issue_not_flowed(cur, gh):
    sendback_stuck(cur)
    for _ in range(2):
        tick(cur)
    assert stoppage.open_keys(cur, KEY) == [KEY]
    assert not sendback.release_if_fixing(cur, PR_URL, CORR)


def test_inbox_flow_relabels_via_supervisor(cur, gh):
    sendback_stuck(cur)
    [it] = inbox.items(cur)
    assert it["key"] == f"stoppage:{KEY}" and [o["id"] for o in it["answer"]["options"]] == ["flow", "accept"]
    res, msg = inbox.answer(cur, it["key"], "flow", "owner", "console")
    assert res == "answered" and "#1234" in msg
    assert inbox.items(cur) == []                                     # 回答済（反映待ち）
    assert stoppage.open_keys(cur, KEY) == [KEY]                      # ラベルは supervisor が替える
    tick(cur)
    assert gh.fix_labels == ["factory:sendback", "factory:planned"]
    tick(cur)                                                         # 台帳に写った次の周期に閉じる
    assert stoppage.open_keys(cur, KEY) == []
    tick(cur)
    cur.execute("select count(*) as n from events where name='sendback.flowed'")
    assert cur.fetchone()["n"] == 1                                   # 張り替えは 1 回だけ


def test_inbox_accept_closes_stoppage(cur, gh):
    sendback_stuck(cur, filed=False)                                  # 上限で差し戻しの Issue を起票しなかった
    [it] = inbox.items(cur)
    assert [o["id"] for o in it["answer"]["options"]] == ["accept"]   # 流す Issue がない
    assert inbox.answer(cur, it["key"], "flow", "owner", "console")[0] == "invalid"
    res, _ = inbox.answer(cur, it["key"], "accept", "owner", "console", note="残りは軽微")
    assert res == "answered" and stoppage.open_keys(cur, KEY) == []
    tick(cur)
    assert jobs(cur) == [(ORIGIN_ISSUE, "implement")]                 # 停止が閉じたので衝突を解く
