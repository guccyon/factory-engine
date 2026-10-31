"""受信トレイの通知（ADR 0026 の決定 7）。番人の周回から tick() を呼ぶ。

- 受信トレイ（inbox.items）に出た件を、通知先へまとめて 1 通で知らせる。初回・再通知・代理への切り替えの 3 つ
- 通知先は工場を建てるときのオプション（INBOX_NOTIFY）。受信トレイ自体は通知先がなくても常にコンソールにある
    log           ログだけ（既定）
    github-issue  工場の状況 Issue（stoppage と同じ Issue）にコメントする。Gen1 はこれを見回りが読んで Claude アプリに知らせる
    slack         Slack の Incoming Webhook（SLACK_WEBHOOK_URL。値は .env に置き、リポジトリに書かない）
- 記録は events だけ: inbox.notified（1 件・1 回ごと。dedupe_key で二重を防ぐ）、inbox.escalated、inbox.cleared（受信トレイから消えた。待ち時間を SLI に）
- 夜間など知らせない時間（INBOX_QUIET、例 22-7）は送らず、明けた最初の周回でまとめて送る
- 通知の失敗でラインを止めない。どの通知先にも届かなければ記録せず、次の周回でもう一度送る
"""
from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

import inbox

ENV = os.environ
CHANNELS = [c.strip() for c in ENV.get("INBOX_NOTIFY", "log").split(",") if c.strip()]
RENOTIFY_MIN = int(ENV.get("INBOX_RENOTIFY_MIN", "240"))     # 前の通知からこれだけたっても残っていれば知らせ直す（仮置き）
MAX_RENOTIFY = int(ENV.get("INBOX_MAX_RENOTIFY", "3"))       # 知らせ直すのはここまで。あとは代理への切り替えと受信トレイに任せる
ESCALATE_MIN = int(ENV.get("INBOX_ESCALATE_MIN", "1440"))    # 出てからこれだけたてば代理（PROXY）にも知らせる（仮置き）
PROXY = ENV.get("INBOX_PROXY", "builder")                    # 代理の担当（Gen1 は建設者）
QUIET = ENV.get("INBOX_QUIET", "")                           # "22-7" のように時（REPORT_TZ）で。空なら常に送る
TZ = ZoneInfo(ENV.get("REPORT_TZ", "Asia/Tokyo"))
CONSOLE_URL = ENV.get("CONSOLE_URL", "").rstrip("/")        # 相対リンク（/#choice-1 など）の前に付ける。空ならリンクを付けない
MENTIONS = dict(p.split("=", 1) for p in ENV.get("INBOX_SLACK_MENTIONS", "").split(",") if "=" in p)   # who=<@U…>


def _log(*a):
    print(datetime.now(timezone.utc).strftime("%H:%M:%S"), "[inbox]", *a, flush=True)


def quiet(now: datetime, spec: str | None = None) -> bool:
    spec = QUIET if spec is None else spec
    if not spec or "-" not in spec:
        return False
    a, b = (int(x) for x in spec.split("-", 1))
    h = now.astimezone(TZ).hour
    return a <= h < b if a < b else (h >= a or h < b)


def _history(cur) -> dict[str, dict]:
    """key ごとの通知の記録（回数・最後の時刻・最初の時刻・代理に回したか・消えたか）"""
    cur.execute("select name, occurred_at, payload from events where name in ('inbox.notified','inbox.escalated','inbox.cleared') "
                "and occurred_at > now() - interval '30 days' order by id")
    h: dict[str, dict] = {}
    for r in cur.fetchall():
        k = r["payload"]["key"]
        if r["name"] == "inbox.cleared":
            h.pop(k, None)   # 消えたあとにまた出たら（同じ停止の key が開き直したなど）新しい件として扱う
            continue
        s = h.setdefault(k, {"count": 0, "first": r["occurred_at"], "last": None, "escalated": False, "payload": r["payload"]})
        if r["name"] == "inbox.notified":
            s["count"] += 1
            s["last"] = r["occurred_at"]
        else:
            s["escalated"] = True
    return h


def due(items: list[dict], hist: dict[str, dict], now: datetime) -> list[tuple[dict, str]]:
    """知らせる件と理由（new / renotify / escalate）"""
    out = []
    for i in items:
        if i["kind"] == "error":
            continue
        s = hist.get(i["key"])
        if not s or not s["count"]:
            out.append((i, "new"))
        elif not s["escalated"] and i["age_sec"] is not None and i["age_sec"] >= ESCALATE_MIN * 60 and i["who"] != PROXY:
            out.append((i, "escalate"))
        elif s["count"] <= MAX_RENOTIFY and (now - s["last"]).total_seconds() >= RENOTIFY_MIN * 60:
            out.append((i, "renotify"))
    return out


REASON = {"new": "新しく", "renotify": "まだ残っている", "escalate": "長く待っているので代理にも"}


def _link(url: str) -> str:
    if url.startswith("https://"):
        return url
    return CONSOLE_URL + url if url and CONSOLE_URL else ""


