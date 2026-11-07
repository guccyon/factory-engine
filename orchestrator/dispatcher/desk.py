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

from psycopg.types.json import Jsonb

import ops

ROLES = ("operator", "requester")
KINDS = ("request", "inquiry")
# 会話の状態（ADR 0039 の決定 2）。上の 6 つが今の窓口の状態、下の 6 つが単発の依頼のループで足した状態
STATES = ("open", "waiting_requester", "drafted", "confirmed", "answered", "closed",
          "routed", "awaiting_confirm", "running", "delivered", "accepted", "handed_off")
ENDED = ("closed", "accepted", "handed_off")   # 終わった会話。書き込めない
LOOP_BUSY = ("routed", "awaiting_confirm", "running", "delivered")   # 振り分けた後。依頼者の言い足しは記録だけ
# 閉じる理由（決定 2・4）。完了にできるのは依頼者だけ（accepted・declined・superseded・withdrawn）。handed_off はチケット化で渡したとき
CLOSE_REASONS = ("accepted", "declined", "superseded", "withdrawn", "handed_off")
PATHS = ("ticket", "direct_line", "operate", "quick", "analysis")   # 対応の種類（ADR 0037 の道。definitions/workflow.yaml の paths と同じ）
# 状態を進めてよい向き（決定 2 の図）。進めるのは dispatcher だけ（advance）。依頼者の取り下げ（close_conversation）はどこからでも
TRANSITIONS = {
    "open": ("waiting_requester", "drafted", "answered", "routed"),
    "waiting_requester": ("open", "drafted", "routed"),
    "drafted": ("open", "confirmed", "waiting_requester"),
    "confirmed": ("handed_off",),
    "answered": ("open",),
    "routed": ("awaiting_confirm", "running", "delivered", "waiting_requester"),   # delivered は断りの提出（どの対応の種類にも当てはまらない）
    "awaiting_confirm": ("running", "closed", "routed"),                           # closed は確認で断られた（依頼者の答え）
    "running": ("delivered",),
    "delivered": ("accepted", "closed", "routed", "open"),                         # open は続き・差し戻し（相談役の振り分けから）
}
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


def open_conversation(cur, who: dict, kind: str, product: str, body: str, *, level: str | None = None, due: str | None = None,
                      source_ref: str | None = None, derived_from: int | None = None) -> dict:
    """相談を開く。依頼（request）と問い合わせ（inquiry）の 2 種類だけ。返り値は会話（相手の返事は read_conversation で取る）。
    source_ref は依頼の来た口（GitHub の Issue の URL。ADR 0039 の決定 1）、derived_from は断りから別案として始めた元の会話（決定 4）"""
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
    if source_ref is not None and not (isinstance(source_ref, str) and source_ref.startswith("https://") and len(source_ref) <= 300):
        raise DeskError(400, "依頼の来た口は https:// の URL")
    if derived_from is not None:
        if _conversation(cur, who, derived_from)["product"] != product:
            raise DeskError(400, "別案は元の依頼と同じ製品で開く")
    wanted = {k: v for k, v in (("level", level), ("due", due)) if v is not None}
    title = body.strip().splitlines()[0][:MAX_TITLE]
    cur.execute("insert into desk_conversations(kind, product, opened_by, title, wanted, source_ref, derived_from) "
                "values (%s,%s,%s,%s,%s,%s,%s) returning *", (kind, product, who["name"], title, Jsonb(wanted), source_ref, derived_from))
    c = cur.fetchone()
    _event(cur, "desk.opened", c["id"], {"kind": kind, "product": product, "by": who["name"], "title": title, "wanted": wanted,
                                         **({"source_ref": source_ref} if source_ref else {}),
                                         **({"derived_from": derived_from} if derived_from else {})})
    _event(cur, "desk.message", c["id"], {"author": who["name"], "role": "requester", "body": body})
    if kind == "request":
        # 受付（ADR 0039 の決定 2・3）: 中身は読まずに、受け付けたことと次に起きることをすぐ返す
        _event(cur, "desk.message", c["id"], {"author": "factory", "role": "factory", "kind": "ack", "body": ACK_TEXT.format(id=c["id"])})
    return c


