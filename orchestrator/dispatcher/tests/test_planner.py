"""プランナーの制御プレーン側（planner.py、ADR 0025・0026）: 判定の記録、経路どおりのラベル、問いと返事、定義との食い違い"""
import os
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
pytest.importorskip("psycopg")
import planner as pn  # noqa: E402

URL = os.environ.get("TEST_DATABASE_URL")
REPO, ISSUE, CORR = "o/r", 5, "github:o/r#5"
DEFS = yaml.safe_load((Path(__file__).resolve().parents[3] / "definitions" / "planner.yaml").read_text(encoding="utf-8"))
Q = [{"question": "進め方は", "why": "決まると着手できる", "options": ["倣う", "案を出す", "作る"], "recommended": 0}]


def res(decision="ready", steps=("implement",), guards=(), round_=1, questions=(), summary="項目を足す", marks=()):
    return {"decision": decision, "steps": list(steps), "guards": list(guards), "advisories": list(marks), "round": round_,
            "facts": {"has_design_system": False}, "versions": {"engine": "e" * 40, "product": "p" * 40, "prompt": "2"},
            "plan": {"design_scale": "small", "summary": summary, "questions": list(questions), "evidence": ["issue.md"], "interpretation": "i",
                     "confidence": 0.8, "steps": [{"role": r, "focus": f"{r} の仕事", "why": "要る", "effort": "light"} for r in steps],
                     "alternatives": [{"steps": ["ui_design", "implement"], "why_not": "画面は変えない"}]},
            "cost_usd": 0.03, "model": "m"}


# ------------------------------------------------------------------ DB の要らないもの
def test_constants_match_definitions():
    lb = DEFS["labels"]
    assert (pn.TRIGGER_LABEL, pn.NEEDS_INFO_LABEL, pn.ESCALATE_LABEL, pn.WAITING_LABEL) == (lb["trigger"], lb["needs_info"], lb["escalate"], lb["waiting"])
    assert pn.ROLE_LABELS == {k: v["label"] for k, v in DEFS["roles"].items()}
    assert pn.ROLE_JA == {k: v["ja"] for k, v in DEFS["roles"].items()}
    assert set(pn.GUARD_JA) == set(DEFS["guards"]) and set(pn.ADVISORY_JA) == set(DEFS["advisories"])
    assert (pn.MAX_ROUNDS, pn.MARKER) == (DEFS["asking"]["max_rounds"], DEFS["asking"]["answer_marker"])
    u = DEFS["upstream"]
    assert [pn.REQ_MARK, pn.REQ_MARK_END] == u["markers"] and set(pn.STORY_ACTION_JA) == set(u["story_actions"]) and pn.STORIES_DIR == u["stories_dir"]
    assert (pn.ADR_DIR, pn.CONTRACTS_DIR) == (u["adr"]["dir"], u["contracts"]["dir"])
    assert set(pn.CONTRACT_KIND_JA) == set(u["contracts"]["kinds"]) and set(pn.COMPAT_JA) == set(u["contracts"]["compat"])


def test_requirements_section_replaces_the_previous_one():
    req = {"acceptance_criteria": ["通知が届く"], "non_goals": ["メール"], "success_definition": "届く"}
    st = [{"action": "add", "id": "student-gets-reminder", "why": "新しい流れ", "title": "生徒が前日に知らせを受け取れる", "actor": "student"},
          {"action": "keep", "id": "coach-replies", "why": "隣の流れ"}]
    sec = pn.requirements_section(3, req, st)
    assert sec.startswith(pn.REQ_MARK) and sec.endswith(pn.REQ_MARK_END) and "- 通知が届く" in sec and "やらないこと:" in sec
    assert "- 増やす `student-gets-reminder`（新規: 生徒が前日に知らせを受け取れる・student）: 新しい流れ" in sec and "- 守る `coach-replies`: 隣の流れ" in sec
    assert "specs/stories/<id>.yaml" in sec
    body = pn.replace_requirements("本文", sec)
    assert body.startswith("本文\n\n") and body.count(pn.REQ_MARK) == 1
    again = pn.replace_requirements(body + "\n<!-- factory-uxspec:v1 -->x<!-- /factory-uxspec -->", pn.requirements_section(4, req, []))
    assert again.count(pn.REQ_MARK) == 1 and "Plan 4" in again and "Plan 3" not in again and "factory-uxspec" in again
    assert "ユーザーストーリー:" not in pn.requirements_section(5, req, [])


