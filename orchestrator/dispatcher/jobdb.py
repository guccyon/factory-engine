"""ジョブ用の DB（job_<id>）と、その利用者。

ジョブの DB は制御プレーンと同じ Postgres の上に作る。前は制御プレーンと同じ管理者の利用者（factory）で渡していたので、
implement のワーカー（とその中のエージェント）が制御プレーンの DB（events・窓口の会話など）に繋いで読めた。
今は、管理者でない専用の利用者（JOB_DB_USER、既定 factory_job）の持ち物にして渡し、制御プレーンの DB には繋がせない。

- 専用の利用者は LOGIN と CREATEDB だけ（rally の sqlx::test がテストごとに DB を作るため）。SUPERUSER・CREATEROLE は持たない
- 制御プレーンの DB（factory）の CONNECT を PUBLIC から外し、専用の利用者からは繋げないようにする
- 知見の制限付きの保管先（restricted スキーマ、005_restricted.sql）は保管先の専用の利用者だけが読み書きできる
"""
from __future__ import annotations

import re

from psycopg import sql

NAME_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_READY: set[tuple[str, str]] = set()


def ensure_role(cur, user: str, password: str, control_db: str) -> None:
    """専用の利用者を作り（あれば合言葉と権限をそろえ）、制御プレーンの DB に繋がせない。周回ごとに 1 度だけ"""
    if (user, control_db) in _READY:
        return
    if not NAME_RE.match(user) or not NAME_RE.match(control_db):
        raise ValueError(f"利用者と DB の名前は英小文字・数字・_（{user}, {control_db}）")
    cur.execute("select 1 from pg_roles where rolname=%s", (user,))
    verb = "alter" if cur.fetchone() else "create"
    cur.execute(sql.SQL(verb + " role {} login createdb nosuperuser nocreaterole password {}").format(sql.Identifier(user), sql.Literal(password)))
    cur.execute(sql.SQL("revoke connect on database {} from public").format(sql.Identifier(control_db)))
    cur.execute(sql.SQL("revoke all on database {} from {}").format(sql.Identifier(control_db), sql.Identifier(user)))
    _READY.add((user, control_db))


def create(cur, dbname: str, owner: str) -> None:
    """job_<id> を作り直し、専用の利用者の持ち物にする"""
    if not NAME_RE.match(dbname):
        raise ValueError(f"DB の名前: {dbname}")
    cur.execute(sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(dbname)))
    cur.execute(sql.SQL("create database {} owner {}").format(sql.Identifier(dbname), sql.Identifier(owner)))
