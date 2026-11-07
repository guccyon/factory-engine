"""事業部の窓口（ADR 0030）の制御プレーンの側。トークンと役割、会話の記録、明確な指示（優先度・保留・チケットの状態）。

窓口の API（desk_api.py）と factoryctl が同じ関数を通る。チケットの操作は ops.py を通るので、操業コンソールと振る舞いは変わらない。
会話の中身は events の desk.*（correlation_id は desk:<会話の id>）に残す。依頼者の言葉はデータとして持つだけで、
ここではシェルにもプロンプトにも渡さない（壁打ちの相手役（desk_line.py → core-gates/desk_consult.py）にはデータとして渡す）。

役割（ADR 0030 の 5）:
  operator   運用主。対象の製品（空ならすべて）の相談とチケットをすべて見て、優先度・保留を変えられる
  requester  依頼者。対象の製品について相談を開き、自分の相談と、自分の依頼から作ったチケットの優先度・保留だけを変えられる。
             チケットの状態は対象の製品のものを見られる（読むだけ）
"""
from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timezone

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
    if c["state"] == "confirmed":
        raise DeskError(409, "確認して投入した相談には書けない（足したいことは新しく開く。チケットの優先度・保留は tickets の操作で）")
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
    d = latest_draft(cur, conversation_id)
    return {"conversation": view_conversation(c), "messages": messages, "tickets": tickets,
            "draft": {"draft_id": d["id"], **d["payload"]} if d else None,
            "last_id": messages[-1]["id"] if messages else after}


def view_conversation(c: dict) -> dict:
    return {k: (v.isoformat() if hasattr(v, "isoformat") else float(v) if k == "cost_usd" else v) for k, v in c.items()}


# ------------------------------------------------------------------ 下書きの確認（ADR 0030 の 3。依頼者の確認 1 回で投入する）
DRAFT_TYPE = "TaskSetDraft"


def latest_draft(cur, conversation_id: int) -> dict | None:
    """会話の最新の下書き（artifacts の TaskSetDraft）。言い足しで出し直したら、古い下書きは確認できない"""
    cur.execute("select id, created_at, payload from artifacts where type=%s and correlation_id=%s order by id desc limit 1",
                (DRAFT_TYPE, corr(conversation_id)))
    return cur.fetchone()


def confirm_draft(cur, who: dict, conversation_id: int, draft_id: int, level: str | None = None) -> dict:
    """依頼者が下書きを確認する。チケットの起票は dispatcher が次の周回で行う（窓口のプロセスは GitHub に触らない）。
    level を渡すと、希望の優先度をここで決め直す（なければ相談を開いたときの希望、それもなければ p2）"""
    c = _conversation(cur, who, conversation_id, for_update=True)
    if c["kind"] != "request":
        raise DeskError(409, "確認できるのは依頼の下書きだけ")
    if c["state"] != "drafted":
        raise DeskError(409, f"いまは確認できない（状態 {c['state']}。下書きが出てから）")
    d = latest_draft(cur, conversation_id)
    if not d or d["id"] != draft_id:
        raise DeskError(409, f"最新の下書きではない（最新は {d['id'] if d else 'なし'}）")
    if level is not None and level not in ops.LEVELS:
        raise DeskError(400, f"優先度は {' / '.join(ops.LEVELS)}")
    level = level or (c["wanted"] or {}).get("level") or "p2"
    cur.execute("update desk_conversations set state='confirmed', updated_at=now() where id=%s returning *", (c["id"],))
    c = cur.fetchone()
    _event(cur, "desk.request_confirmed", c["id"], {"by": who["name"], "draft_id": draft_id, "level": level,
                                                     "tasks": len(d["payload"].get("tasks") or [])})
    return c


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


# ------------------------------------------------------------------ 受信トレイ（ADR 0034 の順番 2）
# 窓口のトークンの役割で、答えてよい件だけを見せる（ADR 0034 の決定 7）。運用主は受信トレイのどの役割（オーナー・PO・デザインのチーフ・
# 建設者の代理）の件も見て答えられる（Gen1 はオーナー 1 人が兼ねる。コンソールと同じ扱いで、件の allowed とは照らさない）。
# 依頼者は「起票者」の件だけ。どちらもトークンの対象の製品の件に限る（製品の分からない件は、製品を限らない運用主だけ）
INBOX_WHO = {"operator": None, "requester": ("requester",)}
INBOX_ANSWER_ROLE = {"operator": None, "requester": "requester"}   # inbox.answer に渡す役割（None は照らさない）
INBOX_STATUS = {"answered": 200, "noted": 200, "already": 409, "not_found": 404, "invalid": 400, "failed": 409}
_PRODUCT_IN_REF = re.compile(r"^(?:[a-z]+:)?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?:[#@].*)?$")
_PRODUCT_IN_URL = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/")


