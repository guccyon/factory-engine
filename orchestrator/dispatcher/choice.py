"""人の選択と違和感の印（ADR 0025 の P4-7）。

デザイン検査役が人に回した案（design_variants）、デザインシステムのデザイナー役のコンセプトの案（concept_tone）と、内部品質の抜き取り（taste_sample、ADR 0019）を、同じ形で人に見せて受ける。
- 依頼: artifacts の HumanChoice（status: open）と events の choice.requested
- 選択: choice.made（選んだ案、推奨と一致したか、理由の一言）。デザインの線では、選ばれた案が実装の線の入口になる
- 戻す: choice.returned（どの案も選ばず、作り手に戻す）
- 違和感の印: taste.flagged（教師信号 taste_flag。signature は taste:<業務>:<観点>。ADR 0040 の決定 9、learning.yaml）
- 実装後の画面（exit_capture。exit_capture.py が CI のキャプチャから作る。ADR 0025 の 4）: 選ぶものはなく、画面ごとに違和感の印を付けるか、
  「見た」で閉じる（choice.seen。違和感のなかった画面として負例の材料）。新しい head のキャプチャや期限で閉じる（choice.closed）

ここは記録だけで、次の工程を起こすのは supervisor（状態は制御プレーンに置く。ADR 0020）。
案の画像はオブジェクトストアの choices/ か design/（デザイナー役の案。design_line.store_files）の下に置き、操業コンソールが中継する（外部の URL を画面に出さない）。
"""
from __future__ import annotations

import re

from psycopg.types.json import Jsonb

KINDS = {"design_variants": "画面の体験の案の選択", "design_look": "画面の見た目の案の選択", "concept_tone": "コンセプトとトーン＆マナーの選択", "taste_sample": "抜き取り",
         "exit_capture": "実装後の画面"}
IMAGE_PREFIXES = ("choices/", "design/")
MAX_OPTIONS, MAX_LEN = 4, 1000
MAX_OPTIONS_BY_KIND = {"exit_capture": 12}   # 実装後の画面は選ぶものでなく見るもの。画面の数だけ並べる（多ければ残りは成果物で）
SUBJECT_KEYS = ("repo", "ticket", "pr_url", "uxspec", "app", "head_sha", "artifact_url")
OPTION_ID = re.compile(r"^[A-Za-z0-9_-]{1,20}$")
# 違和感の印の束ね方（ADR 0040 の決定 9）。束の鍵は taste:<業務>:<観点>。文面は材料に残し、鍵に入れない（文面ごとに束が割れる #264）。
# 業務は依頼の種類から（definitions/work.yaml のノード）。観点は definitions/design_guideline.yaml の節の id
# （dispatcher のコンテナには definitions/ がないので写す。食い違いはテストで見る）。語彙の外・付いていない印は unclassified
CHOICE_WORK = {"design_variants": "dev.design.screen", "design_look": "dev.design.screen", "concept_tone": "dev.design.system",
               "taste_sample": "dev.design.screen", "exit_capture": "dev.design.screen"}
VIEWPOINTS = ("principles", "concept_tone", "foundations", "components", "patterns", "content", "accessibility", "platforms",
              "data_visualization", "governance")
UNCLASSIFIED = "unclassified"


def _clip(v, n=MAX_LEN) -> str:
    return str(v or "")[:n]


def validate(kind: str, options: list[dict]) -> str | None:
    """依頼の形を確かめる。問題があれば理由を返す"""
    if kind not in KINDS:
        return f"kind は {sorted(KINDS)} のどれか"
    most = MAX_OPTIONS_BY_KIND.get(kind, MAX_OPTIONS)
    if not isinstance(options, list) or not 1 <= len(options) <= most:
        return f"options は 1〜{most} 件"
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
               "recommended": recommended, "subject": {k: _clip(v, 300) for k, v in (subject or {}).items() if k in SUBJECT_KEYS}}
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
    if p.get("kind") == "exit_capture":
        return False, "exit_capture は選ばない（印を付けるか、見たで閉じる）"
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


