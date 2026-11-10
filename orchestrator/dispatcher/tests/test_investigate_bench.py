"""一次調査の評価セット（investigate_bench.py と eval-corpus/investigate/cases.yaml、ADR 0048 の決定 8・順番 6）"""
import os
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("NOTIFIER", "log")
URL = os.environ.get("TEST_DATABASE_URL")
ROOT = Path(__file__).resolve().parents[3]
CASES = (ROOT / "eval-corpus" / "investigate" / "cases.yaml").read_text(encoding="utf-8")
DEFS = yaml.safe_load((ROOT / "definitions" / "investigate.yaml").read_text(encoding="utf-8"))
needs_db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL がない")

import investigate_bench as ib  # noqa: E402


def test_cases_are_well_formed():
    cases = ib.load_cases(CASES)
    assert len(cases) >= 5 and len({c["id"] for c in cases}) == len(cases)
    for c in cases:
        e = c["expected"]
        assert e["outcome"] in DEFS["outcomes"] and (e["fix_target"] in DEFS["fix_targets"] or e["fix_target"] == "none")
        assert all(s in DEFS["stop_reasons"] for s in e.get("stop_reason") or [])
        assert len(c["window"]) == 2 and c["fixed_in"] and len(c["engine_ref"]) == 40   # 直す前の版で読ませる
        assert e["outcome"] == "unidentified" or e["must_mention"]
    assert any(c["expected"]["outcome"] == "unidentified" for c in cases)   # 引き継ぐのが正しい件も入れる


def test_pass_rate_matches_definition():
    assert ib.PASS_RATE == DEFS["eval"]["pass_rate"]


def test_score():
    case = next(c for c in ib.load_cases(CASES) if c["id"] == "inv-danger-precision")
    good = {"outcome": "identified", "fix_target": "engine", "report": "危険の欄が内部のジョブの古い停止で埋まり、上限の件数から製品の停止が押し出された"}
    assert ib.score(case, good) == {"pass": True, "reasons": []}
    vague = {**good, "report": "停止が多かった"}
    r = ib.score(case, vague)
    assert not r["pass"] and len(r["reasons"]) == 2
    assert not ib.score(case, {**good, "fix_target": "operation"})["pass"]
    assert not ib.score(case, None)["pass"]
    roi = next(c for c in ib.load_cases(CASES) if c["id"] == "inv-roi-crash")
    assert ib.score(roi, {"outcome": "unidentified", "fix_target": "none", "handoff": {"stop_reason": "needs_code"}})["pass"]
    assert not ib.score(roi, {"outcome": "unidentified", "fix_target": "none", "handoff": {"stop_reason": "budget"}})["pass"]
    hyp = {"outcome": "identified", "fix_target": "engine", "report": "x",
           "handoff": {"hypotheses": [{"claim": "内部の委員会の停止が上限の件数を埋めた"}]}}
    assert ib.score(case, hyp)["pass"]   # 仮説に書いたものも数える


def test_bad_cases_are_rejected():
    with pytest.raises(ValueError):
        ib.load_cases("cases: [{id: a, question: q, expected: {}, engine_ref: 'main; rm -rf /'}]")
    with pytest.raises(ValueError):
        ib.load_cases("cases: [{id: a}]")


if URL:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb

    import desk_investigate as di


@pytest.fixture()
def cur():
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets, desk_conversations, desk_tokens restart identity cascade")
    yield c
    conn.close()


def finish(cur, jid, res, state="succeeded"):
    cur.execute("update jobs set state=%s, finished_at=now(), result=%s where id=%s returning *", (state, Jsonb(res), jid))
    return ib.record(cur, cur.fetchone(), res, state, None)


