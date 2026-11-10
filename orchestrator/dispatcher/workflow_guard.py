"""ワークフローの上限を守らせる（ADR 0038 の順番 2）。決定的（LLM を使わない）。supervisor は数えて比べ、決まった動きをするだけ。

最初に守らせるのは衝突の解消の回数（retries.conflict）。2026-11-07〜09 の実績で、63 チケットが衝突の解消を 136 回行い、
rally #1054 は 13 回解き続けていた。上限はチケットごとの合計（head をまたいで数える）で、宣言（Plan の workflow）がなければ既定値の 3 回。

上限に当たったときの動き（ADR 0038 の決定 7）:
  1. 待つ: 最後に衝突したファイル（conflict.files）に触る、流れているほかのチケットが終わるまで、衝突を解かない（workflow.enforced kind=conflict_wait）。
     待っている間は、ci_failure_step も人への停止を開かない
  2. 待ちが明けたら（触るチケットがなかったときはすぐ）、もう 1 回だけ解く（kind=conflict_retry）
  3. それでも衝突したら、もう解かない（kind=conflict_human）。ci_failure_step が人への停止（ci:）を開き、上限に当たったことを書く

WORKFLOW_ENFORCE（既定 conflict）で守らせる上限を選ぶ。空にすると守らせず、記録（workflow_shadow）だけに戻る。
"""
from __future__ import annotations

import os

from psycopg.types.json import Jsonb

import serialize
import workflow_shadow

ENFORCE = {x.strip() for x in os.environ.get("WORKFLOW_ENFORCE", "conflict").split(",") if x.strip()}


def conflicts(cur, corr: str) -> int:
    """チケットの衝突の解消の回数（head・PR をまたいだ合計）"""
    cur.execute("select count(*) as n from events where name='continue.enqueued' and correlation_id=%s and payload->>'reason'='conflict'", (corr,))
    return int(cur.fetchone()["n"])


def last_conflict_files(cur, corr: str) -> list[str]:
    cur.execute("select payload->'files' as f from events where name='conflict.files' and correlation_id=%s order by id desc limit 1", (corr,))
    r = cur.fetchone()
    return list((r or {}).get("f") or [])


def _enforced(cur, corr: str, kind: str) -> dict | None:
    cur.execute("select payload from events where name='workflow.enforced' and correlation_id=%s and payload->>'kind'=%s order by id desc limit 1",
                (corr, kind))
    r = cur.fetchone()
    return r["payload"] if r else None


def _record(cur, corr: str, kind: str, payload: dict, key: str) -> bool:
    cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values ('workflow.enforced',%s,%s,%s) "
                "on conflict (dedupe_key) do nothing returning id", (corr, Jsonb({"kind": kind, **payload}), f"workflow.enforced:{key}"))
    return cur.fetchone() is not None


def touching(files: list[str], inflight: dict[str, set[str]], me: str) -> list[str]:
    """files のどれかに触る、流れているほかのチケット"""
    out = []
    for corr, fs in sorted(inflight.items()):
        if corr != me and any(serialize.touches(fs, f) for f in files):
            out.append(corr)
    return out


def conflict_gate(cur, corr: str, pr_url: str, inflight_fn) -> str:
    """衝突の解消を積んでよいか。返り値は ok（積む）・wait（待つ）・human（もう解かない）。inflight_fn は流れているチケットとファイルを返す
    （GitHub を引くので、上限に当たったときだけ呼ぶ）"""
    if "conflict" not in ENFORCE:
        return "ok"
    caps, wf = workflow_shadow.caps_of(cur, corr)
    cap = int(caps.get("retries.conflict") or 0)
    n = conflicts(cur, corr)
    if n < cap:
        return "ok"
    base = {"pr_url": pr_url, "cap": cap, "conflicts": n, "plan_id": (wf or {}).get("plan_id"), "default": wf is None}
    if _enforced(cur, corr, "conflict_human"):
        return "human"
    if n > cap:   # 待ちの後の 1 回も衝突した（上限 + 1 回まで解いた）
        _record(cur, corr, "conflict_human", base, f"human:{corr}")
        return "human"
    files = last_conflict_files(cur, corr)
    behind = touching(files, inflight_fn(), corr) if files else []
    waited = _enforced(cur, corr, "conflict_wait")
    if behind:
        if not waited:
            _record(cur, corr, "conflict_wait", {**base, "files": files, "behind": behind}, f"wait:{corr}")
        return "wait"
    _record(cur, corr, "conflict_retry", {**base, "files": files, "waited": bool(waited)}, f"retry:{corr}")
    return "ok"


def waiting(cur, corr: str) -> bool:
    """衝突の上限に当たって待っている（人への停止はまだ開かない）"""
    return "conflict" in ENFORCE and _enforced(cur, corr, "conflict_wait") is not None \
        and _enforced(cur, corr, "conflict_retry") is None and _enforced(cur, corr, "conflict_human") is None


def capped(cur, corr: str) -> dict | None:
    """衝突の上限に当たり、もう解かない（人への停止の文に使う）"""
    return _enforced(cur, corr, "conflict_human") if "conflict" in ENFORCE else None


# ------------------------------------------------------------------ プランナーを通す（ADR 0038 の追記、2026-11-06 オーナー）
# 複数の役のワークフローを経由して成果を出すタスクは、必ずプランナーを通して、ステップ・厚み・モデル・上限を決める。
# 特定の職能への定型の単発の依頼（決定論の取りまとめ、1 つの役だけの依頼）は通さなくてよい。
# 工場の内部の起票（知見の担保・分析役・差し戻しの直しなど）は factory:planned を直接付けてきたので、指示役が投入の直前に
# Plan のないチケットを factory:plan に回す。起票する側を全部直さなくても、この 1 か所で必ず通る。
PLAN_GATE = os.environ.get("PLAN_GATE", "1") == "1"
PLANNER_LABEL = "factory:plan"   # プランナーの入口（planner.py の TRIGGER_LABEL）
DIRECT_LABELS = {"factory:changelog", "factory:direct"}   # 単発の依頼の印。changelog は決定論の取りまとめ、direct は 1 つの役への定型の依頼


def planned_by(cur, repo: str, issue: int) -> str | None:
    """このチケットがプランナーの判断の下にあるか。あればその根拠（plan・split_child）、なければ None"""
    corr = f"github:{repo}#{issue}"
    cur.execute("select 1 from artifacts where type='Plan' and correlation_id=%s limit 1", (corr,))
    if cur.fetchone():
        return "plan"
    cur.execute("select 1 from events where name='plan.split' and payload->>'repo'=%s and (payload->>'child'=%s or payload->'children' @> %s::jsonb) limit 1",
                (repo, str(issue), f"[{int(issue)}]"))
    return "split_child" if cur.fetchone() else None


def needs_planner(cur, repo: str, issue: int, labels: list[str]) -> bool:
    """投入の前にプランナーへ回すべきか（Plan がなく、単発の印もない）"""
    if not PLAN_GATE or DIRECT_LABELS & set(labels):
        return False
    return planned_by(cur, repo, issue) is None


def record_rerouted(cur, repo: str, issue: int, labels: list[str]) -> bool:
    cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values ('plan.rerouted',%s,%s,%s) "
                "on conflict (dedupe_key) do nothing returning id",
                (f"github:{repo}#{issue}", Jsonb({"repo": repo, "issue": issue, "labels": sorted(labels)}), f"plan.rerouted:{repo}#{issue}"))
    return cur.fetchone() is not None
