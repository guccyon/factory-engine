"""judge が人に回した判断に、受信トレイでその場で答える（ADR 0034）。TEST_DATABASE_URL の Postgres で確かめる（なければ飛ばす）。

11-07 rally #921・#930・#970: 停止 judge: の件は PR へのリンクだけで、問いも選択肢も答える場所も受信トレイに出ていなかった。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL がない")

if URL:
    os.environ.setdefault("DATABASE_URL", URL)
    os.environ.setdefault("GH_TOKEN", "x")
    os.environ.setdefault("NOTIFIER", "log")
    import psycopg
    from psycopg.rows import dict_row

    import inbox
    import judgment
    import stoppage
    import supervisor

REPO = "o/r"
PR = f"https://github.com/{REPO}/pull/921"
CORR = f"github:{REPO}#900"
P = {"repo": REPO, "number": 921, "url": PR, "corr": CORR}


@pytest.fixture()
def cur():
    conn = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    c = conn.cursor()
    c.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
    yield c
    conn.close()


def judge(cur, verdicts, defects=None, job_id=None):
    """仮決め 2 件（と検査の指摘）を judge が裁いた記録を作り、人に回したものがあれば停止 judge: を開く（dispatcher.act_on_judgment と同じ）"""
    judgment.record_provisional(cur, CORR, job_id, PR, "### ui start: いつから通話を始められるか\n- 選んだ: 開始時刻ちょうどから\n"
                                                   "- 他の案: 5 分前から入室前チェック\n- 確信度: 0.5\n"
                                                   "### scope copy: 開始前の文言\n- 選んだ: 1 文\n- 確信度: 0.6\n")
    inp = judgment.judge_input(cur, PR, "attended")
    inp["defects"] = defects or []
    ids = {p["question"]: p["artifact_id"] for p in inp["provisional"]}
    vs = [{"artifact_id": ids[q], **v} for q, v in verdicts.items()]
    ds = [{"index": i, "verdict": "escalate", "confidence": 0.4, "evidence": "e", "interpretation": "直すかは人が決める", "recommend": "adopt"}
          for i, _ in enumerate(defects or [])]
    out = judgment.record_judged(cur, CORR, job_id, PR, inp, {"provisional": vs, "defects": ds}, "attended", "test")
    if out["escalated"]:
        stoppage.open_stoppage(cur, f"judge:{PR}", "human", f"judge が {len(out['escalated'])} 件を人に回した",
                               waiting_for="人の判断", correlation_id=CORR, detected_by="supervisor", quiet=True)
    return out


ESC = {"verdict": "escalate", "confidence": 0.5, "evidence": "e", "interpretation": "接続確認ができなくなる。デモなので今のままを推す", "recommend": "adopt"}
DEFECT = {"rubric_id": "copy.two_sentences", "severity": "minor", "location": "apps/student/lib/call.dart", "evidence": "1 文で次の操作を案内していない",
          "remediation_hint": "2 文にする"}


def by_q(items):
    return {i["title"].split(": ", 1)[1]: i for i in items}


def test_questions_options_and_recommendation_show_in_the_inbox(cur):
    judge(cur, {"いつから通話を始められるか": ESC,
                "開始前の文言": {**ESC, "recommend": "replace", "replacement": "2 文にする", "interpretation": "基準は 2 文"}}, [DEFECT])
    xs = inbox.items(cur)
    assert [i["kind"] for i in xs] == ["judgment"] * 3                    # 停止の 1 件ではなく、問い 1 つずつ
    q = by_q(xs)
    start = q["いつから通話を始められるか"]
    assert start["title"].startswith("o/r#921 の判断（画面と操作）") and start["url"] == PR and start["who"] == "owner"
    detail = {d["label"]: d["text"] for d in start["detail"]}
    assert detail["今の案"] == "開始時刻ちょうどから" and detail["他の案"] == "5 分前から入室前チェック"
    assert "デモなので" in detail["見立て"]
    a = start["answer"]
    assert a["type"] == "choose" and [o["id"] for o in a["options"]] == ["keep"] and a["recommended"] == "keep" and a["effect"]
    assert a["extra"][0]["id"] == "change" and a["extra"][0]["note_required"]
    copy = q["開始前の文言"]["answer"]
    assert [o["id"] for o in copy["options"]] == ["keep", "replace"] and copy["recommended"] == "replace"
    assert {d["label"]: d["text"] for d in q["開始前の文言"]["detail"]}["おすすめの案"] == "2 文にする"
    defect = next(i for i in xs if "copy.two_sentences" in i["title"])
    assert [o["id"] for o in defect["answer"]["options"]] == ["fix", "dismiss"] and defect["answer"]["recommended"] == "fix"
    v = inbox.view(cur)
    assert v["count"] == 3 and v["kinds"]["judgment"] and all("target" not in i["answer"] for i in v["items"])


def test_answers_record_the_decision_and_close_the_stoppage_when_all_are_decided(cur):
    judge(cur, {"いつから通話を始められるか": ESC, "開始前の文言": ESC}, [DEFECT])
    q = by_q(inbox.items(cur))
    k_start, k_copy = q["いつから通話を始められるか"]["key"], q["開始前の文言"]["key"]
    k_def = next(k for t, k in ((t, i["key"]) for t, i in q.items()) if "copy.two_sentences" in t)

    assert inbox.answer(cur, k_start, "keep", "console", "console")[0] == "answered"
    left = inbox.items(cur)
    assert len(left) == 2 and not any(i.get("answered") for i in left)     # 1 つ答えても同じ PR の残りの問いは開いたまま
    assert stoppage.open_keys(cur, f"judge:{PR}")
    assert inbox.answer(cur, k_start, "keep", "slack:U1", "slack")[0] == "already"

    assert inbox.answer(cur, k_copy, "change", "console", "console")[0] == "invalid"   # 別の答えには一言が要る
    assert inbox.answer(cur, k_copy, "change", "console", "console", note="生徒アプリも本部画面も 2 文にそろえる")[0] == "answered"
    assert stoppage.open_keys(cur, f"judge:{PR}")
    res, msg = inbox.answer(cur, k_def, "fix", "console", "console")
    assert res == "answered" and "すべて決まった" in msg
    assert not stoppage.open_keys(cur, f"judge:{PR}") and inbox.items(cur) == []
    assert judgment.decisions_settled(cur, PR)

    fixes = judgment.pending_fixes(cur, PR)
    assert [f["decision"] for f in fixes] == ["change", "fix"]
    assert fixes[0]["fix"]["rubric_id"] == "human_decision" and "2 文にそろえる" in fixes[0]["fix"]["remediation_hint"]
    assert fixes[1]["fix"]["rubric_id"] == "copy.two_sentences"
    cur.execute("select payload->'human_grade' as g from artifacts where id=%s", (int(k_start.split(":")[1]),))
    assert cur.fetchone()["g"] == {"decision": "keep", "note": None, "by": "console", "via": "console"}
    cur.execute("select count(*) as n from events where name='judgment.graded'")
    assert cur.fetchone()["n"] == 0                                        # judge との一致率には数えない


def test_human_fixes_go_to_the_same_worker_and_hold_the_merge(cur, monkeypatch):
    judge(cur, {"いつから通話を始められるか": ESC, "開始前の文言": ESC})
    for i in inbox.items(cur):
        opt = "change" if "文言" in i["title"] else "keep"
        assert inbox.answer(cur, i["key"], opt, "console", "console", note="2 文にする" if opt == "change" else "")[0] == "answered"
    assert judgment.pending_fixes(cur, PR)
    calls = []
    monkeypatch.setattr(supervisor, "gh", lambda *a, **k: calls.append(a))
    pr = {"head": {"ref": "factory/issue-900-job-1", "sha": "abc", "repo": {"full_name": REPO}}, "labels": [{"name": "factory:needs-judgment"}]}
    assert supervisor.waiting_for_human(cur, PR, pr) is False                # 判断がそろえば、ラベルが残っていても人は待っていない
    supervisor.human_fix_step(cur, P, pr)
    cur.execute("select j.id, e.payload from jobs j join events e on e.job_id=j.id and e.name='job.enqueued'")
    [j] = cur.fetchall()
    assert j["payload"]["continue"]["reason"] == "review_fix" and j["payload"]["input"]["by"] == "human"
    assert j["payload"]["input"]["defects"][0]["rubric_id"] == "human_decision"
    assert judgment.pending_fixes(cur, PR) == []
    supervisor.human_fix_step(cur, P, pr)                                    # 渡した直しは二度渡さない
    cur.execute("select count(*) as n from jobs")
    assert cur.fetchone()["n"] == 1
    cur.execute("update jobs set state='succeeded'")
    supervisor.human_fix_step(cur, P, pr)                                    # 直しがなく判断がそろったら、残ったラベルを外す
    assert calls and calls[-1][0] == "DELETE" and "factory%3Aneeds-judgment" in calls[-1][1]


def test_human_fix_is_not_capped_by_the_continue_limit(cur, monkeypatch):
    import continuation
    monkeypatch.setattr(continuation, "CONTINUE_MAX", 0)
    assert continuation.enqueue(cur, repo=REPO, pr_number=921, pr_url=PR, branch="factory/issue-900-job-1", head_sha="a",
                                reason="review_fix", by="judge", signal={}) == "limit"
    assert isinstance(continuation.enqueue(cur, repo=REPO, pr_number=921, pr_url=PR, branch="factory/issue-900-job-1", head_sha="a",
                                           reason="review_fix", by="human", signal={}, limit=False), int)


def test_forced_escalation_keeps_the_judge_verdict_as_the_recommendation(cur):
    judgment.record_provisional(cur, CORR, None, PR, "### aesthetic badge: 札の色\n- 選んだ: 赤\n- 確信度: 0.9\n")
    inp = judgment.judge_input(cur, PR, "attended")
    [p] = inp["provisional"]
    out = judgment.record_judged(cur, CORR, None, PR, inp, {"provisional": [{"artifact_id": p["artifact_id"], "verdict": "replace", "replacement": "橙",
                                                                             "confidence": 0.9, "evidence": "e", "interpretation": "i"}]}, "attended", "t")
    assert out["escalated"] and not out["replaced"]                        # level がないので人へ（委任の範囲の外）
    cur.execute("select payload from artifacts where id=%s", (out["escalated"][0]["artifact_id"],))
    j = cur.fetchone()["payload"]
    assert (j["recommend"], j["recommended_replacement"], j["decision"]) == ("replace", "橙", "赤")   # 今の案は替えない


def test_old_stoppage_without_records_stays_a_link(cur):
    stoppage.open_stoppage(cur, f"judge:{PR}", "human", "judge が 1 件を人に回した", waiting_for="人の判断", correlation_id=CORR,
                           detected_by="supervisor", quiet=True)
    [i] = inbox.items(cur)
    assert i["kind"] == "stoppage" and i["answer"] is None and i["url"] == PR
