"""事業部の窓口（ADR 0030）の制御プレーンの側。トークンと役割、会話の記録、明確な指示（優先度・保留・チケットの状態）。

窓口の API（desk_api.py）と factoryctl が同じ関数を通る。チケットの操作は ops.py を通るので、操業コンソールと振る舞いは変わらない。
会話の中身は events の desk.*（correlation_id は desk:<会話の id>）に残す。依頼者の言葉はデータとして持つだけで、
ここではシェルにもプロンプトにも渡さない（壁打ちの相手役に渡すのは順番 2 で、そのときもデータとして渡す）。

役割（ADR 0030 の 5）:
  operator   運用主。対象の製品（空ならすべて）の相談とチケットをすべて見て、優先度・保留を変えられる
  requester  依頼者。対象の製品について相談を開き、自分の相談と、自分の依頼から作ったチケットの優先度・保留だけを変えられる。
             チケットの状態は対象の製品のものを見られる（読むだけ）
"""
from __future__ import annotations

import hashlib
import re
import secrets

from psycopg.types.json import Jsonb

import ops

ROLES = ("operator", "requester")
KINDS = ("request", "inquiry")
TOKEN_PREFIX = "fdk_"
MAX_BODY = 8000          # 1 通の上限（文字数）。長い資料は製品のリポジトリに置いて参照を渡してもらう
MAX_TITLE = 80
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
PRODUCT_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
REF_RE = re.compile(r"^(?:(?P<source>[a-z]+):)?(?P<ref>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#\d+)$")


