"""工場の中での一次調査の、調査役の SQL を流す口（ADR 0048 の決定 3、順番 2。definitions/investigate.yaml の sources.db）。

調査役（ワーカー）には DB の接続情報を渡さない。調査役が出した SELECT をここで受け取り、
調査用の利用者 factory_investigate（postgres/init/008_investigate.sql）で別に繋いだ接続の、読み取り専用のトランザクションで流す。

  - 繋ぎ先は INVESTIGATE_DATABASE_URL だけ。管理者の DATABASE_URL には決して落とさない（無ければ使えないと返す）
  - 1 文だけ・SELECT か WITH で始まるものだけを受ける。本当の守りは利用者の権限（investigate のビューしか読めない）で、
    ここは手前の目安（拒んだ理由を調査役に返して書き直させる）
  - 1 文 10 秒・500 行まで（値は definitions/investigate.yaml の sources.db。食い違いはテストで見る）
"""
from __future__ import annotations

import os
import re

TIMEOUT_S = 10
MAX_ROWS = 500
ENV = "INVESTIGATE_DATABASE_URL"
USER = "factory_investigate"
STARTS = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
STRING = re.compile(r"'(?:[^']|'')*'")


class Unavailable(RuntimeError):
    """調査用の利用者がまだ有効になっていない（INVESTIGATE_DATABASE_URL がない、または繋げない）"""


class Rejected(ValueError):
    """流す前に断った（1 文でない、SELECT でない）。理由は調査役に返す"""


def check_sql(sql: str) -> str:
    """流してよい形なら末尾の ; を落とした文を返す。だめなら Rejected"""
    body = COMMENT.sub(" ", sql or "").strip()
    bare = STRING.sub("''", body)
    if bare.endswith(";"):
        bare = bare[:-1].rstrip()
        body = body[:-1].rstrip() if body.endswith(";") else body
    if not bare:
        raise Rejected("SQL が空")
    if ";" in bare:
        raise Rejected("1 回に流せるのは 1 文だけ")
    if not STARTS.match(bare):
        raise Rejected("SELECT か WITH で始まる文だけ流せる")
    return body


def url() -> str:
    u = os.environ.get(ENV, "").strip()
    if not u:
        raise Unavailable(f"{ENV} がない（調査用の利用者がまだ有効になっていない）")
    if f"//{USER}" not in u and f"user={USER}" not in u:
        raise Unavailable(f"{ENV} は {USER} の接続にする（管理者の接続では流さない）")
    return u


def query(sql: str, max_rows: int = MAX_ROWS, timeout_s: int = TIMEOUT_S, connect=None) -> dict:
    """{columns, rows, truncated} を返す。rows は値の list（JSON にできる形）"""
    stmt = check_sql(sql)
    if connect is None:
        import psycopg
        connect = psycopg.connect
    try:
        conn = connect(url(), autocommit=False)
    except Unavailable:
        raise
    except Exception as e:  # noqa: BLE001  繋げないことは調査役に返す（工場は止めない）
        raise Unavailable(f"繋げない: {type(e).__name__}") from e
    try:
        with conn.cursor() as cur:
            cur.execute("set transaction read only")
            cur.execute(f"set local statement_timeout = '{int(timeout_s)}s'")
            cur.execute(stmt, prepare=True)          # 拡張問い合わせで流す（複数の文は送れない）
            cols = [d.name for d in (cur.description or [])]
            rows = cur.fetchmany(max_rows + 1)
        conn.rollback()
    finally:
        conn.close()
    truncated = len(rows) > max_rows
    return {"columns": cols, "rows": [list(_jsonable(v) for v in r) for r in rows[:max_rows]], "truncated": truncated}


def _jsonable(v):
    if v is None or isinstance(v, (str, int, float, bool, list, dict)):
        return v
    return str(v)
