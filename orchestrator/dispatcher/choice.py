"""人の選択と違和感の印（ADR 0025 の P4-7）。

デザイン検査役が人に回した案（design_variants）、デザインシステムのデザイナー役のコンセプトの案（concept_tone）と、内部品質の抜き取り（taste_sample、ADR 0019）を、同じ形で人に見せて受ける。
- 依頼: artifacts の HumanChoice（status: open）と events の choice.requested
- 選択: choice.made（選んだ案、推奨と一致したか、理由の一言）。デザインの線では、選ばれた案が実装の線の入口になる
- 戻す: choice.returned（どの案も選ばず、作り手に戻す）
- 違和感の印: taste.flagged（教師信号 taste_flag。signature は taste:<言語化の候補>。learning.yaml）

ここは記録だけで、次の工程を起こすのは supervisor（状態は制御プレーンに置く。ADR 0020）。
案の画像はオブジェクトストアの choices/ か design/（デザイナー役の案。design_line.store_files）の下に置き、操業コンソールが中継する（外部の URL を画面に出さない）。
"""
from __future__ import annotations

import re

from psycopg.types.json import Jsonb

KINDS = {"design_variants": "画面の案の選択", "concept_tone": "コンセプトとトーン＆マナーの選択", "taste_sample": "抜き取り"}
IMAGE_PREFIXES = ("choices/", "design/")
MAX_OPTIONS, MAX_LEN = 4, 1000
OPTION_ID = re.compile(r"^[A-Za-z0-9_-]{1,20}$")


def _clip(v, n=MAX_LEN) -> str:
    return str(v or "")[:n]


def validate(kind: str, options: list[dict]) -> str | None:
    """依頼の形を確かめる。問題があれば理由を返す"""
    if kind not in KINDS:
        return f"kind は {sorted(KINDS)} のどれか"
    if not isinstance(options, list) or not 1 <= len(options) <= MAX_OPTIONS:
        return f"options は 1〜{MAX_OPTIONS} 件"
    ids = [o.get("id") for o in options if isinstance(o, dict)]
    if len(ids) != len(options) or len(set(ids)) != len(ids) or not all(isinstance(i, str) and OPTION_ID.match(i) for i in ids):
        return "options の id は英数字・_・- の 20 文字以内で重複なし"
    for o in options:
        key = o.get("image_key")
        if key is not None and not image_key_ok(key):
            return f"image_key は {' か '.join(IMAGE_PREFIXES)} の下だけ"
    return None


def image_key_ok(key) -> bool:
    return isinstance(key, str) and key.startswith(IMAGE_PREFIXES) and ".." not in key


def request(cur, kind: str, correlation_id: str, question: str, options: list[dict], *, recommended: str | None = None,
            subject: dict | None = None, context: str = "", job_id: int | None = None, produced_by: str = "design") -> int:
    """人に選んでもらう依頼を作る。返り値は HumanChoice の artifact id"""
    err = validate(kind, options)
    if err:
        raise ValueError(err)
    if recommended is not None and recommended not in {o["id"] for o in options}:
        raise ValueError("recommended は options の id のどれか")
    payload = {"kind": kind, "status": "open", "question": _clip(question, 300), "context": _clip(context),
               "options": [{"id": o["id"], "label": _clip(o.get("label"), 80), "note": _clip(o.get("note")), "image_key": o.get("image_key")} for o in options],
               "recommended": recommended, "subject": {k: _clip(v, 300) for k, v in (subject or {}).items() if k in ("repo", "ticket", "pr_url", "uxspec")}}
    cur.execute("insert into artifacts(type, correlation_id, job_id, produced_by, payload) values ('HumanChoice',%s,%s,%s,%s) returning id",
                (correlation_id, job_id, produced_by, Jsonb(payload)))
    aid = cur.fetchone()["id"]
    cur.execute("insert into events(name, correlation_id, job_id, payload) values ('choice.requested',%s,%s,%s)",
                (correlation_id, job_id, Jsonb({"artifact_id": aid, "kind": kind, "options": len(options), "recommended": recommended})))
    return aid