def test_questions_comment_has_marker_and_options():
    text = pn.questions_comment(7, 1, 2, "要約", Q, ["no_design_system"])
    assert text.startswith(pn.MARKER) and "（1/2）" in text and "c. 作る" in text and "a. 倣う（推奨）" in text
    assert "デザインシステムがなく" in text and "Plan 7" in text


@pytest.mark.parametrize("bodies,want", [
    ([], False),
    (["受け付けました"], False),                                   # 問いがまだない
    (["受け付けました", pn.MARKER + " 質問"], False),               # 返事がまだない
    ([pn.MARKER + " 質問", "倣ってください"], True),
    ([pn.MARKER + " 質問", "<!-- factory:x --> 工場の知らせ"], False),   # 工場のコメントは返事に数えない
    ([pn.MARKER + " 質問", "   "], False),
    ([pn.MARKER + " 1", "返事", pn.MARKER + " 2"], False),          # 最後の問いより後だけを見る
])
def test_answered(bodies, want):
    assert pn.answered([{"body": b} for b in bodies]) is want


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


class Calls:
    def __init__(self, comments=()):
        self.labels, self.comments, self.issue_comments = [], [], list(comments)

    def swap(self, repo, n, remove, add):
        self.labels.append((n, tuple(remove), tuple(add)))

    def comment(self, repo, n, text):
        self.comments.append(text)

    child_state = "open"
    created = None

    def gh(self, method, path, body=None):
        if "labels=" in path:
            return [{"number": ISSUE}, {"number": 6, "pull_request": {}}]
        if path.endswith("/comments?per_page=100"):
            return [{"body": b} for b in self.issue_comments]
        if method == "POST" and path.endswith("/issues"):
            self.created = body
            return {"number": 40}
        if method == "GET" and path.endswith("/issues/40"):
            return {"state": self.child_state}
        return {}


def job(cur, kind="plan"):
    cur.execute("insert into jobs(correlation_id, repo, issue_number, enqueued_by, kind, state) values (%s,%s,%s,'test',%s,'succeeded') returning id",
                (CORR, REPO, ISSUE, kind))
    return cur.fetchone()["id"]


def run(cur, c, r, gh=None):
    jid = job(cur)
    pid = pn.record(cur, REPO, ISSUE, CORR, jid, r)
    pn.act(cur, REPO, ISSUE, CORR, jid, pid, r, c.swap, c.comment, gh, "お知らせの画面")
    return pid


@needs_db
@pytest.mark.parametrize("steps,label", [(["implement"], "factory:planned"), (["ui_design", "implement"], "factory:design"),
                                         (["design_system", "ui_design", "implement"], "factory:design-system"), (["ui_design"], "factory:design")])