class DeskError(Exception):
    """依頼者に返す誤り。status は HTTP の状態コード"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def corr(conversation_id: int) -> str:
    return f"desk:{conversation_id}"


# ------------------------------------------------------------------ トークン
def create_token(cur, name: str, role: str, products: list[str], by: str) -> str:
    """依頼者のトークンを発行する。返り値のトークンはここでしか見えない（保存するのはハッシュだけ）"""
    if not NAME_RE.match(name or ""):
        raise DeskError(400, "名前は英数字と _ . - の 64 文字まで")
    if role not in ROLES:
        raise DeskError(400, f"役割は {' / '.join(ROLES)}")
    bad = [p for p in products if not PRODUCT_RE.match(p)]
    if bad:
        raise DeskError(400, f"製品は owner/repo: {', '.join(bad)}")
    if role == "requester" and not products:
        raise DeskError(400, "依頼者には対象の製品を 1 つ以上渡す")
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    cur.execute("select 1 from desk_tokens where name=%s", (name,))
    if cur.fetchone():
        raise DeskError(409, f"{name} はもうある（作り直すなら先に revoke）")
    cur.execute("insert into desk_tokens(name, role, products, token_hash) values (%s,%s,%s,%s)", (name, role, products, _hash(token)))
    cur.execute("insert into events(name, payload) values ('desk.token_issued', %s)",
                (Jsonb({"name": name, "role": role, "products": products, "by": by}),))
    return token


def revoke_token(cur, name: str, by: str) -> bool:
    cur.execute("update desk_tokens set revoked_at=now() where name=%s and revoked_at is null returning id", (name,))
    if not cur.fetchone():
        return False
    cur.execute("insert into events(name, payload) values ('desk.token_revoked', %s)", (Jsonb({"name": name, "by": by}),))
    return True


def list_tokens(cur) -> list[dict]:
    cur.execute("select name, role, products, created_at, revoked_at from desk_tokens order by created_at")
    return cur.fetchall()


def authenticate(cur, token: str | None) -> dict | None:
    """トークンから依頼者。{name, role, products}。知らない・取り消したトークンは None"""
    if not token or not token.startswith(TOKEN_PREFIX):
        return None
    cur.execute("select name, role, products from desk_tokens where token_hash=%s and revoked_at is null", (_hash(token),))
    return cur.fetchone()


# ------------------------------------------------------------------ 範囲
def can_product(who: dict, product: str) -> bool:
    if who["role"] == "operator" and not who["products"]:
        return True
    return product in who["products"]


def _conversation(cur, who: dict, conversation_id: int, for_update: bool = False) -> dict:
    cur.execute("select * from desk_conversations where id=%s" + (" for update" if for_update else ""), (conversation_id,))
    c = cur.fetchone()
    # 見えない会話は「ない」と同じに返す（ほかの依頼者の相談があることも漏らさない）
    if not c or not can_product(who, c["product"]) or (who["role"] != "operator" and c["opened_by"] != who["name"]):
        raise DeskError(404, "そのような相談はない")
    return c


def _ticket_for_change(cur, who: dict, ticket: dict | None) -> dict:
    if not ticket or not can_product(who, ticket["repo"]):
        raise DeskError(404, "そのようなチケットはない")
    if who["role"] == "operator":
        return ticket
    cur.execute("select opened_by from desk_conversations where id=%s", (ticket.get("desk_conversation_id"),))
    c = cur.fetchone()
    if not c or c["opened_by"] != who["name"]:
        raise DeskError(403, "依頼者が変えられるのは、自分の依頼から作ったチケットだけ（ほかは運用主に）")
    return ticket


# ------------------------------------------------------------------ 会話
def _text(body, limit: int = MAX_BODY) -> str:
    if not isinstance(body, str) or not body.strip():
        raise DeskError(400, "本文が空")
    if len(body) > limit:
        raise DeskError(413, f"本文は {limit} 文字まで")
    return body


def _event(cur, name: str, conversation_id: int, payload: dict) -> int:
    cur.execute("insert into events(name, correlation_id, payload) values (%s,%s,%s) returning id",
                (name, corr(conversation_id), Jsonb({"conversation_id": conversation_id, **payload})))
    return cur.fetchone()["id"]


def open_conversation(cur, who: dict, kind: str, product: str, body: str, *, level: str | None = None, due: str | None = None) -> dict:
    """相談を開く。依頼（request）と問い合わせ（inquiry）の 2 種類だけ。返り値は会話（相手の返事は read_conversation で取る）"""
    if kind not in KINDS:
        raise DeskError(400, "種類は request（依頼）か inquiry（問い合わせ）")
    if not isinstance(product, str) or not PRODUCT_RE.match(product):
        raise DeskError(400, "製品は owner/repo")
    if not can_product(who, product):
        raise DeskError(403, f"{product} はこのトークンの対象ではない")
    body = _text(body)
    if level is not None and level not in ops.LEVELS:
        raise DeskError(400, f"希望の優先度は {' / '.join(ops.LEVELS)}")
    if due is not None and (not isinstance(due, str) or len(due) > 40):
        raise DeskError(400, "期限は 40 文字までの文字列（例 2026-11-30）")
    wanted = {k: v for k, v in (("level", level), ("due", due)) if v is not None}
    title = body.strip().splitlines()[0][:MAX_TITLE]
    cur.execute("insert into desk_conversations(kind, product, opened_by, title, wanted) values (%s,%s,%s,%s,%s) returning *",
                (kind, product, who["name"], title, Jsonb(wanted)))
    c = cur.fetchone()
    _event(cur, "desk.opened", c["id"], {"kind": kind, "product": product, "by": who["name"], "title": title, "wanted": wanted})
    _event(cur, "desk.message", c["id"], {"author": who["name"], "role": "requester", "body": body})
    return c


def post_message(cur, who: dict, conversation_id: int, body: str) -> dict:
    """依頼者の返事（問いへの答え・言い足し）。閉じた相談には書けない"""
    c = _conversation(cur, who, conversation_id, for_update=True)
    if c["state"] == "closed":
        raise DeskError(409, "閉じた相談には書けない（新しく開く）")
    body = _text(body)
    answered_question = c["state"] == "waiting_requester"
    cur.execute("update desk_conversations set state='open', rounds=rounds + %s, updated_at=now() where id=%s returning *",
                (1 if answered_question else 0, c["id"]))
    c = cur.fetchone()
    _event(cur, "desk.message", c["id"], {"author": who["name"], "role": "requester", "body": body})
    return c


def factory_message(cur, conversation_id: int, role: str, body: str, *, kind: str = "text", state: str | None = None,
                    payload: dict | None = None) -> dict:
    """工場の側（プランナーの相談役・回答役・工場）の返事を会話に書く。state を渡すと会話の状態も変える
    （問いなら waiting_requester、下書きなら drafted、回答なら answered）"""
    cur.execute("select * from desk_conversations where id=%s for update", (conversation_id,))
    c = cur.fetchone()
    if not c:
        raise DeskError(404, "そのような相談はない")
    if state:
        cur.execute("update desk_conversations set state=%s, updated_at=now() where id=%s returning *", (state, conversation_id))
        c = cur.fetchone()
    _event(cur, "desk.answered" if kind == "answer" else "desk.message", conversation_id,
           {"author": role, "role": role, "kind": kind, "body": body, **(payload or {})})
    return c


def close_conversation(cur, who: dict, conversation_id: int, reason: str = "") -> dict:
    c = _conversation(cur, who, conversation_id, for_update=True)
    if c["state"] == "closed":
        return c
    cur.execute("update desk_conversations set state='closed', updated_at=now() where id=%s returning *", (c["id"],))
    c = cur.fetchone()
    _event(cur, "desk.closed", c["id"], {"by": who["name"], "reason": reason})
    return c


def list_conversations(cur, who: dict, limit: int = 50) -> list[dict]:
    if who["role"] == "operator":
        cur.execute("select * from desk_conversations where (%s or product = any(%s)) order by updated_at desc limit %s",
                    (not who["products"], who["products"], limit))
    else:
        cur.execute("select * from desk_conversations where opened_by=%s and product = any(%s) order by updated_at desc limit %s",
                    (who["name"], who["products"], limit))
    return [view_conversation(c) for c in cur.fetchall()]


def read_conversation(cur, who: dict, conversation_id: int, after: int = 0) -> dict:
    """会話と、after（events の id）より後の発言。待つ側は最後に見た発言の id を after に渡して読み直す"""
    c = _conversation(cur, who, conversation_id)
    cur.execute("select id, name, occurred_at, payload from events where correlation_id=%s and name like 'desk.%%' and id > %s order by id",
                (corr(conversation_id), after))
    messages = [{"id": e["id"], "event": e["name"], "at": e["occurred_at"].isoformat(),
                 **{k: v for k, v in e["payload"].items() if k != "conversation_id"}} for e in cur.fetchall()]
    cur.execute("select id, source_ref, title, state, priority from tickets where desk_conversation_id=%s order by id", (conversation_id,))
    tickets = [{**t, "level": ops.level_of(t["priority"])} for t in cur.fetchall()]
    return {"conversation": view_conversation(c), "messages": messages, "tickets": tickets,
            "last_id": messages[-1]["id"] if messages else after}


def view_conversation(c: dict) -> dict:
    return {k: (v.isoformat() if hasattr(v, "isoformat") else float(v) if k == "cost_usd" else v) for k, v in c.items()}


# ------------------------------------------------------------------ チケット（明確な指示は壁打ちを通さず即時に通す）
def find_ticket(cur, key: str | int) -> dict | None:
    """チケットを id（数）か参照（owner/repo#123、github: などの入口つきも可）で引く"""
    if isinstance(key, int) or (isinstance(key, str) and key.isdigit()):
        return ops.get_ticket(cur, int(key))
    m = REF_RE.match(key or "")
    if not m:
        return None
    cur.execute("select * from tickets where source=%s and source_ref=%s", (m["source"] or "github", m["ref"]))
    return cur.fetchone()