ACK_TEXT = ("依頼 {id} を受け付けました。次は振り分けです。相談役が、決まった操作で済むか、チケットに切るかを決めて、5 分ほどで返します"
            "（問いがあればそのときに聞きます）。")


def post_message(cur, who: dict, conversation_id: int, body: str) -> dict:
    """依頼者の返事（問いへの答え・言い足し）。閉じた相談には書けない"""
    c = _conversation(cur, who, conversation_id, for_update=True)
    if c["state"] in ENDED:
        raise DeskError(409, "終わった相談には書けない（新しく開く）")
    if c["state"] == "confirmed":
        raise DeskError(409, "確認して投入した相談には書けない（足したいことは新しく開く。チケットの優先度・保留は tickets の操作で）")
    body = _text(body)
    if c["state"] in LOOP_BUSY:
        # 振り分けた後の言い足しは記録だけ（状態は dispatcher が進める。ADR 0039 の決定 2）
        cur.execute("update desk_conversations set updated_at=now() where id=%s returning *", (c["id"],))
    else:
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
    """依頼者が会話を閉じる（取り下げ。ADR 0039 の決定 4）。動いているジョブは止め（cancel_requested）、止める前に出ていた結果は残す。
    取り下げは依頼者が完了にしたものとして扱う（閉じる理由は withdrawn）。reason は依頼者の一言"""
    c = _conversation(cur, who, conversation_id, for_update=True)
    if c["state"] in ENDED:
        return c
    cur.execute("update jobs set cancel_requested=true where correlation_id=%s and state in ('queued','running') and not cancel_requested "
                "returning id", (corr(c["id"]),))
    cancelled = [r["id"] for r in cur.fetchall()]
    return finish(cur, c["id"], "withdrawn", who["name"], note=reason, cancelled_jobs=cancelled)


def advance(cur, conversation_id: int, to: str, event: str, payload: dict | None = None, **cols) -> dict:
    """会話の状態を進める（dispatcher だけが呼ぶ。ADR 0039 の決定 2）。向きは TRANSITIONS に限り、状態と一緒に event を残す。
    cols は一緒に書く列（path・source_ref など）。終わり方（閉じる・受け取った・渡した）は finish を通す"""
    if to not in STATES:
        raise ValueError(f"知らない状態: {to}")
    cur.execute("select * from desk_conversations where id=%s for update", (conversation_id,))
    c = cur.fetchone()
    if not c:
        raise DeskError(404, "そのような相談はない")
    if to not in TRANSITIONS.get(c["state"], ()):
        raise DeskError(409, f"{c['state']} から {to} には進めない")
    bad = set(cols) - {"path", "source_ref"}
    if bad:
        raise ValueError(f"advance で書けない列: {sorted(bad)}")
    if cols.get("path") is not None and cols["path"] not in PATHS:
        raise ValueError(f"対応の種類は {' / '.join(PATHS)}")
    prev = c["state"]
    sets = "".join(f", {k}=%({k})s" for k in cols)
    cur.execute(f"update desk_conversations set state=%(to)s, updated_at=now(){sets} where id=%(id)s returning *",
                {"to": to, "id": conversation_id, **cols})
    c = cur.fetchone()
    _event(cur, event, conversation_id, {**(payload or {}), "from": prev, "state": to})
    return c


def finish(cur, conversation_id: int, reason: str, by: str, *, note: str = "", path: str | None = None, **payload) -> dict:
    """会話を終える（ADR 0039 の決定 4）。reason は CLOSE_REASONS。accepted は状態 accepted、handed_off は状態 handed_off、
    ほかは closed。desk.closed に理由と、決定 7 の記録の材料（record）を残す。もう終わっていれば何もしない"""
    if reason not in CLOSE_REASONS:
        raise ValueError(f"閉じる理由は {' / '.join(CLOSE_REASONS)}")
    cur.execute("select * from desk_conversations where id=%s for update", (conversation_id,))
    c = cur.fetchone()
    if not c:
        raise DeskError(404, "そのような相談はない")
    if c["state"] in ENDED:
        return c
    if path is not None and path not in PATHS:
        raise ValueError(f"対応の種類は {' / '.join(PATHS)}")
    state = reason if reason in ("accepted", "handed_off") else "closed"
    cur.execute("update desk_conversations set state=%s, close_reason=%s, closed_by=%s, closed_at=now(), updated_at=now(), "
                "path=coalesce(%s, path) where id=%s returning *", (state, reason, by, path, conversation_id))
    done = cur.fetchone()
    _event(cur, "desk.closed", conversation_id, {"by": by, "reason": reason, "note": note, "from": c["state"],
                                                  "record": closing_record(cur, done), **payload})
    return done