def test_ready_labels_the_first_role_and_keeps_rationale(cur, steps, label):
    c = Calls()
    pid = run(cur, c, res(steps=steps, marks=["small_change_with_designer"]))
    assert c.labels == [(ISSUE, ("factory:running",), (label,))]
    assert pn.path_ja(steps) in c.comments[0] and f"Plan {pid}" in c.comments[0]
    assert "とらなかった経路" in c.comments[0] and "画面は変えない" in c.comments[0] and "小さな改修だがデザイナーを通す" in c.comments[0]
    cur.execute("select payload from events where name='plan.decided'")
    ev = cur.fetchone()["payload"]
    assert (ev["decision"], ev["steps"], ev["plan_id"], ev["cost_usd"], ev["alternatives"]) == ("ready", steps, pid, 0.03, 1)
    assert ev["efforts"] == ["light"] * len(steps) and ev["versions"]["prompt"] == "2"
    [p] = pn.plans(cur, CORR)
    assert p["summary"] == "項目を足す" and p["alternatives"][0]["why_not"] == "画面は変えない" and p["versions"]["engine"] == "e" * 40
    assert p["step_details"][0]["focus"].endswith("の仕事") and p["evidence"] == ["issue.md"] and p["interpretation"] == "i"


@needs_db
def test_ask_then_answer_requeues_next_round(cur):
    c = Calls()
    run(cur, c, res("ask", ["ui_design", "implement"], ["no_design_system"], questions=Q))
    assert c.labels == [(ISSUE, ("factory:running",), ("factory:needs-info",))] and c.comments[0].startswith(pn.MARKER)
    cur.execute("select count(*) as n from events where name='plan.asked'")
    assert cur.fetchone()["n"] == 1
    c.issue_comments = [c.comments[0]]
    assert pn.poll_answers(cur, c.gh, c.swap, [REPO], "tier-large") == 0          # 返事がまだない
    c.issue_comments.append("既存の画面に倣ってください")
    assert pn.poll_answers(cur, c.gh, c.swap, [REPO], "tier-large") == 1
    assert c.labels[-1] == (ISSUE, ("factory:needs-info",), ("factory:queued",))
    cur.execute("select j.kind, j.enqueued_by, e.payload from jobs j join events e on e.job_id=j.id and e.name='job.enqueued' where j.state='queued'")
    r = cur.fetchone()
    assert (r["kind"], r["enqueued_by"], r["payload"]["input"]) == ("plan", "planner", {"round": 2})
    assert pn.poll_answers(cur, c.gh, c.swap, [REPO], "tier-large") == 0          # 同じチケットの active なジョブは 1 本


@needs_db
def test_escalate_goes_to_human(cur):
    c = Calls()
    run(cur, c, res("escalate", ["ui_design", "implement"], ["rounds_exhausted"], round_=3))
    assert c.labels == [(ISSUE, ("factory:running",), ("factory:plan-human",))]
    assert "往復を使い切った" in c.comments[0] and "factory:plan" in c.comments[0] and "プロダクトデザイナー → 実装役" in c.comments[0]
    assert len(pn.plans(cur, CORR)) == 1


@needs_db
def test_ready_without_known_roles_goes_to_human(cur):
    c = Calls()
    run(cur, c, res(steps=["bogus"]))
    assert c.labels == [(ISSUE, ("factory:running",), ("factory:plan-human",))]


@needs_db
def test_design_system_first_is_split_and_advanced_when_child_closes(cur):
    c = Calls()
    run(cur, c, res(steps=["design_system", "ui_design", "implement"]), c.gh)
    assert c.created["labels"] == ["factory:design-system"] and c.created["title"] == "デザインシステム: お知らせの画面"
    assert "#5 の経路" in c.created["body"] and "design_system の仕事" in c.created["body"]
    assert c.labels == [(ISSUE, ("factory:running",), ("factory:waiting-steps",))] and "#40 に切りました" in c.comments[0]
    assert pn.advance_steps(cur, c.gh, c.swap, c.comment, [REPO]) == 0      # まだ閉じていない
    c.child_state = "closed"
    assert pn.advance_steps(cur, c.gh, c.swap, c.comment, [REPO]) == 1
    assert c.labels[-1] == (ISSUE, ("factory:waiting-steps",), ("factory:design",))
    assert pn.advance_steps(cur, c.gh, c.swap, c.comment, [REPO]) == 0      # 1 度だけ
    cur.execute("select payload from events where name='plan.advanced'")
    assert cur.fetchone()["payload"]["role"] == "ui_design"


