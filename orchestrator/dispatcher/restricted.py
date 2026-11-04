"""知見の制限付きの保管先（ADR 0024 の段取り 5、決定 8）。

利用者の情報をどうしても含む知見（障害対応など）の中身を、制御プレーンの DB に暗号にして置く。
git の知見からは evidence の `restricted: R-NNNN` で指すだけにする（core-gates/knowledge_check.py が id の形だけを許す）。

- 中身は管理者だけが持つ鍵（RESTRICTED_STORE_KEY）で pgcrypto の対称鍵暗号にする。鍵は .env にもサービスの環境にも置かず、
  使うときに `docker compose exec -e RESTRICTED_STORE_KEY dispatcher factoryctl restricted ...` で渡すか、聞かれたときに打つ。
  ワーカーのジョブの DB は同じ Postgres の上に作るので、DB の権限だけでは管理者に限れない。鍵がなければ読めない形で守る
- 読むたびに restricted_reads と events の restricted.read に、誰が・いつ・何のために読んだかを残す。events には中身を書かない
- 一覧に出すのは id・製品・指している知見・作った人と時刻だけ（中身の見出しも出さない）
- 消さずに retired にする（id は再利用しない）。retired にしたときに中身は消す

ここはエージェントの経路に繋がない。ワーカーにも、操業コンソールにも、窓口の API にも出さない（factoryctl だけ）。
"""
from __future__ import annotations

import re

import psycopg
from psycopg.types.json import Jsonb

ID_RE = re.compile(r"^R-(\d{4,})$")
PRODUCT_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
KNOWLEDGE_RE = re.compile(r"^K-\d{4,}$")
MIN_KEY_LEN = 16          # 短い鍵は受け付けない
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


def _check_key(key: str | None) -> str:
    if not key or len(key) < MIN_KEY_LEN:
        raise RestrictedError(f"鍵（RESTRICTED_STORE_KEY）がないか短い（{MIN_KEY_LEN} 文字以上）")
    return key


def add(cur, product: str, body: str, knowledge_ids: list[str], by: str, key: str) -> str:
    """中身を暗号にして置き、R-NNNN を返す。events には id と製品と指している知見だけを残す"""
    key = _check_key(key)
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
    cur.execute("insert into restricted_records(product, body, knowledge_ids, created_by) values (%s, pgp_sym_encrypt(%s, %s), %s, %s) returning id",
                (product, body, key, knowledge_ids, by))
    rid = format_id(cur.fetchone()["id"])
    cur.execute("insert into events(name, payload) values ('restricted.added', %s)",
                (Jsonb({"id": rid, "product": product, "knowledge_ids": knowledge_ids, "by": by}),))
    return rid


def list_records(cur, product: str | None = None) -> list[dict]:
    """中身を出さない一覧。読んだ回数と最後に読んだ時刻を添える"""
    cur.execute("""select r.id, r.product, r.knowledge_ids, r.created_by, r.created_at, r.retired_at,
                          count(x.id) as reads, max(x.read_at) as last_read_at
                     from restricted_records r left join restricted_reads x on x.record_id = r.id
                    where %(p)s::text is null or r.product = %(p)s
                    group by r.id order by r.id""", {"p": product})
    return [{**r, "rid": format_id(r["id"])} for r in cur.fetchall()]


def read(cur, rid: str, by: str, purpose: str, key: str) -> str:
    """中身を復号して返す。読んだ記録を先に残す（鍵が誤っていて読めなかった試みも残す）"""
    key = _check_key(key)
    n = parse_id(rid)
    purpose = (purpose or "").strip()
    if not purpose:
        raise RestrictedError("何のために読むか（--purpose）を渡す")
    cur.execute("select id, retired_at from restricted_records where id=%s", (n,))
    r = cur.fetchone()
    if not r:
        raise RestrictedError(f"{rid} はない")
    if r["retired_at"]:
        raise RestrictedError(f"{rid} は retired（中身は消してある）")
    cur.execute("insert into restricted_reads(record_id, read_by, purpose) values (%s,%s,%s)", (n, by, purpose))
    cur.execute("insert into events(name, payload) values ('restricted.read', %s)", (Jsonb({"id": rid, "by": by, "purpose": purpose}),))
    try:   # factoryctl は autocommit。読んだ記録は復号の失敗で巻き戻らない
        cur.execute("select pgp_sym_decrypt(body, %s) as body from restricted_records where id=%s", (key, n))
        body = cur.fetchone()["body"]
    except psycopg.Error as e:   # pgcrypto は鍵の誤りを 1 つの誤りで返す
        cur.execute("insert into events(name, payload) values ('restricted.read_failed', %s)", (Jsonb({"id": rid, "by": by}),))
        raise RestrictedError(f"{rid} を復号できない（鍵が違う）") from e
    return body


def retire(cur, rid: str, by: str, reason: str) -> bool:
    """retired にして中身を消す。id と、作った・読んだ記録は残す"""
    n = parse_id(rid)
    cur.execute("update restricted_records set retired_at=now(), retired_by=%s, body=''::bytea where id=%s and retired_at is null returning id", (by, n))
    if not cur.fetchone():
        return False
    cur.execute("insert into events(name, payload) values ('restricted.retired', %s)", (Jsonb({"id": rid, "by": by, "reason": reason}),))
    return True
