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
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

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
# 単発の依頼のループの値（definitions/desk.yaml の loop を写す。食い違いはテストで見る。仮置き。運用パラメータとして工場が調整してよい）
LOOP = {
    "targets": {"ack": 60,
                "route": {"default": 300, "direct_line": 300, "operate": 300, "quick": 300, "analysis": 900},
                "deliver": {"operate": 300, "quick": 900, "analysis": 7200}},
    "progress_at": 0.8,
    "max_follow_ups": 5,
    "remind_after_days": [3, 7],
    "to_operator_after_days": 10,
    "decline_reasons": {"no_operation": "決まった操作の一覧にない", "no_data_source": "工場がつながっていないデータ源が要る",
                        "needs_production_data": "本番のデータが要る（後のフェーズ）", "too_large": "単発で受けるには大きすぎる",
                        "no_capability": "工場がまだ持たない対応（集計・分析など）"},
    "alternatives": {"other_path": "できる範囲の別の対応の種類（チケット化など）", "add_capability": "工場に足す提案（建設者に回す）",
                     "to_human": "人に回す（運用主）"},
}
TZ = ZoneInfo(os.environ.get("REPORT_TZ", "Asia/Tokyo"))
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
                      source_ref: str | None = None, derived_from: int | None = None, notify_to: list[str] | None = None) -> dict:
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
    notify_to = check_notify(cur, notify_to)
    wanted = {k: v for k, v in (("level", level), ("due", due), ("notify", notify_to)) if v is not None}
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
        ahead, by = route_eta(cur, c["created_at"])
        _event(cur, "desk.message", c["id"], {"author": "factory", "role": "factory", "kind": "ack", "body": ack_text(c["id"], ahead, by),
                                              "route_by": by.isoformat(), "ahead": ahead})
    return c


NOTIFY_DESK_ONLY = "desk"   # 知らせは窓口の会話だけ（Slack などには送らない）


def check_notify(cur, notify_to) -> list[str] | None:
    """知らせの受け取り方（ADR 0039 の決定 5a。会話の notify 欄、wanted.notify に持つ）。選べるのは notifications.yaml にある宛先の名前か
    desk（窓口の会話だけ）。依頼の本文に書かれた宛先には送らない。None は既定（done.task の宛先）"""
    if notify_to is None:
        return None
    if isinstance(notify_to, str):
        notify_to = [notify_to]
    if not isinstance(notify_to, list) or not notify_to or len(notify_to) > 5 or not all(isinstance(x, str) for x in notify_to):
        raise DeskError(400, "notify は宛先の名前の並び（5 つまで）か desk")
    import notify
    cfg = notify.cached(cur)
    allowed = {n for n, d in cfg.destinations.items() if d.party in notify.KINDS["done.task"]["parties"]}
    bad = [x for x in notify_to if x != NOTIFY_DESK_ONLY and x not in allowed]
    if bad:
        raise DeskError(400, f"notify は通知の設定にある宛先の名前か desk（{' / '.join(sorted(allowed)) or '宛先なし'}）: {', '.join(bad)}")
    return sorted(set(notify_to))


def route_eta(cur, now: datetime) -> tuple[int, datetime]:
    """振り分けを返せる目安の時刻（決定 3）。窓口の枠に先に積まれている相談の数だけ、振り分けの目安を足す（枠が埋まっているとき）"""
    cur.execute("select count(*) as n from jobs where kind='desk_consult' and state='queued' and not cancel_requested")
    ahead = cur.fetchone()["n"]
    return ahead, now + timedelta(seconds=LOOP["targets"]["route"]["default"] * (1 + ahead))


def clock(t: datetime) -> str:
    """依頼者に見せる時刻（製品の時間帯。日をまたぐなら日付も）"""
    lt, now = t.astimezone(TZ), datetime.now(TZ)
    return f"{lt:%H:%M}" if lt.date() == now.date() else f"{lt:%m/%d %H:%M}"


def ack_text(cid: int, ahead: int, by: datetime) -> str:
    wait = f"先に {ahead} 件の相談があるので、{clock(by)} ごろまでに返します" if ahead else f"{clock(by)} ごろまで（5 分ほど）に返します"
    return (f"依頼 {cid} を受け付けました。次は振り分けです。相談役が、決まった操作で済むか、チケットに切るか、受けられないかを決めて、"
            f"{wait}（問いがあればそのときに聞きます）。")


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
    _resolve_inbox(cur, conversation_id, by, reason)
    return done


