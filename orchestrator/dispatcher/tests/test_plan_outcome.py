"""判定と結果の突き合わせ（ADR 0036 の 4a）: 層の決め方・判定・実績の表・合格線、記録からの組み立て。

決定的な部品は DB なしで見る。記録からの組み立ては、空の Postgres に postgres/init/*.sql を当て、TEST_DATABASE_URL を向けて動かす（なければ飛ばす）。
"""
import os
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import plan_outcome as po  # noqa: E402

DEFS = yaml.safe_load((Path(__file__).resolve().parents[3] / "definitions" / "planner.yaml").read_text(encoding="utf-8"))
URL = os.environ.get("TEST_DATABASE_URL")
R = "https://github.com/o/r/pull/"


def test_values_match_definitions():
    s = DEFS["sizing"]
    assert [(x["layer"], x["globs"]) for x in s["layers"]] == [(l, g) for l, g in po.LAYERS]
    assert list(s["uncounted_layers"]) == list(po.UNCOUNTED_LAYERS)
    assert list(s["follow_globs"]) == po.FOLLOW_GLOBS
    assert s["split"] == po.SPLIT and s["min_turns"] == po.MIN_TURNS and s["bands"] == po.BANDS and s["pass_lines"] == po.PASS_LINES


def test_layers_of_rally_paths():
    files = ["apps/backend/crates/infra/migrations/0042_coach_leaves.sql", "apps/backend/crates/interface/src/coach_leaves.rs",
             "specs/contracts/api/admin-web/openapi.yaml", "specs/ux/issue-455.yaml", "apps/admin-web/src/components/headquarters/CoachLeaveHome.tsx",
             "apps/student-app/lib/main.dart", "changelog.d/455.md", "README.md", "tools/screenshots/run.sh"]
    assert po.layers_of(files) == ["db", "contract", "docs", "backend", "web", "mobile", "other"]
    assert po.layers_key(po.layers_of(files)) == "db+contract+backend+web+mobile"
    assert po.layers_key(["docs"]) == "docs"


def test_generated_types_and_follow_only_layers():
    # 契約と backend のチケット（rally #648 の形）: admin-web は生成した型と試験・撮影の値の追従だけ
    files = ["specs/contracts/api/admin-web/openapi.yaml", "apps/admin-web/src/api/schema.ts", "apps/admin-web/screenshots/fixtures.ts",
             "apps/admin-web/tests/headquartersApi.test.ts", "apps/backend/crates/domain/src/x.rs", "apps/backend/crates/interface/tests/x.rs"]
    assert po.layers_of(files) == ["contract", "backend", "other"]
    assert po.over_split(po.layers_of(files), 6, 36) == []
    # 画面の本体も変われば web を数える（rally #900 の形）
    assert po.layers_of(files + ["apps/admin-web/src/components/headquarters/SlotSupplyReport.tsx"]) == ["contract", "backend", "web"]


def test_over_split_and_band():
    assert po.over_split(["contract", "backend", "web"], 10, 20) == ["layers"]
    assert po.over_split(["backend", "docs", "other"], 31, 61) == ["files", "turns"]
    assert po.over_split(["backend"], 30, 60) == []
    assert [po.band(t) for t in (0, 30, 31, 60, 90, 91)] == ["S", "S", "M", "M", "L", "XL"]


def test_expected_scores():
    actual = ["apps/admin-web/src/a.tsx", "apps/admin-web/src/b.tsx", "specs/contracts/api/admin-web/openapi.yaml", "README.md"]
    r, p = po.expected_scores(["apps/admin-web/src/a.tsx", "openapi.yaml", "apps/backend/x.rs"], actual)
    assert (r, p) == (round(2 / 3, 3), round(2 / 3, 3))   # 名前だけの見込みはファイル名で当てる。README は分母に入れない
    assert po.expected_scores([], actual) == (None, None)
    # 試験・撮影の値・changelog は再現率の分母に入れない。当たれば適合率には数える
    actual2 = ["apps/backend/src/a.rs", "apps/backend/tests/a.rs", "apps/admin-web/screenshots/fixtures.ts", "changelog.d/1.md"]
    assert po.expected_scores(["apps/backend/src/a.rs", "apps/backend/tests/a.rs"], actual2) == (1.0, 1.0)
    assert po.expected_scores(["README.md"], ["README.md"]) == (1.0, 1.0)   # 本体がなければ全部が分母