def ticket_status(cur, who: dict, key: str | int) -> dict:
    """チケットの状態（読むだけ）。段・優先度・保留・依存待ち・順番待ちの何番目か・作業中のジョブ・PR"""
    t = find_ticket(cur, key)
    if not t or not can_product(who, t["repo"]):
        raise DeskError(404, "そのようなチケットはない")
    cur.execute("select id, kind, state, attempt, priority, enqueued_at, started_at from jobs where ticket_id=%s and state in ('queued','running') order by id",
                (t["id"],))
    jobs = cur.fetchall()
    rank = None
    queued = [j for j in jobs if j["state"] == "queued"]
    if queued and not t["held"]:
        j = queued[0]
        cur.execute("select count(*) as n from jobs q where q.state='queued' and not q.cancel_requested "
                    "and not exists (select 1 from tickets x where x.id=q.ticket_id and x.held) "
                    "and (q.priority > %s or (q.priority = %s and q.enqueued_at < %s))", (j["priority"], j["priority"], j["enqueued_at"]))
        rank = cur.fetchone()["n"] + 1
    cur.execute("select payload->>'pr_url' as pr_url, created_at from artifacts where type='PR' and correlation_id=%s order by id desc limit 1",
                (f"{t['source']}:{t['source_ref']}",))
    pr = cur.fetchone()
    stage = ("closed" if t["state"] == "closed" else "running" if any(j["state"] == "running" for j in jobs)
             else "queued" if queued else "waiting" if t["waiting_on"] else "pr" if pr and t["state"] == "active" else t["state"])
    return {"ticket_id": t["id"], "ref": t["source_ref"], "source": t["source"], "title": t["title"], "url": t["url"], "stage": stage,
            "priority": t["priority"], "level": ops.level_of(t["priority"]), "priority_set_by": t["priority_set_by"], "held": t["held"],
            "waiting_on": t["waiting_on"], "rank": rank, "conversation_id": t.get("desk_conversation_id"),
            "jobs": [{**j, "enqueued_at": j["enqueued_at"].isoformat(), "started_at": j["started_at"].isoformat() if j["started_at"] else None}
                     for j in jobs],
            "pr_url": pr["pr_url"] if pr else None}


