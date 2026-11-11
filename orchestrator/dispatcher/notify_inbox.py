"""受信トレイの通知（ADR 0026 の決定 7、宛先の出し分けは ADR 0032）。番人の周回から tick() を呼ぶ。

- 受信トレイ（inbox.items）に出た件を、宛先ごとにまとめて 1 通で知らせる。初回・再通知・代理への切り替えの 3 つ
- 宛先は通知の種類 request.inbox の割り当て（notify.py）。製品の .factory-config/notifications.yaml があれば、
  事業主の判断が要る件だけを事業主の宛先に送る（誰の出番か who で絞る。建設の判断は送らない）。
  なければ今までどおり INBOX_NOTIFY の通知先すべてに送る（log / github-issue / slack＝SLACK_WEBHOOK_URL）
- 受信トレイ自体は通知先がなくても常にコンソールにある
- 記録は events だけ: inbox.notified（1 件・1 宛先・1 回ごと。channels に宛先の名前。dedupe_key で二重を防ぐ）、inbox.escalated、
  inbox.cleared（受信トレイから消えた。待ち時間を SLI に）、inbox.written_back（答え済みを知らせ直した。1 つの答え・1 宛先に 1 回）
- 答え済みの件（inbox.answer。ADR 0034 の決定 5）は、その件を知らせた宛先に「誰が・どこで・何と答えたか。この件は閉じた」を書き戻す。
  答え済みで反映を待っている件は人の出番ではないので、知らせ直さない（受信トレイの items が除く）
- 夜間など知らせない時間（宛先の quiet。今までの設定では INBOX_QUIET、例 22-7）は送らず、明けた最初の周回でまとめて送る
- 通知の失敗でラインを止めない。届かなかった宛先には記録せず、次の周回でもう一度送る
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from urllib.parse import quote

from psycopg.types.json import Jsonb

import inbox
import notify

ENV = os.environ
RENOTIFY_MIN = int(ENV.get("INBOX_RENOTIFY_MIN", "240"))     # 前の通知からこれだけたっても残っていれば知らせ直す（仮置き）
MAX_RENOTIFY = int(ENV.get("INBOX_MAX_RENOTIFY", "3"))       # 知らせ直すのはここまで。あとは代理への切り替えと受信トレイに任せる
ESCALATE_MIN = int(ENV.get("INBOX_ESCALATE_MIN", "1440"))    # 出てからこれだけたてば代理（PROXY）にも知らせる（仮置き）
PROXY = ENV.get("INBOX_PROXY", "builder")                    # 代理の担当（Gen1 は建設者）
QUIET = ENV.get("INBOX_QUIET", "")                           # "22-7" のように時（REPORT_TZ）で。空なら常に送る
CONSOLE_URL = ENV.get("CONSOLE_URL", "").rstrip("/")        # 相対リンク（/#choice-1 など）の前に付ける。空ならリンクを付けない
MENTIONS = dict(p.split("=", 1) for p in ENV.get("INBOX_SLACK_MENTIONS", "").split(",") if "=" in p)   # who=<@U…>


def _log(*a):
    print(datetime.now(timezone.utc).strftime("%H:%M:%S"), "[inbox]", *a, flush=True)


def quiet(now: datetime, spec: str | None = None) -> bool:
    return notify.quiet(now, QUIET if spec is None else spec)


WRITE_BACK_HOURS = 48   # これより前の答えは書き戻さない（工場を止めていた間の答えをまとめて流さない）


def _events(cur) -> list[dict]:
    cur.execute("select name, occurred_at, payload from events where name in ('inbox.notified','inbox.escalated','inbox.cleared') "
                "and occurred_at > now() - interval '30 days' order by id")
    return cur.fetchall()


def _history(rows: list[dict], dest: str | None = None) -> dict[str, dict]:
    """key ごとの通知の記録（回数・最後の時刻・最初の時刻・代理に回したか）。dest を渡すとその宛先の分だけ。
    channels のない古い代理の記録はどの宛先にも効く"""
    h: dict[str, dict] = {}
    for r in rows:
        k = r["payload"]["key"]
        if r["name"] == "inbox.cleared":
            h.pop(k, None)   # 消えたあとにまた出たら（同じ停止の key が開き直したなど）新しい件として扱う
            continue
        chans = r["payload"].get("channels")
        if dest is not None and chans is not None and dest not in chans:
            continue
        s = h.setdefault(k, {"count": 0, "first": r["occurred_at"], "last": None, "escalated": False, "payload": r["payload"]})
        if r["name"] == "inbox.notified":
            # 宛先ごとなら数える。全体なら宛先ごとの回数のいちばん大きいもの（古い記録は 1 件に全部の通知先が入っている）
            s["count"] = s["count"] + 1 if dest is not None else max(s["count"], int(r["payload"].get("count") or 1))
            s["last"] = r["occurred_at"]
        else:
            s["escalated"] = True
    return h


def due(items: list[dict], hist: dict[str, dict], now: datetime, escalate: bool = True) -> list[tuple[dict, str]]:
    """知らせる件と理由（new / renotify / escalate）"""
    out = []
    for i in items:
        if i["kind"] == "error":
            continue
        s = hist.get(i["key"])
        if not s or not s["count"]:
            out.append((i, "new"))
        elif i.get("once"):   # 任意の依頼（実装後の画面）は知らせ直さず、代理にも回さない
            continue
        elif escalate and not s["escalated"] and i["age_sec"] is not None and i["age_sec"] >= ESCALATE_MIN * 60 and i["who"] != PROXY:
            out.append((i, "escalate"))
        elif s["count"] <= MAX_RENOTIFY and (now - s["last"]).total_seconds() >= RENOTIFY_MIN * 60:
            out.append((i, "renotify"))
    return out


REASON = {"new": "新しく", "renotify": "まだ残っている", "escalate": "長く待っているので代理にも"}


def _link(url: str, dest: notify.Destination | None = None, key: str = "") -> str:
    if dest is not None and dest.party == "business_owner":
        return url if url.startswith("https://") else ""   # 操業コンソールは工場のマシンの中。事業主の宛先には付けない（ADR 0032 の R7）
    if key and CONSOLE_URL:
        return f"{CONSOLE_URL}/inbox/{quote(key, safe='')}"   # 受信トレイのメッセージの詳細（2026-11-07）。PR などの元のリンクは詳細に出る
    if url.startswith("https://"):
        return url
    return CONSOLE_URL + url if url and CONSOLE_URL else ""


def _dur(sec) -> str:
    if sec is None:
        return ""
    return f"{sec // 60} 分" if sec < 3600 else f"{sec // 3600} 時間 {sec % 3600 // 60} 分"


def render(batch: list[tuple[dict, str]], channel: str, dest: notify.Destination | None = None) -> str:
    slack = channel in ("slack", "slack_webhook", "slack_app")
    mention = dest.mention if dest is not None else (lambda who: MENTIONS.get(who, ""))
    lines = [f"*受信トレイ: 判断待ちが {len(batch)} 件*" if slack else f"**受信トレイ: 判断待ちが {len(batch)} 件**"]
    for i, why in batch:
        who = inbox.WHO.get(i["who"], i["who"])
        if slack and mention(i["who"]):
            who = f"{mention(i['who'])} {who}"
        if why == "escalate":
            who += f" → {inbox.WHO.get(PROXY, PROXY)}" + (f" {mention(PROXY)}" if slack and mention(PROXY) else "")
        link = _link(i.get("url") or "", dest, i.get("key") or "")
        title = (f"<{link}|{i['title']}>" if slack else f"[{i['title']}]({link})") if link else i["title"]
        wait = f"（{_dur(i['age_sec'])}待ち）" if i.get("age_sec") else ""
        key = f" `{i['key']}`" if i.get("answer") else ""   # Claude に「受信トレイの <key> に答えて」と言えるように（ADR 0034 の順番 2）
        lines.append(f"- {REASON[why]} {who}: {title}{wait} — {i['action']}{key}")
    if any(i.get("answer") for i, _ in batch) and not (dest is not None and dest.party == "business_owner"):
        lines.append("手元の Claude からも答えられる（窓口の MCP。「受信トレイの `key` を読んで」「〜で答えて」と言う）")
    return "\n".join(lines)


def _send(channel, batch: list[tuple[dict, str]]) -> bool:
    """1 つの宛先に送る。channel は宛先（notify.Destination）か、今までの通知先の名前"""
    dest = channel if isinstance(channel, notify.Destination) else notify.implicit([channel]).destinations[channel]
    if dest.via == "github-issue":
        batch = [(i, w) for i, w in batch if not (w == "new" and i.get("announced"))]   # 停止は開いたときに stoppage が同じ Issue に書いている
        if not batch:
            return True
    return notify.post(dest, render(batch, dest.via, dest))


def render_answered(answers: list[dict], channel: str) -> str:
    slack = channel in ("slack", "slack_webhook", "slack_app")
    head = f"受信トレイ: 回答済 {len(answers)} 件"
    lines = [f"*{head}*" if slack else f"**{head}**"]
    lines += [f"- {a.get('title') or a['key']} — {inbox.answered_text(a)}。この件は閉じました" for a in answers]
    return "\n".join(lines)


def write_back(cur, now: datetime, dests: dict, rows: list[dict], poster) -> int:
    """答え済みの件を、その件を答えより前に知らせた宛先に書き戻す（ADR 0034 の決定 5）。届かなければ次の周回でもう一度"""
    cur.execute("select id, occurred_at, payload from events e where name='inbox.answered' and coalesce((payload->>'closes')::boolean, true) "
                "and occurred_at > %s - make_interval(hours => %s) order by id", (now, WRITE_BACK_HOURS))
    answers = cur.fetchall()
    if not answers:
        return 0
    cur.execute("select dedupe_key from events where name='inbox.written_back' and occurred_at > %s - make_interval(hours => %s)",
                (now, WRITE_BACK_HOURS * 2))
    done = {r["dedupe_key"] for r in cur.fetchall()}
    notified = [r for r in rows if r["name"] == "inbox.notified"]
    n = 0
    for d in dests.values():
        if notify.quiet(now, d.quiet):
            continue
        mine = []
        for a in answers:
            keys = {a["payload"].get("key"), *(a["payload"].get("also") or [])}
            told = any(r["payload"].get("key") in keys and r["occurred_at"] <= a["occurred_at"]
                       and (r["payload"].get("channels") is None or d.name in r["payload"]["channels"]) for r in notified)
            if told and f"inbox.written_back:{a['id']}:{d.name}" not in done:
                mine.append(a)
        if not mine:
            continue
        try:
            ok = poster(d.name, render_answered([a["payload"] for a in mine], d.via))
        except Exception as e:  # noqa: BLE001  通知の失敗でラインを止めない
            _log("write back failed", d.name, repr(e))
            ok = False
        if not ok:
            continue
        for a in mine:
            cur.execute("insert into events(name, payload, dedupe_key) values ('inbox.written_back', %s, %s) on conflict (dedupe_key) do nothing",
                        (Jsonb({"key": a["payload"].get("key"), "answer_id": a["id"], "channels": [d.name]}), f"inbox.written_back:{a['id']}:{d.name}"))
            n += 1
    return n


# 窓口の依頼の提出・断りの受取確認は、依頼者の選んだ手段で done.task として知らせる（ADR 0039 の決定 5a。desk_line.send_notices）。
# 受信トレイの通知では重ねて送らない
DONE_TASK_KINDS = ("desk_receipt", "desk_decline")


def tick(cur, now: datetime | None = None, channels: list[str] | None = None, sender=None, config: notify.Config | None = None,
         poster=None) -> dict:
    """1 周回ぶん。返り値は数（テストと日報向け）。channels を渡すと、その名前の今までの通知先に送る（テスト向け）。
    poster(宛先の名前, 文) は書き戻しの送り手（テスト向け。既定は notify.post）"""
    now = now or datetime.now(timezone.utc)
    if config is None:
        config = notify.implicit(channels, QUIET, MENTIONS) if channels is not None else notify.current(cur)
    dests = {d.name: d for d in notify.targets(config, "request.inbox")}
    send = sender or (lambda name, b: _send(dests[name], b))
    xs = inbox.items(cur, now)
    rows = _events(cur)
    hist = _history(rows)
    # 消えた件: 待ち時間を残す（判断待ちの滞留の SLI。最初の通知から消えるまで）
    present = {i["key"] for i in xs}
    cleared = 0
    for k, s in hist.items():
        if k not in present and s["count"]:
            cur.execute("insert into events(name, payload) values ('inbox.cleared', %s)",
                        (Jsonb({"key": k, "kind": s["payload"].get("kind"), "who": s["payload"].get("who"), "notified": s["count"],
                                "escalated": s["escalated"], "waited_sec": int((now - s["first"]).total_seconds())}),))
            cleared += 1
    wrote = write_back(cur, now, dests, rows, poster or (lambda name, text: notify.post(dests[name], text)))
    # 代理が建設者なら、設定のある工場では代理に回さない（建設の判断は建設者が問い合わせて拾う。ADR 0032 の決定 5）
    escalate = not (config.explicit and PROXY == "builder")
    sent_keys: set[str] = set()
    ok: list[str] = []
    quiet_all, tried = bool(dests), False
    for d in dests.values():
        if notify.quiet(now, d.quiet):
            continue
        quiet_all = False
        mine = [i for i in xs if i.get("task_kind") not in DONE_TASK_KINDS
                and any(t.name == d.name for t in notify.targets(config, "request.inbox", i["who"]))]
        dh = _history(rows, d.name)
        batch = due(mine, dh, now, escalate)
        if not batch:
            continue
        tried = True
        try:
            delivered = send(d.name, batch)
        except Exception as e:  # noqa: BLE001  通知の失敗でラインを止めない
            _log("send failed", d.name, repr(e))
            delivered = False
        if not delivered:
            continue
        ok.append(d.name)
        for i, why in batch:
            n = (dh.get(i["key"]) or {}).get("count", 0)
            if why == "escalate":
                cur.execute("insert into events(name, payload) values ('inbox.escalated', %s)",
                            (Jsonb({"key": i["key"], "kind": i["kind"], "who": i["who"], "to": PROXY, "age_sec": i["age_sec"], "channels": [d.name]}),))
            cur.execute("insert into events(name, payload, dedupe_key) values ('inbox.notified', %s, %s) on conflict (dedupe_key) do nothing",
                        (Jsonb({"key": i["key"], "kind": i["kind"], "who": i["who"], "reason": why, "count": n + 1, "channels": [d.name],
                                "age_sec": i["age_sec"]}), f"inbox.notified:{i['key']}:{d.name}:{n + 1}:{now:%Y%m%dT%H%M%S}"))
            sent_keys.add(i["key"])
    out = {"items": len(xs), "sent": len(sent_keys), "cleared": cleared, "quiet": quiet_all, "written_back": wrote}
    if tried and not ok:
        out["failed"] = True
    if ok:
        out["channels"] = ok
    return out
