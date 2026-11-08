"""実装の工程のモデルの振り分け（2026-11-08 オーナー「確信度の高い工程から安いモデルを試す」）。

プランナーの見積もり（ADR 0036 の size）が小さいチケットのうち、決まった割合だけ実装を別の tier（Sonnet）で流し、
残り（同じ条件の既定の tier）と比べる。比べるのは工程ごとの記録（費用・作業と待ちの時間・一発で通ったか・混入した Defect）で、
建設日報の「工程ごとの ROI」（health/daily/stage_roi.py）が集計する。

- 決めるのはチケットの最初の実装を積むとき 1 回だけ。events の model.routed に残し、やり直し・続き・積み直しは同じ側で流す
- 条件に当たらないチケット（arm=out）も残す。比べる母数と、条件に当たる割合が分かるように
- 試す側で途中終了したら、続きは既定の tier に戻す（model.routed_back）。戻した数も比べる材料
- 検査・judge・QA は対象にしない

値は definitions/agents.yaml の experiments.implement_small_sonnet の写し（dispatcher のイメージには definitions/ を入れない。test_model_route.py が食い違いを見る）。
"""
from __future__ import annotations

import hashlib
import os

from psycopg.types.json import Jsonb

EXPERIMENT = {
    "id": "implement_small_sonnet",
    "step": "implement",
    "tier": "tier-small",
    "share": 0.2,
    "eligible": {"turns_band": ["S"], "max_layers": 1, "exclude_layers": ["db", "contract"]},
    "back_to_default_on": ["incomplete"],
}
UNCOUNTED_LAYERS = ("docs", "other")   # planner.yaml の sizing.uncounted_layers と同じ（切る基準と同じ数え方）


def share() -> float:
    """試す側に回す割合。運用の値なので環境変数 MODEL_ROUTE_SHARE で上書きできる（0 で止める）"""
    try:
        v = float(os.environ.get("MODEL_ROUTE_SHARE", EXPERIMENT["share"]))
    except ValueError:
        v = EXPERIMENT["share"]
    return min(max(v, 0.0), 1.0)


def eligible(size: dict | None) -> tuple[bool, str]:
    """見積もりが小さいチケットか。返り値は（当たるか、外れた理由）"""
    if not isinstance(size, dict):
        return False, "no_size"
    el = EXPERIMENT["eligible"]
    if size.get("turns_band") not in el["turns_band"]:
        return False, "band"
    layers = [l for l in size.get("layers") or [] if isinstance(l, str)]
    if any(l in el["exclude_layers"] for l in layers):
        return False, "excluded_layer"
    if len([l for l in layers if l not in UNCOUNTED_LAYERS]) > el["max_layers"]:
        return False, "layers"
    return True, ""


def bucket(corr: str) -> float:
    """チケットごとに決まる 0〜1 の値（同じチケットはいつも同じ側。乱数の種や時刻に依らない）"""
    return int(hashlib.sha256(f"{EXPERIMENT['id']}:{corr}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


def routed(cur, corr: str) -> dict | None:
    cur.execute("select payload from events where name='model.routed' and correlation_id=%s order by id desc limit 1", (corr,))
    r = cur.fetchone()
    return r["payload"] if r else None


def latest_size(cur, corr: str) -> tuple[dict | None, int | None]:
    cur.execute("select id, payload from artifacts where type='Plan' and correlation_id=%s order by id desc limit 1", (corr,))
    r = cur.fetchone()
    if not r:
        return None, None
    return (r["payload"] or {}).get("size"), r["id"]


def tier_for(cur, corr: str, default: str, *, reason: str | None = None) -> str:
    """実装のジョブを積むときの tier。最初の 1 回で決めて残し、あとは同じ側を返す。
    reason は続きの理由（continuation.REASONS）。試す側で back_to_default_on に当たれば既定の tier に戻す"""
    try:
        with cur.connection.transaction():   # 失敗しても呼び出し元のトランザクションを壊さない（セーブポイント）
            return _tier_for(cur, corr, default, reason)
    except Exception:  # noqa: BLE001  振り分けの失敗で実装を止めない（既定の tier で流す）
        return default


def _tier_for(cur, corr: str, default: str, reason: str | None) -> str:
    prev = routed(cur, corr)
    if prev is not None:
        if prev.get("arm") == "trial" and not prev.get("routed_back") and reason in EXPERIMENT["back_to_default_on"]:
            cur.execute("insert into events(name, correlation_id, payload) values ('model.routed_back',%s,%s)",
                        (corr, Jsonb({"experiment": EXPERIMENT["id"], "from": prev.get("tier"), "to": default, "reason": reason})))
            cur.execute("insert into events(name, correlation_id, payload) values ('model.routed',%s,%s)",
                        (corr, Jsonb({**prev, "tier": default, "routed_back": reason})))
            return default
        return prev.get("tier") or default
    size, plan_id = latest_size(cur, corr)
    ok, why = eligible(size)
    s = share()
    arm = ("trial" if bucket(corr) < s else "control") if ok else "out"
    tier = EXPERIMENT["tier"] if arm == "trial" else default
    cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values ('model.routed',%s,%s,%s) on conflict (dedupe_key) do nothing",
                (corr, Jsonb({"experiment": EXPERIMENT["id"], "step": EXPERIMENT["step"], "arm": arm, "eligible": ok, "why_not": why or None,
                              "tier": tier, "default_tier": default, "share": s, "plan_id": plan_id,
                              "size": {"turns_band": (size or {}).get("turns_band"), "layers": (size or {}).get("layers")} if isinstance(size, dict) else None}),
                 f"model.routed:{corr}"))
    return tier