@needs_db
def test_design_system_only_stays_on_the_ticket(cur):
    c = Calls()
    run(cur, c, res(steps=["design_system"]), c.gh)
    assert c.created is None and c.labels == [(ISSUE, ("factory:running",), ("factory:design-system",))]


# ------------------------------------------------------------------ 大きさで切る（ADR 0036 の 2）
class SplitGH:
    def __init__(self):
        self.posts, self.patches, self.states, self.next = [], [], {}, 40

    def __call__(self, method, path, body=None):
        if "labels=" in path:
            return [{"number": ISSUE}]
        if method == "POST" and path.endswith("/issues"):
            self.posts.append(body)
            self.next += 1
            self.states[self.next] = "open"
            return {"number": self.next, "html_url": f"https://github.com/{REPO}/issues/{self.next}"}
        if method == "PATCH":
            self.patches.append((path, body))
            return {}
        if method == "GET" and path.endswith(f"/issues/{ISSUE}"):
            return {"labels": [{"name": "factory:priority:p0"}, {"name": "factory:running"}]}
        if method == "GET" and "/issues/" in path:
            return {"state": self.states.get(int(path.rsplit("/", 1)[1]), "open")}
        return {}


def T(key, kind="implement", deps=()):
    return {"key": key, "title": f"題 {key}", "kind": kind, "summary": f"{key} の要約", "do": [f"{key} をする"], "acceptance": [f"{key} ができる"],
            "depends_on": list(deps), "effort": "standard", "expected_files": [f"{key}.rs"]}


@needs_db
def test_split_files_children_in_dependency_order_and_closes_parent(cur):
    c, gh = Calls(), SplitGH()
    r = res(decision="split", steps=["implement"], guards=["oversized"])
    r["tasks"], r["oversize"] = [T("t1", "contract"), T("t2", deps=["t1"]), T("t3", "design", deps=["t2"])], ["layers", "turns"]
    r["plan"]["size"] = {"layers": ["contract", "backend", "web"], "turns_band": "XL", "basis": "似た #455 は 181 ターン"}
    pid = run(cur, c, r, gh)
    assert [p["labels"] for p in gh.posts] == [["factory:plan", "factory:priority:p0"], ["factory:desk-waiting", "factory:priority:p0"],
                                               ["factory:desk-waiting", "factory:priority:p0"]]
    assert gh.posts[0]["title"] == "題 t1（#5 の t1）" and "依存:" not in gh.posts[0]["body"]
    assert "依存: #41" in gh.posts[1]["body"] and "依存: #42" in gh.posts[2]["body"] and "t2 ができる" in gh.posts[1]["body"]
    assert "UXSpec" in gh.posts[0]["body"] and f"parent={ISSUE} plan={pid} task=t1" in gh.posts[0]["body"]
    assert c.labels == [(ISSUE, ("factory:running",), ("factory:waiting-steps",))]
    assert "3 件のタスクに切りました" in c.comments[0] and "#42 題 t2（#41 の後）" in c.comments[0] and "181 ターン" in c.comments[0]
    cur.execute("select waiting_on from tickets where source_ref=%s", (f"{REPO}#42",))
    assert cur.fetchone()["waiting_on"] == [f"{REPO}#41"]
    cur.execute("select payload from events where name='plan.decided'")
    d = cur.fetchone()["payload"]
    assert d["tasks"] == 3 and d["oversize"] == ["layers", "turns"] and d["size"]["turns_band"] == "XL"
    # 子がすべて閉じたら、もとのチケットを閉じる（1 度だけ）
    gh.states.update({41: "closed", 42: "closed"})
    assert pn.advance_steps(cur, gh, c.swap, c.comment, [REPO]) == 0
    gh.states[43] = "closed"
    assert pn.advance_steps(cur, gh, c.swap, c.comment, [REPO]) == 1
    assert gh.patches == [(f"repos/{REPO}/issues/{ISSUE}", {"state": "closed", "state_reason": "completed"})]
    assert pn.advance_steps(cur, gh, c.swap, c.comment, [REPO]) == 0
    # 判定し直しても、同じ Plan の子は 2 度作らない
    pn.split_tasks(cur, gh, REPO, ISSUE, CORR, pid, "お知らせの画面", r["tasks"], r["plan"])
    assert len(gh.posts) == 3