def _o(**kw):
    base = {"incomplete": 0, "layers": ["backend"], "files": 5, "turns": 20, "jobs": 1, "route_changed": False, "split_child": False}
    return {**base, **kw}


def test_judge():
    assert po.judge(_o()) == ("ok", [])
    assert po.judge(_o(incomplete=1, turns=121, files=55, layers=["db", "backend", "contract"])) == ("undersized", ["incomplete", "layers", "files", "turns"])
    assert po.judge(_o(route_changed=True)) == ("misrouted", ["route_changed"])
    assert po.judge(_o(split_child=True, turns=8)) == ("oversized", ["small_child"])
    assert po.judge(_o(split_child=True, turns=12)) == ("ok", [])
    assert po.judge(_o(jobs=0, files=40)) == ("ok", [])   # 実装のジョブがない PR（デザインシステムの役など）は大きさで外れにしない


def test_percentile():
    assert po.percentile([], 0.9) is None
    assert po.percentile([10, 20, 30, 40, 50, 60, 70, 80, 90, 100], 0.9) == 90
    assert po.percentile([5], 0.9) == 5


def _row(n, turns, files=10, layers="backend", incomplete=0, recall=0.8, judgment="ok", planned=True, repo="o/r"):
    return {"repo": repo, "pr_url": f"{R}{n}", "merged": True, "jobs": 1, "turns": turns, "files": files, "layers_key": layers,
            "incomplete": incomplete, "expected_recall": recall, "judgment": judgment, "planned": planned, "cost_usd": 1.0}


def test_size_table_and_pass_status():
    rows = [_row(i, 20 + i) for i in range(9)] + [_row(99, 181, files=84, layers="db+contract+backend+web", incomplete=1, judgment="undersized")]
    t = {x["layers"]: x for x in po.size_table(rows)}
    assert t["backend"]["n"] == 9 and t["backend"]["turns_median"] == 24
    assert t["db+contract+backend+web"]["examples"] == [f"{R}99"]
    st = po.pass_status(rows)
    assert st["tickets"] == 10 and st["measured"]
    assert st["metrics"]["incomplete_rate"] == {"value": 0.1, "max": 0.1, "ok": True}
    assert st["metrics"]["turns_p90"]["value"] == 28 and st["metrics"]["turns_p90"]["ok"]
    assert st["judgments"]["undersized"] == 1 and st["ok"]
    rows.append(_row(100, 150, incomplete=1, judgment="undersized"))
    st = po.pass_status(rows)
    assert st["metrics"]["incomplete_rate"]["ok"] is False and not st["ok"]
    assert not po.pass_status(rows[:5])["measured"]


# ------------------------------------------------------------------ 記録から（DB）
db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL がない")


@pytest.fixture()
def cur():
    import psycopg
    from psycopg.rows import dict_row
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


def _plan(cur, issue, steps, expected=(), decision="ready"):
    from psycopg.types.json import Jsonb
    cur.execute("insert into artifacts(type, correlation_id, produced_by, payload) values ('Plan',%s,'plan',%s)",
                (f"github:o/r#{issue}", Jsonb({"repo": "o/r", "issue": issue, "decision": decision, "steps": steps, "expected_files": list(expected)})))


def _job(cur, issue, turns, cost, incomplete=False, cont=None):
    from psycopg.types.json import Jsonb
    cur.execute("insert into jobs(correlation_id, repo, issue_number, state, kind, result) values (%s,'o/r',%s,%s,'implement',%s) returning id",
                (f"github:o/r#{issue}", issue, "failed" if incomplete else "succeeded",
                 Jsonb({"num_turns": turns, "cost_usd": cost, "incomplete": incomplete})))
    jid = cur.fetchone()["id"]
    if cont:   # 続きのジョブ（理由つき）
        cur.execute("insert into events(name, correlation_id, job_id, payload) values ('continue.enqueued',%s,%s,%s)",
                    (f"github:o/r#{issue}", jid, Jsonb({"reason": cont})))
    return jid