def closing_record(cur, c: dict) -> dict:
    """閉じたときの記録（ADR 0039 の決定 7 の表。外側のループの束ねる材料）。events の時刻から決定論で組み立てる。
    今は対応の種類・時間・費用・往復・結末まで。進め方・断りの理由・主題は、その段を入れたときに足す"""
    cur.execute("select name, occurred_at, payload from events where correlation_id=%s order by id", (corr(c["id"]),))
    evs = cur.fetchall()
    t0 = c["created_at"]

    def first(*names):
        e = next((e for e in evs if e["name"] in names), None)
        return round((e["occurred_at"] - t0).total_seconds()) if e else None

    first_reply = next((e for e in evs if e["name"] in ("desk.message", "desk.answered") and (e["payload"] or {}).get("role") != "requester"), None)
    return {"path": c.get("path"), "kind": c["kind"],
            "sec_first_reply": round((first_reply["occurred_at"] - t0).total_seconds()) if first_reply else None,
            "sec_routed": first("desk.routed", "desk.drafted"), "sec_delivered": first("desk.delivered", "desk.tickets_created"),
            "cost_usd": float(c.get("cost_usd") or 0), "rounds": c.get("rounds") or 0, "follow_ups": c.get("follow_ups") or 0,
            "reroutes": sum(1 for e in evs if e["name"] == "desk.routed") - 1 if any(e["name"] == "desk.routed" for e in evs) else 0,
            "outcome": c.get("close_reason"), "derived_from": c.get("derived_from"), "superseded_by": c.get("superseded_by")}


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


# ------------------------------------------------------------------ 単発の依頼の確認と受取確認（ADR 0039 の決定 2・5。窓口の道具は増やさない）
CONFIRM_ANSWERS = ("run", "cancel")
RECEIPT_ANSWERS = ("accept", "more", "return")


def _answer_step(cur, who: dict, conversation_id: int, kind: str, state: str, option: str, note: str) -> dict:
    """窓口（API・MCP）からの答えを、受信トレイの件の答え（inbox.answer、正本は 1 つ）として通す"""
    import inbox
    c = _conversation(cur, who, conversation_id)
    if c["state"] != state:
        raise DeskError(409, f"いまは答えられない（状態 {c['state']}）")
    cur.execute("select count(*) as n from events where correlation_id=%s and name='desk.routed'", (corr(conversation_id),))
    key = f"task:{kind}:{conversation_id}:{cur.fetchone()['n']}"
    res, msg = inbox.answer(cur, key, option, who["name"], "mcp", note=note, raw=note,
                            role=None if who["role"] == "operator" else "requester")
    if res not in ("answered", "noted"):
        raise DeskError(409 if res in ("already", "not_found", "failed") else 400, msg)
    return c


def confirm_route(cur, who: dict, conversation_id: int, option: str, note: str = "") -> dict:
    """振り分けの確認（send の段）に答える。run（実行する）か cancel（やめる）"""
    if option not in CONFIRM_ANSWERS:
        raise DeskError(400, f"answer は {' / '.join(CONFIRM_ANSWERS)}")
    return _answer_step(cur, who, conversation_id, "desk_confirm", "awaiting_confirm", option, note)


def receive(cur, who: dict, conversation_id: int, option: str, note: str = "") -> dict:
    """提出の受取確認に答える。accept（受け取った）・more（続き。一言が要る）・return（差し戻す。一言が要る）"""
    if option not in RECEIPT_ANSWERS:
        raise DeskError(400, f"answer は {' / '.join(RECEIPT_ANSWERS)}")
    if option != "accept":
        note = _text(note, 2000)
    return _answer_step(cur, who, conversation_id, "desk_receipt", "delivered", option, note or "")


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