def inbox_product(item: dict) -> str | None:
    """件の製品（owner/repo）。ref（owner/repo#123・owner/repo）か GitHub の URL から。分からなければ None"""
    for rx, v in ((_PRODUCT_IN_REF, item.get("ref") or ""), (_PRODUCT_IN_URL, item.get("url") or "")):
        m = rx.match(v)
        if m:
            return m.group(1)
    return None


def _inbox_visible(who: dict, item: dict) -> bool:
    roles = INBOX_WHO.get(who["role"], ())
    if roles is not None:
        allowed = (item.get("answer") or {}).get("allowed") or [item.get("who")]
        if item.get("who") not in roles and not set(allowed) & set(roles):
            return False
    product = inbox_product(item)
    if product is None:
        return who["role"] == "operator" and not who["products"]
    return can_product(who, product)


def _inbox_summary(item: dict) -> dict:
    import inbox
    a = item.get("answer") or {}
    rec = a.get("recommended")
    return {"key": item["key"], "kind": item["kind"], "kind_label": inbox.KINDS.get(item["kind"], item["kind"]),
            "who": item["who"], "who_label": inbox.WHO.get(item["who"], item["who"]), "title": item["title"], "action": item.get("action") or "",
            "url": item.get("url") or "", "product": inbox_product(item), "since": inbox._iso(item.get("since")), "age_sec": item.get("age_sec"),
            "answerable": bool(a), "irreversible": bool(a.get("irreversible")),
            "recommended": next((o["label"] for o in a.get("options") or [] if o["id"] == rec), None) if rec else None,
            "answered": item.get("answered")}


def inbox_list(cur, who: dict, *, include_answered: bool = False) -> dict:
    """自分が答えてよい受信トレイの件。待ちの長い順。include_answered で、答え済みで反映を待っている件も入れる"""
    import inbox
    xs = [i for i in inbox.items(cur, include_answered=include_answered) if _inbox_visible(who, i)]
    return {"count": sum(1 for i in xs if not i.get("answered")), "items": [_inbox_summary(i) for i in xs]}


def _inbox_item(cur, who: dict, key) -> dict:
    import inbox
    if not isinstance(key, str) or not inbox.KEY.match(key):
        raise DeskError(400, "key は受信トレイの件の key（inbox_list の key）")
    it = inbox.find(cur, key)
    # 見えない件は「ない」と同じに返す（ほかの人の件があることも漏らさない）
    if it is None or not _inbox_visible(who, it):
        a = inbox.last_answer(cur, key) if it is None and who["role"] == "operator" else None
        if a:   # 答えて閉じた件（運用主にだけ、誰が何と答えたかを返す）
            raise DeskError(409, f"この件はすでに{inbox.answered_text(a)}")
        raise DeskError(404, "その件は受信トレイに開いていない")
    return it


def inbox_read(cur, who: dict, key: str) -> dict:
    """受信トレイの 1 件。detail（問い・今の案・他の案・judge の見立てなど）と答え方（選択肢・推奨・作用・取り返しがつくか）を付ける"""
    import inbox
    it = _inbox_item(cur, who, key)
    if it.get("since") is not None and it.get("age_sec") is None:
        it["age_sec"] = inbox._age(it["since"], datetime.now(timezone.utc))
    a = inbox._public(it.get("answer"))
    return {**_inbox_summary(it), "detail": it.get("detail") or [],
            "answer": {k: a[k] for k in ("type", "options", "extra", "recommended", "effect", "irreversible", "note_hint", "allowed") if k in a} if a else None,
            "how": None if a else (it.get("action") or "リンク先で答える")}


def inbox_answer(cur, who: dict, key: str, option, *, note: str = "", raw: str = "", on: str | None = None, confirm: bool = False) -> dict:
    """受信トレイの 1 件に答える。inbox.answer（唯一の口、先勝ち）を入口 mcp で通す。道具の呼び出しは選択肢を明示した答えとして扱う。
    取り返しのつかない作用の件（本番に出すなど）は、本人が明示して承認したしるしの confirm がないと受けない（ADR 0034 の決定 4）"""
    import inbox
    if not isinstance(option, str) or not option:
        raise DeskError(400, "option は inbox_read の answer.options か extra の id")
    it = _inbox_item(cur, who, key)
    if (it.get("answer") or {}).get("irreversible") and confirm is not True:
        raise DeskError(400, "取り返しのつかない作用の件。本人がこの答えをはっきり承認したときだけ confirm: true を付けて送る")
    res, msg = inbox.answer(cur, key, option, _by(who), "mcp", note=str(note or ""), raw=str(raw or "")[:1000], on=on or None,
                            explicit=True, role=INBOX_ANSWER_ROLE.get(who["role"], "requester"))
    if INBOX_STATUS.get(res, 400) != 200:
        raise DeskError(INBOX_STATUS.get(res, 400), msg)
    return {"result": res, "message": msg, "key": key}