def _merged(cur, issue, pr, days_ago=1):
    from psycopg.types.json import Jsonb
    cur.execute("insert into events(name, occurred_at, correlation_id, payload, dedupe_key) values ('pr.merged', now() - make_interval(days => %s),%s,%s,%s)",
                (days_ago, f"github:o/r#{issue}", Jsonb({"pr_url": f"{R}{pr}"}), f"pr.merged:{R}{pr}"))


@db
def test_step_records_the_455_shape(cur):
    _plan(cur, 455, ["ui_design", "implement"], ["apps/admin-web/src/components/headquarters/CoachLeaveHome.tsx", "openapi.yaml"])
    _job(cur, 455, 121, 11.33, incomplete=True)
    _job(cur, 455, 60, 8.14, cont="incomplete")
    _job(cur, 455, 5, 0.5, cont="conflict")   # 衝突の解消の続きは数えない
    _merged(cur, 455, 984)
    files = ([f"apps/backend/crates/interface/src/f{i}.rs" for i in range(50)] + ["apps/backend/crates/infra/migrations/0042.sql",
             "specs/contracts/api/admin-web/openapi.yaml", "apps/admin-web/src/components/headquarters/CoachLeaveHome.tsx", "changelog.d/455.md"])
    calls = []

    def gh(method, path, body=None):
        calls.append(path)
        if "/files" in path:
            return [{"filename": f} for f in files] if path.endswith("page=1") else []
        return {"labels": [{"name": "factory:needs-judgment"}]}

    assert po.step(cur, gh) == 1
    assert po.step(cur, gh) == 0   # 2 回目は残さない
    cur.execute("update events set payload = payload || '{\"expected_recall\": 0.01}' where name='plan.outcome'")
    assert po.rescore(cur, gh) == 1 and po.outcomes(cur, "o/r")[0]["expected_recall"] == round(2 / 53, 3)   # 今の数え方で組み立て直す
    o = po.outcomes(cur, "o/r")[0]
    assert o["judgment"] == "undersized" and o["reasons"] == ["incomplete", "layers", "files", "turns"]
    assert (o["jobs"], o["turns"], o["turns_max"], o["incomplete"], o["files"]) == (2, 181, 121, 1, 54)
    assert o["layers_key"] == "db+contract+backend+web" and o["needs_judgment"] and o["planned"] and o["band"] == "XL"
    assert o["continues"] == {"conflict": 1, "incomplete": 1}
    assert o["expected_recall"] == round(2 / 53, 3) and o["expected_precision"] == 1.0
    assert o["cost_usd"] == pytest.approx(19.47)


@db
def test_route_changed_split_child_and_unplanned(cur):
    from psycopg.types.json import Jsonb
    _plan(cur, 1, ["implement"])
    _plan(cur, 1, ["ui_design", "implement"])
    _job(cur, 1, 20, 1.0)
    _merged(cur, 1, 11)
    cur.execute("insert into events(name, correlation_id, payload) values ('plan.split','github:o/r#2',%s)", (Jsonb({"repo": "o/r", "issue": 2, "child": 3}),))
    _plan(cur, 3, ["implement"])
    _job(cur, 3, 6, 0.3)
    _merged(cur, 3, 13)
    _job(cur, 4, 25, 2.0)   # プランナーを通らなかったチケット
    _merged(cur, 4, 14, days_ago=40)   # 周回の窓（30 日）の外。backfill だけが拾う

    def gh(method, path, body=None):
        return [{"filename": "apps/backend/src/a.rs"}] if "/files" in path else {"labels": []}

    assert po.step(cur, gh) == 2
    assert po.step(cur, gh, days=None, limit=None) == 1
    got = {o["issue"]: o for o in po.outcomes(cur, days=60)}
    assert got[1]["judgment"] == "misrouted" and got[3]["judgment"] == "oversized"
    cur.execute("insert into events(name, correlation_id, payload) values ('plan.split','github:o/r#7',%s)",
                (Jsonb({"repo": "o/r", "issue": 7, "kind": "tasks", "children": [8, 9], "child": None}),))
    assert po._split_child(cur, "o/r", 9) and not po._split_child(cur, "o/r", 10)   # タスクのまとまりに切った子
    assert got[4]["planned"] is False and got[4]["judgment"] == "ok" and got[4]["plan_id"] is None
    assert len(po.outcomes(cur, days=14)) == 2   # occurred_at は PR の結果が出た時刻
