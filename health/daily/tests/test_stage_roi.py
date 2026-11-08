"""工程ごとの ROI（stage_roi.py。2026-11-08「確信度の高い工程から安いモデルを試す」）:
工程×モデルの費用・時間・一発で通ったか・混入した Defect、ボトルネック、モデルの試しの比べ"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import stage_roi as roi  # noqa: E402
import status_report as sr  # noqa: E402

T0 = datetime(2026, 11, 8, 0, 0, tzinfo=timezone.utc)
TIERS = {"tier-large": "Opus", "tier-judge": "Opus", "tier-small": "Sonnet", "tier-light": "Haiku"}


def job(i, corr, kind="implement", tier="tier-large", cost=1.0, wait=10, work=30, state="succeeded", cont=None, attempt=1, at=0):
    q = T0 + timedelta(minutes=at)
    return {"id": i, "corr": corr, "kind": kind, "model_tier": tier, "state": state, "attempt": attempt, "cont": cont, "cost": cost,
            "enqueued_at": q, "started_at": q + timedelta(minutes=wait), "finished_at": q + timedelta(minutes=wait + work)}


def test_tier_models_from_definitions():
    tm = roi.tier_models()
    assert tm["tier-large"] == "Opus" and tm["tier-small"] == "Sonnet" and tm["tier-light"] == "Haiku"


def test_stages_first_pass_defects_and_bottleneck():
    jobs = [job(1, "a", cost=4, at=0), job(2, "a", kind="judge", tier="tier-judge", cost=1, wait=60, work=5, at=40),
            job(3, "a", cost=2, cont="review_fix", at=120),                                     # a の実装は一発で通らなかった
            job(4, "b", cost=3, at=0), job(5, "b", kind="judge", tier="tier-judge", cost=1, wait=60, work=5, at=40),
            job(6, "b", cost=1, cont="conflict", at=200),                                       # 衝突の解消は一発の判定に数えない
            job(7, "b", kind="body_fix", tier="tier-light", cost=0.1, wait=1, work=1, state="failed", at=210)]
    defects = [{"introduced_job": 1, "found_in": "pr"}, {"introduced_job": 1, "found_in": "ci"}, {"found_in": "qa"}]
    v = roi.compute(jobs, {}, defects, {}, TIERS)
    st = {(r["stage"], r["model"]): r for r in v["stages"]}
    impl = st[("implement", "Opus")]
    assert impl["jobs"] == 2 and impl["cost_usd"] == 7 and impl["first_pass"] == 0.5 and impl["defects"] == 2
    assert impl["work_min"] == 30 and impl["wait_min"] == 10
    assert st[("judge", "Opus")]["first_pass"] == 1.0 and st[("judge", "Opus")]["wait_min"] == 60
    assert st[("body_fix", "Haiku")]["first_pass"] == 0.0
    assert st[("implement:review_fix", "Opus")]["ja"] == "直し（指摘）"
    bn = v["bottleneck"]
    assert bn["cost"]["stage"] == "implement" and bn["defects"]["stage"] == "implement" and bn["defects"]["n"] == 2
    assert bn["time"]["stage"] == "judge"   # 待ちが長い工程が時間のボトルネックになる
    assert 0 < bn["wait_share"] < 1
    assert "first_pass" not in bn         # 5 件に届く工程がないうちは一発の割合で比べない
    assert v["experiment"] is None
    text = "\n".join(roi.fmt(v))
    assert "ボトルネック" in text and "判定（judge）" in text and "PR 本文の直し" in text and "Haiku" in text


def test_experiment_compares_trial_and_control():
    routes, jobs, merges, i = {}, [], {}, 0
    for n in range(6):
        for arm, tier, cost in (("trial", "tier-small", 1.0), ("control", "tier-large", 4.0)):
            corr = f"{arm}{n}"
            routes[corr] = {"experiment": "implement_small_sonnet", "arm": arm, "tier": tier}
            i += 1
            jobs.append(job(i, corr, tier=tier, cost=cost))
            if arm == "trial" and n == 0:   # 試す側の 1 件は指摘の直しが要った
                i += 1
                jobs.append(job(i, corr, tier=tier, cost=1.0, cont="review_fix", at=100))
            merges[corr] = T0 + timedelta(hours=3)
    routes["out1"] = {"experiment": "implement_small_sonnet", "arm": "out", "tier": "tier-large"}
    x = roi.compute(jobs, routes, [], merges, TIERS)["experiment"]
    a, b = x["arms"]["trial"], x["arms"]["control"]
    assert a["tickets"] == 6 and b["tickets"] == 6 and a["enough"] and b["enough"] and x["out"] == 1
    assert a["first_pass"] == 5 / 6 and b["first_pass"] == 1.0
    assert a["cost_per_merged"] == 7 / 6 and b["cost_per_merged"] == 4.0
    assert round(x["cost_ratio"], 3) == round((7 / 6) / 4.0, 3)
    assert a["lead_h_median"] == 3.0
    text = "\n".join(roi.experiment_lines(x))
    assert "試す側（Sonnet）" in text and "既定の側の 29%" in text


def test_empty_and_unreadable():
    assert "読めません" in roi.fmt(None)[0]
    v = roi.compute([], {}, [], {}, TIERS)
    assert "ありません" in roi.fmt(v)[0] and roi.daily_line(v) is None


def test_report_has_section():
    v = roi.compute([job(1, "a")], {}, [], {}, TIERS)
    snap = {"date": "2026-11-08", "progress": {"overall": 0.5, "phases": []}, "needs": {}, "activity": {}, "stage_roi": v}
    assert any(l.startswith("## 工程ごとの ROI") for l in sr.render_dashboard(snap, []).split("\n"))


def test_view_against_control_plane():
    import os
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        import pytest
        pytest.skip("TEST_DATABASE_URL がない")
    import psycopg
    from psycopg.types.json import Jsonb
    with psycopg.connect(url, autocommit=True) as conn:
        cur = conn.cursor()
        cur.execute("truncate events, artifacts, jobs, tickets restart identity cascade")
        cur.execute("insert into jobs(correlation_id, repo, issue_number, kind, model_tier, state, enqueued_at, started_at, finished_at, result) "
                    "values ('github:o/app#1','o/app',1,'implement','tier-small','succeeded', now()-interval '2 hours', now()-interval '90 minutes', now()-interval '1 hour', %s)",
                    (Jsonb({"pr_url": "https://github.com/o/app/pull/2"}),))
        cur.execute("insert into events(name, correlation_id, job_id, payload) values ('job.finished','github:o/app#1',1,%s)", (Jsonb({"state": "succeeded", "cost_usd": 1.5}),))
        cur.execute("insert into events(name, correlation_id, payload) values ('model.routed','github:o/app#1',%s)",
                    (Jsonb({"experiment": "implement_small_sonnet", "arm": "trial", "tier": "tier-small"}),))
        cur.execute("insert into events(name, correlation_id, payload) values ('defect.recorded','github:o/app#1',%s)", (Jsonb({"found_in": "ci", "introduced_job": 1}),))
        cur.execute("insert into events(name, correlation_id, payload) values ('pr.merged','github:o/app#1',%s)", (Jsonb({"pr_url": "https://github.com/o/app/pull/2"}),))
        v = roi.view(conn)
    assert v["jobs"] == 1 and v["stages"][0]["model"] == "Sonnet" and v["stages"][0]["cost_usd"] == 1.5 and v["stages"][0]["defects"] == 1
    assert round(v["stages"][0]["wait_min"]) == 30 and v["experiment"]["arms"]["trial"]["merged"] == 1