def open_choices(cur, limit: int = 50) -> list[dict]:
    cur.execute("select id, correlation_id, created_at, payload from artifacts where type='HumanChoice' and payload->>'status'='open' "
                "order by id limit %s", (limit,))
    return [{"id": r["id"], "correlation_id": r["correlation_id"], "created_at": r["created_at"], **r["payload"]} for r in cur.fetchall()]


def get(cur, aid: int) -> dict | None:
    cur.execute("select id, correlation_id, payload from artifacts where id=%s and type='HumanChoice'", (aid,))
    r = cur.fetchone()
    return {"id": r["id"], "correlation_id": r["correlation_id"], **r["payload"]} if r else None


def find_open(cur, kind: str, repo: str, ticket: str) -> int | None:
    """subject の repo と ticket で開いている依頼を探す（factoryctl から選んだときに画面の依頼も閉じるため）"""
    cur.execute("select id from artifacts where type='HumanChoice' and payload->>'status'='open' and payload->>'kind'=%s "
                "and payload->'subject'->>'repo'=%s and payload->'subject'->>'ticket'=%s order by id desc limit 1", (kind, repo, ticket))
    r = cur.fetchone()
    return r["id"] if r else None


def _get_open(cur, aid: int):
    cur.execute("select id, correlation_id, payload from artifacts where id=%s and type='HumanChoice' for update", (aid,))
    r = cur.fetchone()
    if not r:
        return None, "no such choice"
    if r["payload"].get("status") != "open":
        return None, "already closed"
    return r, None


def choose(cur, aid: int, option_id: str, by: str, reason: str = "") -> tuple[bool, str]:
    r, err = _get_open(cur, aid)
    if err:
        return False, err
    p = r["payload"]
    if option_id not in {o["id"] for o in p["options"]}:
        return False, "no such option"
    agreed = None if p.get("recommended") is None else option_id == p["recommended"]
    cur.execute("update artifacts set payload = payload || %s where id=%s",
                (Jsonb({"status": "chosen", "chosen": option_id, "chosen_by": by, "reason": _clip(reason, 300)}), aid))
    cur.execute("insert into events(name, correlation_id, payload) values ('choice.made',%s,%s)",
                (r["correlation_id"], Jsonb({"artifact_id": aid, "kind": p["kind"], "option": option_id, "recommended": p.get("recommended"),
                                             "agreed": agreed, "by": by, "reason": _clip(reason, 300)})))
    return True, "chosen"


def return_to_maker(cur, aid: int, by: str, reason: str) -> tuple[bool, str]:
    """どの案も選ばず作り手に戻す。理由は必須（作り直しの入力になる）"""
    if not reason.strip():
        return False, "reason required"
    r, err = _get_open(cur, aid)
    if err:
        return False, err
    cur.execute("update artifacts set payload = payload || %s where id=%s",
                (Jsonb({"status": "returned", "chosen_by": by, "reason": _clip(reason, 300)}), aid))
    cur.execute("insert into events(name, correlation_id, payload) values ('choice.returned',%s,%s)",
                (r["correlation_id"], Jsonb({"artifact_id": aid, "kind": r["payload"]["kind"], "by": by, "reason": _clip(reason, 300)})))
    return True, "returned"


def signature(note: str) -> str:
    """違和感の言葉を束ねるための仮の signature。言語化の候補は learn が後で寄せる（ここでは空白を詰めて切るだけ）"""
    return "taste:" + re.sub(r"\s+", " ", note.strip())[:40]


def flag(cur, aid: int, note: str, by: str, option_id: str | None = None) -> tuple[bool, str]:
    """違和感の印。開いている依頼でも閉じた依頼でも付けられる（出口のキャプチャへの印もこの形で受ける）"""
    if not note.strip():
        return False, "note required"
    cur.execute("select correlation_id, payload from artifacts where id=%s and type='HumanChoice'", (aid,))
    r = cur.fetchone()
    if not r:
        return False, "no such choice"
    if option_id is not None and option_id not in {o["id"] for o in r["payload"]["options"]}:
        return False, "no such option"
    cur.execute("insert into events(name, correlation_id, payload) values ('taste.flagged',%s,%s)",
                (r["correlation_id"], Jsonb({"artifact_id": aid, "kind": r["payload"]["kind"], "option": option_id, "note": _clip(note, 300),
                                             "signature": signature(note), "subject": r["payload"].get("subject"), "by": by})))
    return True, "flagged"