def _resolve_inbox(cur, conversation_id: int, by: str, outcome: str) -> None:
    """終えた会話の受信トレイの件（確認・受取確認・断り・返事待ち）を消す（取り下げで閉じた依頼の件を開いたままにしない）"""
    import inbox
    cur.execute("select distinct o.payload->>'key' as key from events o where o.name='inbox.requested' and o.correlation_id=%s",
                (corr(conversation_id),))
    for r in cur.fetchall():
        inbox.resolve(cur, r["key"], by, outcome)


def closing_record(cur, c: dict) -> dict:
    """閉じたときの記録（ADR 0039 の決定 7 の表。外側のループの束ねる材料）。events の時刻から決定論で組み立てる。
    今は対応の種類・時間・費用・往復・結末まで。進め方・断りの理由・主題は、その段を入れたときに足す"""
    cur.execute("select id, name, occurred_at, payload from events where correlation_id=%s order by id", (corr(c["id"]),))
    evs = cur.fetchall()
    t0 = c["created_at"]
    ms = milestones(c, evs)

    def first(*names):
        e = next((e for e in evs if e["name"] in names), None)
        return round((e["occurred_at"] - t0).total_seconds()) if e else None

    first_reply = next((e for e in evs if e["name"] in ("desk.message", "desk.answered") and (e["payload"] or {}).get("role") != "requester"), None)
    return {"path": c.get("path"), "kind": c["kind"],
            "sec_first_reply": round((first_reply["occurred_at"] - t0).total_seconds()) if first_reply else None,
            "sec_routed": first("desk.routed", "desk.drafted"), "sec_delivered": first("desk.delivered", "desk.tickets_created"),
            "cost_usd": float(c.get("cost_usd") or 0), "rounds": c.get("rounds") or 0, "follow_ups": c.get("follow_ups") or 0,
            "reroutes": sum(1 for e in evs if e["name"] == "desk.routed") - 1 if any(e["name"] == "desk.routed" for e in evs) else 0,
            "outcome": c.get("close_reason"), "derived_from": c.get("derived_from"), "superseded_by": c.get("superseded_by"),
            "late": sorted({(e["payload"] or {}).get("milestone") for e in evs if e["name"] == "desk.late"} - {None}),
            "met_route": all(m["end"] <= m["deadline"] for m in ms if m["milestone"] == "route" and m["end"]),
            "met_deliver": all(m["end"] <= m["deadline"] for m in ms if m["milestone"] == "deliver" and m["end"]),
            "met_ack": first_reply is not None and (first_reply["occurred_at"] - t0).total_seconds() <= LOOP["targets"]["ack"],
            "declined": next(((e["payload"] or {}).get("reason") for e in reversed(evs) if e["name"] == "desk.declined"), None),
            "cap_hit": any(e["name"] == "desk.cap_hit" for e in evs)}


QUIET_KINDS = ("ack", "progress")   # 工場の返事のうち、マイルストーンを終えないもの（受付の返事・途中の様子）


def route_target(path: str | None) -> int:
    t = LOOP["targets"]["route"]
    return t.get(path or "default", t["default"])


def milestones(c: dict, evs: list[dict]) -> list[dict]:
    """応答時間の目安のマイルストーン（決定 3）を events から決定論で切り出す。返り値は
    [{milestone: route|deliver, key, start, deadline, end（まだなら None）, target_sec}]。
      route:   依頼者の発言（振り分けの前・続き・差し戻し）から、工場の次の返事（問い・振り分け・下書き・断り・誤りの知らせ）まで。
               振り分けた後の言い足し（記録だけ）は数えない
      deliver: 実行に入った（確認された・確認を飛ばした）時から提出まで。analysis は振り分けで示した時刻（deliver_by）があればそれ
    最初の返事（ack）は closing_record の met_ack で見る"""
    out, busy, open_route, route_ev, run = [], False, None, None, None
    path = c.get("path")
    for e in evs:
        p, name = e["payload"] or {}, e["name"]
        if name == "desk.routed":
            route_ev = p
        if name in ("desk.message", "desk.answered") and p.get("role") == "requester":
            if not busy and open_route is None:
                open_route = {"milestone": "route", "key": f"route:{e['id']}", "start": e["occurred_at"]}
        elif name in ("desk.message", "desk.answered", "desk.drafted", "desk.routed", "desk.declined") and p.get("kind") not in QUIET_KINDS:
            if open_route is not None:
                open_route["end"] = e["occurred_at"]
                open_route["path"] = (route_ev or {}).get("path") if name == "desk.routed" else None
                out.append(open_route)
                open_route = None
        if name == "desk.routed":
            busy = True
        elif name in ("desk.rerouted", "desk.closed"):
            busy = False
        if name == "desk.confirmed_run":
            run = {"milestone": "deliver", "key": f"deliver:{e['id']}", "start": e["occurred_at"], "path": (route_ev or {}).get("path") or path,
                   "deliver_by": (route_ev or {}).get("deliver_by")}
        elif name in ("desk.delivered", "desk.closed") and run is not None:
            run["end"] = e["occurred_at"]
            out.append(run)
            run = None
    if open_route is not None and c["state"] not in ENDED:
        out.append({**open_route, "end": None, "path": None})
    if run is not None and c["state"] not in ENDED:
        out.append({**run, "end": None})
    for m in out:
        if m["milestone"] == "route":
            m["target_sec"] = route_target(m.get("path"))
            m["deadline"] = m["start"] + timedelta(seconds=m["target_sec"])
        else:
            target = LOOP["targets"]["deliver"].get(m.get("path"))
            by = _parse_time(m.pop("deliver_by", None)) if m.get("path") == "analysis" else None
            m["deadline"] = by or (m["start"] + timedelta(seconds=target) if target else None)
            m["target_sec"] = round((m["deadline"] - m["start"]).total_seconds()) if m["deadline"] else None
            m.pop("deliver_by", None)
    return [m for m in out if m.get("deadline")]