def _by(who: dict) -> str:
    return f"desk:{who['name']}"


def set_priority(cur, who: dict, key: str | int, *, level: str | None = None, priority: int | None = None, reason: str = "",
                 conversation_id: int | None = None) -> dict:
    """依頼者の指定として優先度を変える（ADR 0030 の 2。tickets.priority_set_by に desk:<名前>）。p0 は作業中のものを止めてでも先に流す"""
    if level is not None:
        if level not in ops.LEVELS:
            raise DeskError(400, f"優先度は {' / '.join(ops.LEVELS)}")
        priority = ops.LEVELS[level][0]
    if not isinstance(priority, int) or isinstance(priority, bool) or not 0 <= priority <= 100:
        raise DeskError(400, "level（p0〜p3）か priority（0〜100）を渡す")
    if conversation_id is not None:
        _conversation(cur, who, conversation_id)
    t = _ticket_for_change(cur, who, find_ticket(cur, key))
    nt = ops.set_priority(cur, t["id"], priority, _by(who), reason)
    payload = {"ticket_id": t["id"], "ref": t["source_ref"], "from": t["priority"], "to": priority, "by": who["name"], "reason": reason}
    if conversation_id is not None:
        _event(cur, "desk.priority_changed", conversation_id, payload)
    else:
        cur.execute("insert into events(name, correlation_id, payload) values ('desk.priority_changed', %s, %s)",
                    (f"{t['source']}:{t['source_ref']}", Jsonb(payload)))
    return ticket_status(cur, who, nt["id"])


def set_held(cur, who: dict, key: str | int, held: bool, reason: str = "") -> dict:
    t = _ticket_for_change(cur, who, find_ticket(cur, key))
    ops.set_held(cur, t["id"], held, _by(who), reason)
    return ticket_status(cur, who, t["id"])