def mark_seen(cur, aid: int, by: str, note: str = "") -> tuple[bool, str]:
    """実装後の画面を見て閉じる。印を付けなかった画面は「違和感のなかった画面」として残る（taste_flag の負例の材料）"""
    r, err = _get_open(cur, aid)
    if err:
        return False, err
    p = r["payload"]
    if p.get("kind") != "exit_capture":
        return False, "seen は exit_capture だけ"
    cur.execute("select payload->>'option' as o from events where name='taste.flagged' and (payload->>'artifact_id')::bigint=%s", (aid,))
    flagged = sorted({x["o"] for x in cur.fetchall() if x["o"]})
    cur.execute("update artifacts set payload = payload || %s where id=%s",
                (Jsonb({"status": "seen", "chosen_by": by, "reason": _clip(note, 300)}), aid))
    cur.execute("insert into events(name, correlation_id, payload) values ('choice.seen',%s,%s)",
                (r["correlation_id"], Jsonb({"artifact_id": aid, "kind": p["kind"], "by": by, "flagged": flagged,
                                             "unflagged": [o["id"] for o in p["options"] if o["id"] not in flagged],
                                             "subject": p.get("subject"), "note": _clip(note, 300)})))
    return True, "seen"


def close(cur, aid: int, status: str, by: str, reason: str) -> bool:
    """人を待たずに工場が閉じる（新しい版で置き換わった・期限）。人の判断ではないので一致率には数えない"""
    r, err = _get_open(cur, aid)
    if err:
        return False
    cur.execute("update artifacts set payload = payload || %s where id=%s", (Jsonb({"status": status, "chosen_by": by, "reason": _clip(reason, 300)}), aid))
    cur.execute("insert into events(name, correlation_id, payload) values ('choice.closed',%s,%s)",
                (r["correlation_id"], Jsonb({"artifact_id": aid, "kind": r["payload"]["kind"], "status": status, "by": by, "reason": _clip(reason, 300)})))
    return True


def viewpoint_of(v) -> str:
    """観点の語彙に入っていれば その id、外か空なら unclassified（決定論。文面からは推し量らない）"""
    v = str(v or "").strip()
    return v if v in VIEWPOINTS else UNCLASSIFIED


def signature(kind: str | None, viewpoint: str | None = None) -> str:
    """違和感の印の束の鍵 taste:<業務>:<観点>（ADR 0040 の決定 9）"""
    return f"taste:{CHOICE_WORK.get(kind or '', 'dev.design.screen')}:{viewpoint_of(viewpoint)}"


def flag(cur, aid: int, note: str, by: str, option_id: str | None = None, viewpoint: str | None = None) -> tuple[bool, str]:
    """違和感の印。開いている依頼でも閉じた依頼でも付けられる（出口のキャプチャへの印もこの形で受ける）。
    viewpoint は人が言語化の候補から選んだ観点（design_guideline の節の id。なければ未分類で束ねる）"""
    if not note.strip():
        return False, "note required"
    cur.execute("select correlation_id, payload from artifacts where id=%s and type='HumanChoice'", (aid,))
    r = cur.fetchone()
    if not r:
        return False, "no such choice"
    opts = {o["id"]: o for o in r["payload"]["options"]}
    if option_id is not None and option_id not in opts:
        return False, "no such option"
    o = opts.get(option_id) or {}
    cur.execute("insert into events(name, correlation_id, payload) values ('taste.flagged',%s,%s)",
                (r["correlation_id"], Jsonb({"artifact_id": aid, "kind": r["payload"]["kind"], "option": option_id, "note": _clip(note, 300),
                                             "option_label": o.get("label"), "option_note": o.get("note"),
                                             "work": CHOICE_WORK.get(r["payload"]["kind"], "dev.design.screen"), "viewpoint": viewpoint_of(viewpoint),
                                             "signature": signature(r["payload"]["kind"], viewpoint), "subject": r["payload"].get("subject"), "by": by})))
    return True, "flagged"