@needs_db
def test_attach_sizes_adds_the_product_table(cur):
    from psycopg.types.json import Jsonb
    cur.execute("insert into events(name, payload, dedupe_key) values ('plan.outcome',%s,'plan.outcome:x')",
                (Jsonb({"repo": REPO, "pr_url": "x", "merged": True, "jobs": 2, "turns": 181, "files": 84, "layers_key": "contract+backend+web",
                        "incomplete": 1, "cost_usd": 19.5}),))
    inp = pn.attach_sizes(cur, REPO, {"round": 2})
    assert inp["round"] == 2 and inp["size_table"][0]["layers"] == "contract+backend+web" and inp["size_table"][0]["turns_median"] == 181
    assert pn.attach_sizes(cur, "other/repo", {})["size_table"] == []


@needs_db
def test_pre_implement_only_once_and_parent_deps_carry(cur):
    from psycopg.types.json import Jsonb
    r = res(steps=["ui_design", "implement"], marks=["split_after_design"])
    run(cur, Calls(), r)
    assert pn.wants_pre_implement(cur, CORR)
    jid = pn.enqueue_pre_implement(cur, REPO, ISSUE, CORR, "design_handed_off")
    cur.execute("select payload from events where name='job.enqueued' and job_id=%s", (jid,))
    assert cur.fetchone()["payload"]["input"]["phase"] == "implement"
    cur.execute("update jobs set state='succeeded' where id=%s", (jid,))
    r2 = res(decision="split", steps=["implement"], guards=["oversized"])
    r2["phase"], r2["tasks"] = "implement", [T("t1", "contract"), T("t2", deps=["t1"])]
    gh = SplitGH()
    gh_get = gh.__call__

    def with_deps(method, path, body=None):   # 親の本文に足りない部品の依存がある
        if method == "GET" and path.endswith(f"/issues/{ISSUE}"):
            return {"labels": [], "body": "UXSpec\n依存: #9"}
        return gh_get(method, path, body)
    run(cur, Calls(), r2, with_deps)
    assert "依存: #9" in gh.posts[0]["body"] and "依存: #9 #41" in gh.posts[1]["body"]
    assert gh.posts[0]["labels"] == ["factory:desk-waiting"]
    assert not pn.wants_pre_implement(cur, CORR)   # 実装の手前の判定は 1 度だけ


class BodyGH:
    def __init__(self, body="もとの本文"):
        self.body, self.patches = body, []

    def __call__(self, method, path, body=None):
        if method == "GET" and path.endswith(f"/issues/{ISSUE}"):
            return {"body": self.body, "labels": []}
        if method == "PATCH":
            self.patches.append(body)
            self.body = body["body"]
        return {}


