"""知見の制限付きの保管先（ADR 0024 の段取り 5、決定 8）。

利用者の情報をどうしても含む知見（障害対応など）の根拠の中身を、制御プレーンの Postgres の専用スキーマ restricted に置く。
git の知見からは evidence の `restricted: R-NNNN` で指すだけにする（core-gates/knowledge_check.py が id の形だけを許す）。

- 守り方は利用者を分けること（2026-11-05 オーナー）。restricted スキーマは保管先の専用の利用者 factory_restricted だけが読み書きでき、
  その合言葉は管理者だけが持つ。factoryctl は RESTRICTED_DATABASE_URL（管理者が使うときに渡す）でこの利用者として繋ぐ
- ワーカーのジョブの DB は別の利用者（factory_job）で、制御プレーンの DB に繋げない（jobdb.py）
- 読むたびに restricted.reads と events の restricted.read に、誰が・いつ・何のために読んだかを残す。events には中身を書かない
- 一覧に出すのは id・製品・指している知見・作った人と時刻・読んだ回数だけ
- 消さずに retired にする（id は再利用しない）。retired にしたときに中身は消す

関数は 2 つのカーソルを取る。rcur は保管先の利用者の接続（restricted.*）、cur は制御プレーンの接続（events）。
ここはエージェントの経路に繋がない。ワーカーにも、操業コンソールにも、窓口の API にも出さない（factoryctl だけ）。
"""
from __future__ import annotations

import re

from psycopg.types.json import Jsonb

ID_RE = re.compile(r"^R-(\d{4,})$")
PRODUCT_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
KNOWLEDGE_RE = re.compile(r"^K-\d{4,}$")
MAX_BODY = 20000          # 1 件の上限（文字数）。障害の記録の要点だけを置く


class RestrictedError(Exception):
    pass


def format_id(n: int) -> str:
    return f"R-{n:04d}"


def parse_id(rid: str) -> int:
    m = ID_RE.match(rid or "")
    if not m:
        raise RestrictedError("id は R-NNNN の形で渡す")
    return int(m.group(1))


def _event(cur, name: str, payload: dict) -> None:
    cur.execute("insert into events(name, payload) values (%s, %s)", (name, Jsonb(payload)))


def add(rcur, cur, product: str, body: str, knowledge_ids: list[str], by: str) -> str:
    """中身を置き、R-NNNN を返す。events には id と製品と指している知見だけを残す"""
    if not PRODUCT_RE.match(product or ""):
        raise RestrictedError("製品は owner/repo の形で渡す")
    body = (body or "").strip()
    if not body:
        raise RestrictedError("中身が空")
    if len(body) > MAX_BODY:
        raise RestrictedError(f"中身が長すぎる（{MAX_BODY} 文字まで）。要点だけを置く")
    bad = [k for k in knowledge_ids if not KNOWLEDGE_RE.match(k)]
    if bad:
        raise RestrictedError(f"知見の id は K-NNNN: {', '.join(bad)}")
    rcur.execute("insert into restricted.records(product, body, knowledge_ids, created_by) values (%s,%s,%s,%s) returning id",
                 (product, body, knowledge_ids, by))
    rid = format_id(rcur.fetchone()["id"])
    _event(cur, "restricted.added", {"id": rid, "product": product, "knowledge_ids": knowledge_ids, "by": by})
    return rid


def list_records(rcur, product: str | None = None) -> list[dict]:
    """中身を出さない一覧。読んだ回数と最後に読んだ時刻を添える"""
    rcur.execute("""select r.id, r.product, r.knowledge_ids, r.created_by, r.created_at, r.retired_at,
                           count(x.id) as reads, max(x.read_at) as last_read_at
                      from restricted.records r left join restricted.reads x on x.record_id = r.id
                     where %(p)s::text is null or r.product = %(p)s
                     group by r.id order by r.id""", {"p": product})
    return [{**r, "rid": format_id(r["id"])} for r in rcur.fetchall()]


def read(rcur, cur, rid: str, by: str, purpose: str) -> str:
    """中身を返す。読んだ記録を先に残す"""
    n = parse_id(rid)
    purpose = (purpose or "").strip()
    if not purpose:
        raise RestrictedError("何のために読むか（--purpose）を渡す")
    rcur.execute("select body, retired_at from restricted.records where id=%s", (n,))
    r = rcur.fetchone()
    if not r:
        raise RestrictedError(f"{rid} はない")
    if r["retired_at"]:
        raise RestrictedError(f"{rid} は retired（中身は消してある）")
    rcur.execute("insert into restricted.reads(record_id, read_by, purpose) values (%s,%s,%s)", (n, by, purpose))
    _event(cur, "restricted.read", {"id": rid, "by": by, "purpose": purpose})
    return r["body"]


def retire(rcur, cur, rid: str, by: str, reason: str) -> bool:
    """retired にして中身を消す。id と、作った・読んだ記録は残す"""
    n = parse_id(rid)
    rcur.execute("update restricted.records set retired_at=now(), retired_by=%s, body='' where id=%s and retired_at is null returning id", (by, n))
    if not rcur.fetchone():
        return False
    _event(cur, "restricted.retired", {"id": rid, "by": by, "reason": reason})
    return True
