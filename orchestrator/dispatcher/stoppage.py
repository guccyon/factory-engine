"""操業停止（Stoppage）の記録と通知（ADR 0005）。dispatcher の番人と factoryctl の両方から使う。

- 記録は events の stoppage.opened / stoppage.closed だけ（payload.key で対）。読むときは view stoppages
- 通知は 1 経路。NOTIFIER で差し替える（github-issue: 工場状況 Issue へのコメント / log: ログだけ）
- 記録や通知に失敗しても呼び出し元（ライン）は止めない。例外は握りつぶしてログに出す
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from psycopg.types.json import Jsonb

ENV = os.environ
CATEGORIES = ("env", "perm", "human", "idle", "factory-bug", "ticket", "external", "budget")   # idle: ラインが空（積まれたチケットがない）。人の承認待ち（human）と分ける（2026-10-28）
NOTIFIER = ENV.get("NOTIFIER", "github-issue")
STATUS_REPO = ENV.get("STATUS_REPO") or next((r.strip() for r in ENV.get("FACTORY_REPOS", "").split(",") if r.strip()), "")
STATUS_LABEL = "factory:status"
STATUS_TITLE = "工場の状況（番人の通知）"
# 通知先の番号（Issue でも PR でもよい。PR の会話にコメントする）。空なら factory:status の Issue を探す・作る（ADR 0005、D8）
STATUS_NUMBER = ENV.get("STATUS_NUMBER", "")
WOKEN_BY = ("pr_event", "watcher", "schedule", "human")   # 停止に気づいて builder を起こしたもの（D8）。人が起こした割合を健康レポートに


def _log(*a):
    print(datetime.now(timezone.utc).strftime("%H:%M:%S"), "[stoppage]", *a, flush=True)


def _iso(t) -> str | None:
    if t is None:
        return None
    return t.isoformat() if hasattr(t, "isoformat") else str(t)


def open_stoppage(cur, key: str, category: str, what: str, *, waiting_for: str = "", cause: str = "", detected_by: str = "watcher",
                  started_at=None, prevention: str = "", correlation_id: str | None = None, job_id: int | None = None, quiet: bool = False) -> bool:
    """開いたら True。同じ key がすでに開いていれば（dedupe）False"""
    if category not in CATEGORIES:
        raise ValueError(f"category must be one of {CATEGORIES}")
    payload = {"key": key, "category": category, "what": what, "waiting_for": waiting_for, "cause": cause,
               "detected_by": detected_by, "started_at": _iso(started_at), "prevention": prevention}
    cur.execute("insert into events(name, correlation_id, job_id, payload, dedupe_key) values ('stoppage.opened',%s,%s,%s,%s) "
                "on conflict (dedupe_key) do nothing returning id", (correlation_id, job_id, Jsonb(payload), f"stoppage.opened:{key}"))
    opened = cur.fetchone() is not None
    if opened and not quiet:
        notify("opened", {**payload, "correlation_id": correlation_id, "job_id": job_id}, cur)
    return opened


def close_stoppage(cur, key: str, *, remedy: str = "", prevention: str = "", ended_at=None, closed_by: str = "watcher", quiet: bool = False,
                   woken_by: str | None = None) -> bool:
    cur.execute("select 1 from stoppages where key=%s and ended_at is null", (key,))
    if cur.fetchone() is None:
        return False
    payload = {"key": key, "remedy": remedy, "prevention": prevention or None, "ended_at": _iso(ended_at), "closed_by": closed_by,
               "woken_by": woken_by if woken_by in WOKEN_BY else None}
    cur.execute("insert into events(name, payload, dedupe_key) values ('stoppage.closed',%s,%s) on conflict (dedupe_key) do nothing returning id",
                (Jsonb({k: v for k, v in payload.items() if v is not None}), f"stoppage.closed:{key}"))
    closed = cur.fetchone() is not None
    if closed and not quiet:
        cur.execute("select * from stoppages where key=%s", (key,))
        notify("closed", cur.fetchone() or payload, cur)
    return closed


def open_keys(cur, prefix: str = "") -> list[str]:
    cur.execute("select key from stoppages where ended_at is null and key like %s", (prefix + "%",))
    return [r["key"] for r in cur.fetchall()]


# ------------------------------------------------------------------ 通知（1 経路。差し替え可能）
def _gh(method: str, path: str, body: dict | None = None):
    req = urllib.request.Request(f"https://api.github.com/{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {ENV['GH_TOKEN']}", "Accept": "application/vnd.github+json",
                                          "Content-Type": "application/json", "User-Agent": "factory-watcher"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
        return json.loads(data) if data else None


def _status_issue() -> int:
    if STATUS_NUMBER.isdigit():
        return int(STATUS_NUMBER)
    found = _gh("GET", f"repos/{STATUS_REPO}/issues?state=open&labels={urllib.parse.quote(STATUS_LABEL)}&per_page=1") or []
    if found:
        return found[0]["number"]
    created = _gh("POST", f"repos/{STATUS_REPO}/issues", {
        "title": STATUS_TITLE, "labels": [STATUS_LABEL],
        "body": "工場の番人（dispatcher）が操業停止の開始と終了をこの Issue にコメントします（ADR 0005）。本文は開いている停止の一覧で、番人が書き換えます。閉じないでください。"})
    return created["number"]


def _text(kind: str, s: dict) -> str:
    def g(k):
        return s.get(k) or ""
    if kind == "opened":
        lines = [f"**停止が始まりました** `{g('key')}`（{g('category')}、検知: {g('detected_by')}）", "", f"- 何が止まったか: {g('what')}"]
        if g("waiting_for"):
            lines.append(f"- 待っているもの: {g('waiting_for')}")
        if g("cause"):
            lines.append(f"- 原因: {g('cause')}")
        return "\n".join(lines)
    mins = ""
    if s.get("started_at") and s.get("ended_at") and hasattr(s["started_at"], "timestamp"):
        mins = f"（{int((s['ended_at'] - s['started_at']).total_seconds() // 60)} 分）"
    lines = [f"**停止が終わりました** `{g('key')}`{mins}"]
    if g("remedy"):
        lines.append(f"- 手当て: {g('remedy')}")
    return "\n".join(lines)


def _body(cur) -> str:
    cur.execute("select key, category, started_at, what, waiting_for from stoppages where ended_at is null order by started_at")
    rows = cur.fetchall()
    head = "工場の番人（dispatcher）が操業停止の開始と終了をこの Issue にコメントします（ADR 0005）。閉じないでください。\n\n"
    if not rows:
        return head + "## 開いている停止\n\nなし"
    tbl = ["## 開いている停止", "", "| key | 分類 | 開始（UTC） | 何が止まったか | 待っているもの |", "|---|---|---|---|---|"]
    for r in rows:
        tbl.append(f"| `{r['key']}` | {r['category']} | {r['started_at']:%m-%d %H:%M} | {r['what']} | {r['waiting_for'] or ''} |")
    return head + "\n".join(tbl)


def notify(kind: str, s: dict, cur=None):
    try:
        if NOTIFIER == "github-issue" and STATUS_REPO and not ENV.get("WORKER_DRY_RUN"):
            n = _status_issue()
            _gh("POST", f"repos/{STATUS_REPO}/issues/{n}/comments", {"body": _text(kind, s)})
            if cur is not None:
                _gh("PATCH", f"repos/{STATUS_REPO}/issues/{n}", {"body": _body(cur)})
        else:
            _log(kind, s.get("key"), s.get("category"), s.get("what"))
    except Exception as e:  # noqa: BLE001  通知の失敗でラインを止めない
        _log("notify failed", kind, s.get("key"), repr(e))