def _parse_time(v) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        t = datetime.fromisoformat(v)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


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
    return {"conversation": view_conversation(c), "status": status_view(cur, c), "messages": messages, "tickets": tickets,
            "draft": {"draft_id": d["id"], **d["payload"]} if d else None,
            "last_id": messages[-1]["id"] if messages else after}


STEP_JA = {"open": ("振り分け", "相談役が対応の種類を決めて返します"),
           "waiting_requester": ("振り分け", "相談役の問いへの返事を待っています"),
           "drafted": ("振り分け", "下書きの確認を待っています（confirm）"),
           "confirmed": ("振り分け", "下書きからチケットを起票します"),
           "answered": ("提出", "回答しました。続きがあれば返事をください"),
           "routed": ("振り分け", "振り分けました。確認か実行に進みます"),
           "awaiting_confirm": ("確認", "実行してよいかの答えを待っています（実行する・やめる）"),
           "running": ("実行", "実行しています。終わったら提出します"),
           "delivered": ("提出", "受け取ったかの答えを待っています"),
           "accepted": ("終わり", "受け取って完了しました"),
           "handed_off": ("終わり", "チケットにして製品のラインに渡しました（進み具合はチケットの状態で）"),
           "closed": ("終わり", "閉じました")}
CLOSE_JA = {"accepted": "受け取った", "declined": "断りに納得した", "superseded": "別案の依頼に引き継いだ", "withdrawn": "取り下げた",
            "handed_off": "チケットにして渡した"}


def status_view(cur, c: dict) -> dict:
    """依頼者に見えるもの（ADR 0039 の決定 6）。どの入口（MCP・API・コンソール）から読んでも同じ。
    今のステップと次に何が起きるか・いつ頃か、振り分けの結果、途中の様子、提出（成果物と所見・受け取りの選択肢）、断りの理由と代わりの案"""
    cur.execute("select id, name, occurred_at, payload from events where correlation_id=%s order by id", (corr(c["id"]),))
    evs = cur.fetchall()

    def last(name):
        return next((e for e in reversed(evs) if e["name"] == name), None)

    step, nxt = STEP_JA.get(c["state"], (c["state"], ""))
    open_ms = [m for m in milestones(c, evs) if m["end"] is None]
    out = {"step": step, "next": nxt, "by": open_ms[-1]["deadline"].isoformat() if open_ms else None, "path": c.get("path")}
    r = last("desk.routed")
    if r:
        p = r["payload"] or {}
        out["route"] = {"round": p.get("round"), "path": p.get("path"), "declined": bool(p.get("declined")), "op": p.get("op"),
                        "what": (p.get("preview") or {}).get("text"), "tier": p.get("tier"), "why": p.get("why") or "",
                        "request": p.get("request") or {}, "deliver_by": p.get("deliver_by")}
    pr = last("desk.message") if c["state"] in ("open", "running") else None
    if pr and (pr["payload"] or {}).get("kind") == "progress":
        out["progress"] = {"at": pr["occurred_at"].isoformat(), "body": pr["payload"].get("body")}
    d = last("desk.delivered")
    if d:
        p = d["payload"] or {}
        out["delivered"] = {"round": p.get("round"), "at": d["occurred_at"].isoformat(), "ok": p.get("ok"), "summary": p.get("summary"),
                            "cap_hit": bool(p.get("cap_hit")), "declined": bool(p.get("declined"))}
    dc = last("desk.declined")
    if dc and d and d["id"] > dc["id"]:
        p = dc["payload"] or {}
        out["declined"] = {"reason": p.get("reason"), "reason_ja": LOOP["decline_reasons"].get(p.get("reason"), p.get("reason")),
                           "why": p.get("why") or "", "alternatives": p.get("alternatives") or []}
    if c["state"] == "awaiting_confirm":
        out["answer"] = {"how": "confirm に answer", "options": [{"id": "run", "label": "実行する"}, {"id": "cancel", "label": "やめる"}]}
    elif c["state"] == "delivered":
        declined = bool((out.get("delivered") or {}).get("declined"))
        out["answer"] = {"how": "messages に answer（more・return は body に一言）",
                         "options": ([{"id": "accept", "label": "納得した"}, {"id": "more", "label": "別案で進める"}, {"id": "return", "label": "断りを差し戻す"}]
                                     if declined else
                                     [{"id": "accept", "label": "受け取った"}, {"id": "more", "label": "続きを頼む"}, {"id": "return", "label": "差し戻す"}])}
    if c["state"] in ENDED:
        out["closed"] = {"reason": c.get("close_reason"), "reason_ja": CLOSE_JA.get(c.get("close_reason")), "by": c.get("closed_by"),
                         "superseded_by": c.get("superseded_by")}
    if c.get("derived_from"):
        out["derived_from"] = c["derived_from"]
    return out


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
    """提出の受取確認に答える。accept（受け取った）・more（続き。一言が要る）・return（差し戻す。一言が要る）。
    断りの提出なら accept（納得した）・more（別案で進める。一言は任意）・return（断りを差し戻す。一言が要る）"""
    if option not in RECEIPT_ANSWERS:
        raise DeskError(400, f"answer は {' / '.join(RECEIPT_ANSWERS)}")
    # 断りの提出（決定 4）なら、同じ 3 つの語を 納得した・別案で進める・断りを差し戻す として受ける（道具を増やさない）
    cur.execute("select payload from events where correlation_id=%s and name='desk.delivered' order by id desc limit 1", (corr(conversation_id),))
    declined = bool(((cur.fetchone() or {}).get("payload") or {}).get("declined"))
    if option == "return" or (option == "more" and not declined):
        note = _text(note, 2000)
    elif note:
        note = _text(note, 2000)
    return _answer_step(cur, who, conversation_id, "desk_decline" if declined else "desk_receipt", "delivered", option, note or "")


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