def _dur(sec) -> str:
    if sec is None:
        return ""
    return f"{sec // 60} 分" if sec < 3600 else f"{sec // 3600} 時間 {sec % 3600 // 60} 分"


def render(batch: list[tuple[dict, str]], channel: str) -> str:
    lines = [f"**受信トレイ: 人の出番が {len(batch)} 件**" if channel != "slack" else f"*受信トレイ: 人の出番が {len(batch)} 件*"]
    for i, why in batch:
        who = inbox.WHO.get(i["who"], i["who"])
        if channel == "slack" and MENTIONS.get(i["who"]):
            who = f"{MENTIONS[i['who']]} {who}"
        if why == "escalate":
            who += f" → {inbox.WHO.get(PROXY, PROXY)}" + (f" {MENTIONS[PROXY]}" if channel == "slack" and MENTIONS.get(PROXY) else "")
        link = _link(i.get("url") or "")
        title = (f"<{link}|{i['title']}>" if channel == "slack" else f"[{i['title']}]({link})") if link else i["title"]
        wait = f"（{_dur(i['age_sec'])}待ち）" if i.get("age_sec") else ""
        lines.append(f"- {REASON[why]} {who}: {title}{wait} — {i['action']}")
    return "\n".join(lines)


def _send(channel: str, batch: list[tuple[dict, str]]) -> bool:
    import stoppage   # 状況 Issue の探し方・GitHub の呼び方は stoppage に 1 か所
    if channel == "github-issue":
        batch = [(i, w) for i, w in batch if not (w == "new" and i.get("announced"))]   # 停止は開いたときに stoppage が同じ Issue に書いている
        if not batch:
            return True
        if not (stoppage.STATUS_REPO and ENV.get("GH_TOKEN")) or ENV.get("WORKER_DRY_RUN"):
            _log("github-issue skipped (no STATUS_REPO/GH_TOKEN or dry run)")
            return False
        stoppage._gh("POST", f"repos/{stoppage.STATUS_REPO}/issues/{stoppage._status_issue()}/comments", {"body": render(batch, channel)})
        return True
    if channel == "slack":
        url = ENV.get("SLACK_WEBHOOK_URL", "")
        if not url.startswith("https://hooks.slack.com/"):
            _log("slack skipped (SLACK_WEBHOOK_URL がない)")
            return False
        req = urllib.request.Request(url, method="POST", data=json.dumps({"text": render(batch, channel)}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15):
            return True
    if channel == "log":
        _log(render(batch, channel).replace("\n", " | "))
        return True
    _log("unknown channel", channel)
    return False


def tick(cur, now: datetime | None = None, channels: list[str] | None = None, sender=None) -> dict:
    """1 周回ぶん。返り値は数（テストと日報向け）"""
    now = now or datetime.now(timezone.utc)
    channels = CHANNELS if channels is None else channels
    sender = sender or _send
    xs = inbox.items(cur, now)
    hist = _history(cur)
    # 消えた件: 待ち時間を残す（判断待ちの滞留の SLI。最初の通知から消えるまで）
    present = {i["key"] for i in xs}
    cleared = 0
    for k, s in hist.items():
        if k not in present and s["count"]:
            cur.execute("insert into events(name, payload) values ('inbox.cleared', %s)",
                        (Jsonb({"key": k, "kind": s["payload"].get("kind"), "who": s["payload"].get("who"), "notified": s["count"],
                                "escalated": s["escalated"], "waited_sec": int((now - s["first"]).total_seconds())}),))
            cleared += 1
    if quiet(now):
        return {"items": len(xs), "sent": 0, "cleared": cleared, "quiet": True}
    batch = due(xs, hist, now)
    if not batch:
        return {"items": len(xs), "sent": 0, "cleared": cleared, "quiet": False}
    ok = []
    for ch in channels:
        try:
            if sender(ch, batch):
                ok.append(ch)
        except Exception as e:  # noqa: BLE001  通知の失敗でラインを止めない
            _log("send failed", ch, repr(e))
    if not ok:
        return {"items": len(xs), "sent": 0, "cleared": cleared, "quiet": False, "failed": True}
    for i, why in batch:
        n = (hist.get(i["key"]) or {}).get("count", 0)
        if why == "escalate":
            cur.execute("insert into events(name, payload) values ('inbox.escalated', %s)",
                        (Jsonb({"key": i["key"], "kind": i["kind"], "who": i["who"], "to": PROXY, "age_sec": i["age_sec"]}),))
        cur.execute("insert into events(name, payload, dedupe_key) values ('inbox.notified', %s, %s) on conflict (dedupe_key) do nothing",
                    (Jsonb({"key": i["key"], "kind": i["kind"], "who": i["who"], "reason": why, "count": n + 1, "channels": ok,
                            "age_sec": i["age_sec"]}), f"inbox.notified:{i['key']}:{n + 1}:{now:%Y%m%dT%H%M%S}"))
    return {"items": len(xs), "sent": len(batch), "cleared": cleared, "quiet": False, "channels": ok}
