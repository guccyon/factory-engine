"""依頼者ごとの好み（ADR 0040 の決定 6）。制御プレーンの requester_preferences から、この回の依頼者・業務に効くものを選ぶ。

- 渡すのは active で confirmed のものだけ（知見と同じく、推し量りの段階では作業を変えない）
- 業務を書いた好みは、この回の業務かその祖先に当たるときだけ。書いていない好みはその依頼者の作業すべてに効く
- 渡した記録は requester_preference.used に id（P-NNNN）だけ残す。中身は events に書かない
- 書き込む口: 依頼者が窓口の API（MCP）で自分の好みを見る・足す・直す・消す。相談役・回答役が会話から拾った「依頼者が自分で言った好み」は、
  引用が依頼者の発言にそのままあるものだけを確認済みで残し、会話に知らせる。委員会は通さない
"""
from __future__ import annotations

import re

from psycopg.types.json import Jsonb

LIMIT = 20


def pid(n: int) -> str:
    return f"P-{int(n):04d}"


def _ancestors(node: str) -> set[str]:
    parts = node.split(".")
    return {".".join(parts[:i]) for i in range(1, len(parts) + 1)}


def applies(pref_work: list[str] | None, job_work: list[str]) -> bool:
    """好みの業務がこの回の業務（かその祖先）に当たるか。好みが業務を持たなければ当たる"""
    if not pref_work:
        return True
    near: set[str] = set()
    for w in job_work:
        near |= _ancestors(w)
    return bool(set(pref_work) & near)


def for_job(cur, product: str, requester: str | None, work: list[str]) -> list[dict]:
    """この回に渡す好み。依頼者が分からなければ渡さない"""
    if not requester or not product:
        return []
    # 表がまだない DB（007 を当てる前）でも相談を止めない。失敗は戻し、好みなしで進める。
    # 工場の接続は autocommit なので素の savepoint は使えない。transaction() は外に取引があれば savepoint、なければ取引を張る
    try:
        with cur.connection.transaction():
            cur.execute("select id, statement, work, axes, verified_at from requester_preferences "
                        "where product=%s and requester=%s and status='active' and confidence='confirmed' "
                        "order by verified_at desc, id desc limit %s", (product, requester, LIMIT * 3))
            rows = cur.fetchall()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for r in rows:
        if applies(r.get("work") or [], work):
            out.append({"id": pid(r["id"]), "statement": r["statement"], "work": list(r.get("work") or []),
                        "verified_at": str(r.get("verified_at") or "")})
        if len(out) >= LIMIT:
            break
    return out



# ------------------------------------------------------------------ 書き込む口（ADR 0040 の決定 6: 依頼者が見て直す・消す、会話から記録する）
# 業務のノード（definitions/work.yaml の nodes）。dispatcher のコンテナには definitions/ がないので写す（食い違いはテストで見る）
WORK_NODES = (
    "biz", "biz.discover", "biz.propose", "biz.quality_policy",
    "plan", "plan.requirements", "plan.workflow", "plan.route",
    "analyze", "analyze.report", "analyze.quick",
    "dev", "dev.design", "dev.design.system", "dev.design.screen", "dev.design.proposal", "dev.contract",
    "dev.build", "dev.describe", "dev.ci", "dev.review", "dev.review.inspect", "dev.review.judge", "dev.review.design",
    "ops", "ops.qa", "ops.diagnose", "ops.release", "ops.runtime", "ops.incident", "ops.operate",
    "desk", "desk.converse",
    "govern", "govern.design_review", "govern.health", "govern.patrol", "govern.incident_review", "govern.investigate",
    "govern.watch", "govern.budget",
    "learn", "learn.case", "learn.committee", "learn.signals",
)
MAX_STATEMENT = 400
MAX_PER_REQUESTER = 50       # 1 人の 1 製品に持つ好みの上限（active）。会話から際限なく増やさない
MAX_STATED_PER_JOB = 3       # 1 回の相談・回答から記録する上限
PID_RE = re.compile(r"^P-(\d{1,9})$")