_DESK_TASK = re.compile(r"^task:desk_[a-z]+:(\d+):\d+$")


def _desk_owner(cur, item: dict) -> dict | None:
    """窓口の単発の依頼の件（task:desk_confirm|desk_receipt|desk_decline|desk_waiting:<会話>:<回>）なら、その会話の製品と依頼者"""
    m = _DESK_TASK.match(item.get("key") or "")
    if not m or cur is None:
        return None
    cur.execute("select product, opened_by from desk_conversations where id=%s", (int(m.group(1)),))
    return cur.fetchone() or {"product": None, "opened_by": None}


def _inbox_visible(who: dict, item: dict, cur=None) -> bool:
    owner = _desk_owner(cur, item)
    if owner is not None:
        # 窓口の依頼の件は、その会話の製品を見られる運用主と、会話を開いた依頼者本人だけ（ほかの依頼者の件は見せない）
        if item.get("who") == "owner":   # 運用主に回した件（依頼者の返事待ちなど）
            return who["role"] == "operator" and bool(owner["product"]) and can_product(who, owner["product"])
        if not owner["product"] or not can_product(who, owner["product"]):
            return False
        item.setdefault("product", owner["product"])
        return who["role"] == "operator" or owner["opened_by"] == who["name"]
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
            "url": item.get("url") or "", "product": item.get("product") or inbox_product(item), "since": inbox._iso(item.get("since")), "age_sec": item.get("age_sec"),
            "answerable": bool(a), "irreversible": bool(a.get("irreversible")),
            "recommended": next((o["label"] for o in a.get("options") or [] if o["id"] == rec), None) if rec else None,
            "answered": item.get("answered")}


def inbox_list(cur, who: dict, *, include_answered: bool = False) -> dict:
    """自分が答えてよい受信トレイの件。待ちの長い順。include_answered で、答え済みで反映を待っている件も入れる"""
    import inbox
    xs = [i for i in inbox.items(cur, include_answered=include_answered) if _inbox_visible(who, i, cur)]
    return {"count": sum(1 for i in xs if not i.get("answered")), "items": [_inbox_summary(i) for i in xs]}


def _inbox_item(cur, who: dict, key) -> dict:
    import inbox
    if not isinstance(key, str) or not inbox.KEY.match(key):
        raise DeskError(400, "key は受信トレイの件の key（inbox_list の key）")
    it = inbox.find(cur, key)
    # 見えない件は「ない」と同じに返す（ほかの人の件があることも漏らさない）
    if it is None or not _inbox_visible(who, it, cur):
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