@needs_db
def test_ready_records_requirements_and_writes_the_section(cur):
    c, gh = Calls(), BodyGH()
    r = res(steps=["ui_design", "implement"])
    r["requirements"] = {"acceptance_criteria": ["通知が届く"], "non_goals": [], "success_definition": "届く"}
    r["stories"] = [{"action": "keep", "id": "coach-replies", "why": "隣の流れ"}]
    pid = run(cur, c, r, gh)
    assert gh.body.startswith("もとの本文\n\n" + pn.REQ_MARK) and "- 通知が届く" in gh.body and "守る `coach-replies`" in gh.body
    assert "本文の「要件とユーザーストーリー（プランナー）」の節に書きました（ストーリー: 守る `coach-replies`）" in c.comments[0]
    cur.execute("select payload from artifacts where type='Requirements'")
    [a] = [x["payload"] for x in cur.fetchall()]
    assert (a["plan_id"], a["acceptance_criteria"], a["stories"][0]["id"]) == (pid, ["通知が届く"], "coach-replies")
    cur.execute("select payload from events where name='plan.decided'")
    ev = cur.fetchone()["payload"]
    assert ev["requirements"] is True and ev["stories"] == [{"action": "keep", "id": "coach-replies"}]
    # 判定し直したら節を置き換える（2 つにしない）
    r["stories"] = []
    run(cur, c, r, gh)
    assert gh.body.count(pn.REQ_MARK) == 1 and "coach-replies" not in gh.body and len(gh.patches) == 2


@needs_db
def test_ask_does_not_write_requirements(cur):
    c, gh = Calls(), BodyGH()
    r = res(decision="ask", questions=Q)
    r["requirements"] = None
    run(cur, c, r, gh)
    assert gh.patches == []
    cur.execute("select count(*) as n from artifacts where type='Requirements'")
    assert cur.fetchone()["n"] == 0


ADRS = {"follow": [{"id": "0004", "why": "列を足す"}],
        "new": [{"status": "proposed", "title": "取り消しは 24 時間前まで", "context": "c", "decision": "d", "alternatives": ["前日まで"], "consequences": "r"}]}
CONTRACTS = [{"contract": "specs/contracts/api/student-app/openapi.yaml", "kind": "change", "compat_impact": "breaking",
              "operations": ["POST /bookings/{id}/cancel"], "summary": "取り消し"}]


def test_requirements_section_has_adrs_and_contracts():
    sec = pn.requirements_section(3, {"acceptance_criteria": ["取り消せる"]}, [], ADRS, CONTRACTS)
    assert "- 従う ADR 0004: 列を足す" in sec and "- 新しく残す: **取り消しは 24 時間前まで**" in sec and "  - とらなかった案: 前日まで" in sec
    assert "docs/adr/<次の番号>-<名前>.md" in sec
    assert "- `specs/contracts/api/student-app/openapi.yaml` を変える（`POST /bookings/{id}/cancel`）・互換を壊す: 取り消し" in sec
    assert "人の承認を待つ" in sec
    plain = pn.requirements_section(3, {"acceptance_criteria": ["取り消せる"]}, [])
    assert "判断（ADR）" not in plain and "契約の差分" not in plain


@needs_db
def test_ready_records_adr_and_contract_diff(cur):
    c, gh = Calls(), BodyGH()
    r = res(steps=["implement"])
    r["requirements"] = {"acceptance_criteria": ["取り消せる"], "non_goals": [], "success_definition": "s"}
    r["adrs"], r["contract_changes"] = ADRS, CONTRACTS
    pid = run(cur, c, r, gh)
    assert "従う ADR 0004" in gh.body and "契約の差分:" in gh.body
    assert "ADR: 従う 1・新しく残す 1、契約の差分 1" in c.comments[0]
    cur.execute("select type, payload from artifacts where type in ('ADR','ContractDiff','Requirements') order by id")
    rows = {x["type"]: x["payload"] for x in cur.fetchall()}
    assert rows["ADR"]["plan_id"] == pid and rows["ADR"]["status"] == "proposed" and rows["ADR"]["decision"] == "d"
    assert rows["ContractDiff"]["compat_impact"] == "breaking" and rows["ContractDiff"]["contract"].endswith("openapi.yaml")
    assert rows["Requirements"]["adrs_follow"] == [{"id": "0004", "why": "列を足す"}]
    cur.execute("select payload from events where name='plan.decided'")
    ev = cur.fetchone()["payload"]
    assert ev["adrs"] == {"follow": ["0004"], "new": 1} and ev["contracts"][0]["kind"] == "change"