class PrefError(Exception):
    """依頼者に返す誤り。status は HTTP の状態コード（desk.DeskError と同じ扱い）"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def parse_pid(v) -> int:
    m = PID_RE.match(str(v or ""))
    if not m:
        raise PrefError(400, "好みの id は P-NNNN")
    return int(m.group(1))


def clean_statement(v) -> str:
    s = " ".join(str(v or "").split())
    if not s:
        raise PrefError(400, "好み（statement）が空")
    if len(s) > MAX_STATEMENT:
        raise PrefError(413, f"好みは {MAX_STATEMENT} 文字まで")
    return s


def clean_work(v) -> list[str]:
    if v in (None, "", []):
        return []
    items = v if isinstance(v, list) else [v]
    bad = [str(w) for w in items if w not in WORK_NODES]
    if bad:
        raise PrefError(400, f"業務 {', '.join(bad[:3])} は definitions/work.yaml のノードにない（空ならその依頼者の作業すべてに効く）")
    return sorted(set(map(str, items)))


def view(r: dict) -> dict:
    return {"id": pid(r["id"]), "product": r["product"], "statement": r["statement"], "work": list(r.get("work") or []),
            "confidence": r["confidence"], "status": r["status"], "created_by": r["created_by"],
            "verified_at": str(r.get("verified_at") or ""), "updated_at": r["updated_at"].isoformat() if r.get("updated_at") else None}


def _event(cur, name: str, payload: dict):
    # 中身（statement）は書かない。人に紐づく情報を events に残さないため（id・製品・依頼者・業務だけ）
    cur.execute("insert into events(name, payload) values (%s,%s)", (name, Jsonb(payload)))


def _own(cur, who: dict, n: int, for_update: bool = True) -> dict:
    cur.execute("select * from requester_preferences where id=%s" + (" for update" if for_update else ""), (n,))
    r = cur.fetchone()
    # ほかの依頼者の好みは「ない」と同じに返す（あることも漏らさない）。運用主は対象の製品のものを直せる
    if not r or not _visible(who, r):
        raise PrefError(404, "そのような好みはない")
    return r


def _visible(who: dict, r: dict) -> bool:
    if who["role"] == "operator":
        return not who["products"] or r["product"] in who["products"]
    return r["requester"] == who["name"] and r["product"] in who["products"]


def list_for(cur, who: dict, product: str | None = None, requester: str | None = None) -> list[dict]:
    """自分の好みの一覧（仮説も含めて active なものすべて）。運用主は requester を指定して見られる"""
    name = requester if who["role"] == "operator" and requester else who["name"]
    q, args = "select * from requester_preferences where requester=%s and status='active'", [name]
    if product:
        q += " and product=%s"
        args.append(product)
    cur.execute(q + " order by product, id", args)
    return [view(r) for r in cur.fetchall() if _visible(who, r)]


def add(cur, who: dict, product: str, statement, work=None) -> dict:
    """依頼者が自分で言った好みを足す（確認済み）。同じ文の好みがあれば確認日を新しくするだけ"""
    if not product or (who["role"] != "operator" and product not in who["products"]) or \
            (who["role"] == "operator" and who["products"] and product not in who["products"]):
        raise PrefError(404, "その製品は扱えない")
    if who["role"] == "operator":
        raise PrefError(403, "好みは依頼者が自分で足す（運用主は直す・消すだけ）")
    r = store(cur, product, who["name"], clean_statement(statement), clean_work(work), "confirmed", "requester", [])
    return view(r)


def store(cur, product: str, requester: str, statement: str, work: list[str], confidence: str, created_by: str,
          evidence: list[int]) -> dict:
    cur.execute("select * from requester_preferences where product=%s and requester=%s and statement=%s and status='active' for update",
                (product, requester, statement))
    r = cur.fetchone()
    if r:
        conf = "confirmed" if "confirmed" in (r["confidence"], confidence) else r["confidence"]
        cur.execute("update requester_preferences set confidence=%s, work=%s, evidence=evidence || %s::bigint[], verified_at=current_date, "
                    "updated_at=now() where id=%s returning *", (conf, sorted(set(r["work"] or []) | set(work)) if r["work"] and work else [],
                                                                  evidence, r["id"]))
        r = cur.fetchone()
        _event(cur, "requester_preference.confirmed", {"id": pid(r["id"]), "product": product, "requester": requester, "by": created_by})
        return r
    cur.execute("select count(*) as n from requester_preferences where product=%s and requester=%s and status='active'", (product, requester))
    if cur.fetchone()["n"] >= MAX_PER_REQUESTER:
        raise PrefError(409, f"好みは 1 つの製品に {MAX_PER_REQUESTER} 件まで（要らないものを消してから足す）")
    cur.execute("insert into requester_preferences(product, requester, statement, work, confidence, evidence, created_by) "
                "values (%s,%s,%s,%s,%s,%s,%s) returning *", (product, requester, statement, work, confidence, evidence, created_by))
    r = cur.fetchone()
    _event(cur, "requester_preference.added", {"id": pid(r["id"]), "product": product, "requester": requester, "work": work,
                                               "confidence": confidence, "by": created_by})
    return r


def update(cur, who: dict, key, statement=None, work=None) -> dict:
    """直す。依頼者が直したものは確認済みにする（推し量りの仮説を依頼者が認めたのと同じ）"""
    r = _own(cur, who, parse_pid(key))
    if r["status"] != "active":
        raise PrefError(404, "そのような好みはない")
    st = clean_statement(statement) if statement is not None else r["statement"]
    wk = clean_work(work) if work is not None else list(r["work"] or [])
    conf = "confirmed" if who["role"] != "operator" else r["confidence"]
    cur.execute("update requester_preferences set statement=%s, work=%s, confidence=%s, verified_at=current_date, updated_at=now() "
                "where id=%s returning *", (st, wk, conf, r["id"]))
    r = cur.fetchone()
    _event(cur, "requester_preference.updated", {"id": pid(r["id"]), "product": r["product"], "requester": r["requester"], "work": wk,
                                                 "confidence": conf, "by": who["name"]})
    return view(r)


def delete(cur, who: dict, key) -> dict:
    """消す。行ごと消し、中身を残さない（git に置かない理由と同じ。消した記録は id だけ events に残る）"""
    r = _own(cur, who, parse_pid(key))
    cur.execute("delete from requester_preferences where id=%s", (r["id"],))
    _event(cur, "requester_preference.deleted", {"id": pid(r["id"]), "product": r["product"], "requester": r["requester"], "by": who["name"]})
    return {"id": pid(r["id"]), "deleted": True}


def record_stated(cur, product: str, requester: str | None, items, msgs: list[dict]) -> list[str]:
    """相談役・回答役が会話から拾った「依頼者が自分で言った好み」を記録する（確認済み）。
    quote が依頼者の発言にそのまま含まれるものだけを採る（工場の言い換えや推測を依頼者の言葉として残さない）。根拠はその発言の events の id"""
    if not requester or not product or not isinstance(items, list):
        return []
    said = [(m.get("id"), str(m.get("body") or "")) for m in msgs if m.get("role") == "requester"]
    out = []
    for it in items[:MAX_STATED_PER_JOB]:
        if not isinstance(it, dict):
            continue
        quote = " ".join(str(it.get("quote") or "").split())
        if len(quote) < 4:
            continue
        ev = [int(i) for i, body in said if i is not None and quote in " ".join(body.split())]
        if not ev:
            continue
        try:
            with cur.connection.transaction():
                r = store(cur, product, requester, clean_statement(it.get("statement") or quote), clean_work(it.get("work")), "confirmed",
                          "requester", ev[:5])
            out.append(pid(r["id"]))
        except Exception:  # noqa: BLE001  1 件の誤り（業務の語彙の外・上限）で相談の記録は止めない
            continue
    return out


def stated_note(ids: list[str]) -> str:
    """会話に書く知らせ。何を覚えたかを依頼者が見て、直す・消す道を示す"""
    return (f"ご自身の言葉から、好みとして {', '.join(ids)} を覚えました。次から窓口と工場の作業で踏まえます。"
            "一覧を見る・直す・消すは desk_preferences / desk_preference_set / desk_preference_delete（または /api/desk/preferences）で。")