@needs_db
def test_run_scores_and_finishes(cur):
    cases = ib.load_cases(CASES)
    got = ib.enqueue(cur, cases, run="r1", only=["inv-danger-precision", "inv-roi-crash"])
    assert set(got["jobs"]) == {"inv-danger-precision", "inv-roi-crash"}
    cur.execute("select payload from events where name='job.enqueued' and job_id=%s", (got["jobs"]["inv-danger-precision"],))
    inp = cur.fetchone()["payload"]["input"]
    assert inp["engine_ref"] == "03318950c6ad8722db35559ae1a598a0c3c7b84e" and inp["window"][0].startswith("2026-11-08")
    assert ib.trial(cur) == {"rate": None, "pass_rate": ib.PASS_RATE}
    finish(cur, got["jobs"]["inv-danger-precision"], {"ok": True, "outcome": "identified", "fix_target": "engine", "cost_usd": 2.0,
                                                       "report": "内部の停止が上限の件数を埋めた"})
    st = ib.status(cur)
    assert (st["run"], st["done"], st["of"], st["passed"]) == ("r1", 1, 2, 1)
    finish(cur, got["jobs"]["inv-roi-crash"], {"ok": False}, state="failed")
    st = ib.status(cur, "r1")
    assert st["done"] == 2 and st["rate"] == 0.5 and st["cost_usd"] == 2.0
    assert ib.trial(cur) is None                                      # 合格線に届いたので試行中ではない
    cur.execute("select count(*) as n from events where name='investigate.bench_finished'")
    assert cur.fetchone()["n"] == 1
    # 2 回目の回は issue_number が重ならない
    assert len(ib.enqueue(cur, cases, run="r2")["jobs"]) == len(cases)


@needs_db
def test_bench_jobs_do_not_count_toward_daily_cap(cur):
    from datetime import datetime, timezone
    got = ib.enqueue(cur, ib.load_cases(CASES), run="r1", only=["inv-roi-crash"])
    finish(cur, got["jobs"]["inv-roi-crash"], {"ok": True, "outcome": "unidentified", "cost_usd": 50.0})
    assert di.spent_today(cur, datetime.now(timezone.utc)) == 0.0


def test_issueless_kinds_never_swap_labels():
    """Issue のない種類（issue_number が仮の負の数）を始めるときにラベルを替えない（2026-11-10、評価の 6 件が 404 で落ちた）"""
    import ast
    tree = ast.parse((Path(__file__).resolve().parents[1] / "dispatcher.py").read_text(encoding="utf-8"))
    consts = {t.id: ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
              for t in n.targets if isinstance(t, ast.Name) and t.id in ("NO_LABEL_KINDS", "ISSUELESS_KINDS")}
    assert set(consts["ISSUELESS_KINDS"]) <= set(consts["NO_LABEL_KINDS"])
    import desk_answer
    import desk_investigate
    import desk_line
    for k in (desk_line.KIND, desk_answer.KIND, desk_investigate.KIND):
        assert k in consts["ISSUELESS_KINDS"]


def test_relay_kinds_get_no_job_db():
    """中継の道具を使う種類にはジョブの DB を作らない（--network が 2 回付いて docker run が 125 で落ちた。2026-11-10）"""
    import ast
    import runner_relay
    tree = ast.parse((Path(__file__).resolve().parents[1] / "dispatcher.py").read_text(encoding="utf-8"))
    consts = {t.id: ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
              for t in n.targets if isinstance(t, ast.Name) and t.id == "NO_JOB_DB_KINDS"}
    assert set(runner_relay.KIND_TOOLS) <= set(consts["NO_JOB_DB_KINDS"])


def test_investigate_is_wired_through_run_job():
    """一次調査のジョブが run_job のどの分かれ道でも漏れていないこと（ラベル・ジョブの DB・標準入力・成功の判定。2026-11-10 に順に漏れが見つかった）"""
    import ast
    tree = ast.parse((Path(__file__).resolve().parents[1] / "dispatcher.py").read_text(encoding="utf-8"))
    names = ("NO_LABEL_KINDS", "NO_JOB_DB_KINDS", "PIPED_KINDS", "OK_RESULT_KINDS")
    consts = {t.id: ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
              for t in n.targets if isinstance(t, ast.Name) and t.id in names}
    for n in names:
        assert "investigate" in consts[n], n
    for k in ("desk_consult", "desk_answer", "qa"):   # 入力を標準入力で受け、PR を作らない種類は成功の判定にも入る
        assert k in consts["PIPED_KINDS"] and k in consts["OK_RESULT_KINDS"]
