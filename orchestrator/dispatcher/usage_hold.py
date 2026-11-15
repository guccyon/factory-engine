"""契約の使用量の上限に当たったら、戻るまで新しいジョブを取り出さない（2026-11-15）。

11/15 07:00〜07:24 JST に、アカウントの使用量の上限に当たったまま、ジョブ 15 本（QA 13・plan 1・inspect 1）を次々に取り出して
どれも数秒で rc=1 で落とし、factory-bug の停止を 15 件開いた。上限は待つほか戻らないので、取り出しを止めるのが正しい手当て。

- dispatcher がジョブの終わりで上限を見分けたら（agent_runner が出す `[agent_runner] usage limit: …` の行）、usage.limit_hit を残す
- 上限の文に戻る時刻（「resets 11am (Asia/Tokyo)」「…|1794708000」）があればその時刻まで、なければ USAGE_HOLD_MIN 分、
  _claim_loop が新しいジョブを取り出さない。止める間を過ぎたら 1 本だけ流してみる（probing。また当たれば止め直す。PROBE_MIN 分動くか終われば全部の取り出しを戻す）
- 止めている間は停止 usage:<時刻>（budget）を 1 件だけ開いて知らせ、戻ったら閉じる。上限で落ちたジョブ 1 件ずつの停止は通知しない
- `factoryctl usage release` で人が早めに戻せる（usage.limit_released）
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

ENV = os.environ
HOLD_MIN = int(ENV.get("USAGE_HOLD_MIN", "20"))          # 戻る時刻が読めないときに止める分数
HOLD_MAX_H = int(ENV.get("USAGE_HOLD_MAX_H", "170"))     # 読めた時刻がこれより先なら読み違いとみて HOLD_MIN にする（週の上限は 7 日 = 168 時間）
PROBE_MIN = int(ENV.get("USAGE_PROBE_MIN", "10"))     # 止めが明けて最初の 1 本がこの分数動いていれば、上限は戻ったとみる
MARGIN_MIN = 2                                           # 戻る時刻ちょうどは外して、少し後から流す
STOPPAGE_PREFIX = "usage:"
MARK = "[agent_runner] usage limit:"

_EPOCH = re.compile(r"\|\s*(\d{10})\b")
_MONTHS = {m: i + 1 for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}
_RESETS = re.compile(r"resets\s+(?:on\s+)?(?:(?P<mon>[A-Za-z]{3})[a-z]*\.?\s+(?P<day>\d{1,2}),?\s+)?(?:at\s+)?"
                     r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm)?\s*(?:\((?P<tz>[^)]+)\))?", re.I)


def limit_text(lines: list[str]) -> str | None:
    """ジョブのログから上限の文を拾う（agent_runner の印の行）。なければ None"""
    for l in reversed(lines):
        i = l.find(MARK)
        if i >= 0:
            return l[i + len(MARK):].strip()[:300]
    return None


def resets_at(text: str, now: datetime) -> datetime | None:
    """上限の文から戻る時刻を読む。読めなければ None"""
    if not text:
        return None
    m = _EPOCH.search(text)
    if m:
        return datetime.fromtimestamp(int(m.group(1)), timezone.utc)
    m = _RESETS.search(text)
    if not m:
        return None
    tz = timezone.utc
    if m.group("tz"):
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(m.group("tz").strip())
        except Exception:  # noqa: BLE001  読めない地域名は UTC とみる
            tz = timezone.utc
    h, mi, ap = int(m.group("h")), int(m.group("m") or 0), (m.group("ap") or "").lower()
    if ap == "pm" and h < 12:
        h += 12
    elif ap == "am" and h == 12:
        h = 0
    if h > 23 or mi > 59:
        return None
    local = now.astimezone(tz)
    if m.group("mon") and m.group("mon").lower()[:3] in _MONTHS:
        try:
            at = local.replace(month=_MONTHS[m.group("mon").lower()[:3]], day=int(m.group("day")), hour=h, minute=mi, second=0, microsecond=0)
        except ValueError:
            return None
        if at < local - timedelta(days=1):
            at = at.replace(year=at.year + 1)
    else:
        at = local.replace(hour=h, minute=mi, second=0, microsecond=0)
        if at <= local:
            at += timedelta(days=1)
    return at.astimezone(timezone.utc)


def hold_until(text: str, now: datetime) -> tuple[datetime, bool]:
    """止める終わりの時刻と、それが上限の文から読めたか"""
    at = resets_at(text, now)
    if at and now < at <= now + timedelta(hours=HOLD_MAX_H):
        return at + timedelta(minutes=MARGIN_MIN), True
    return now + timedelta(minutes=HOLD_MIN), False


def record_hit(cur, job_id: int | None, corr: str | None, text: str, now: datetime | None = None) -> datetime:
    """上限に当たったことを残す。止める終わりの時刻を返す"""
    now = now or datetime.now(timezone.utc)
    until, parsed = hold_until(text or "", now)
    cur.execute("insert into events(name, correlation_id, job_id, payload) values ('usage.limit_hit',%s,%s,%s)",
                (corr, job_id, Jsonb({"text": text or "", "until": until.isoformat(), "parsed": parsed})))
    return until


def release(cur, by: str = "factoryctl") -> None:
    cur.execute("insert into events(name, payload) values ('usage.limit_released',%s)", (Jsonb({"by": by}),))


def held(cur, now: datetime | None = None) -> dict | None:
    """いま取り出しを止めているか。止めていれば {until, since（最後に当たった時刻）, text, parsed}、なければ None"""
    now = now or datetime.now(timezone.utc)
    cur.execute("select occurred_at, payload from events where name='usage.limit_hit' "
                "and occurred_at > now() - make_interval(hours => %s) "
                "and occurred_at > coalesce((select max(occurred_at) from events where name='usage.limit_released'), '-infinity') "
                "order by id desc limit 1", (HOLD_MAX_H + 1,))
    r = cur.fetchone()
    if not r:
        return None
    try:
        until = datetime.fromisoformat((r["payload"] or {}).get("until") or "")
    except ValueError:
        return None
    if now >= until:
        return None
    return {"until": until, "since": r["occurred_at"], "text": (r["payload"] or {}).get("text") or "", "parsed": bool((r["payload"] or {}).get("parsed"))}


def probing(cur, now: datetime | None = None) -> bool:
    """止めが明けた直後、最初に流したジョブがまだ確かめ中か（PROBE_MIN 分まで）。確かめ中は 2 本目以降を取り出さない。
    上限なら数秒で落ちるので、それを過ぎて動いていれば上限は戻っている"""
    now = now or datetime.now(timezone.utc)
    cur.execute("select payload->>'until' as until from events where name='usage.limit_hit' "
                "and occurred_at > now() - make_interval(hours => %s) "
                "and occurred_at > coalesce((select max(occurred_at) from events where name='usage.limit_released'), '-infinity') "
                "order by id desc limit 1", (HOLD_MAX_H + 1,))
    r = cur.fetchone()
    if not r or not r["until"]:
        return False
    try:
        until = datetime.fromisoformat(r["until"])
    except ValueError:
        return False
    if not (until <= now < until + timedelta(minutes=PROBE_MIN)):
        return False
    cur.execute("select exists(select 1 from jobs where started_at >= %s and finished_at is null) as running, "
                "exists(select 1 from jobs where finished_at >= %s and started_at >= %s) as finished", (until, until, until))
    x = cur.fetchone()
    return bool(x["running"]) and not x["finished"]


def watch(cur, stoppage, now: datetime | None = None) -> None:
    """番人の周回から。止めている間は停止 usage:<始まり> を 1 件だけ開き、戻ったら閉じる"""
    h = held(cur, now)
    open_keys = stoppage.open_keys(cur, STOPPAGE_PREFIX)
    if h:
        if open_keys:
            return   # 止め直し（1 本流してまた当たった）でも、開いている 1 件のまま
        jst = h["until"].astimezone(timezone(timedelta(hours=9)))
        stoppage.open_stoppage(cur, f"{STOPPAGE_PREFIX}{h['since']:%Y%m%dT%H%M%S}", "budget",
                               f"契約の使用量の上限に当たったので、新しいジョブの取り出しを止めている（{jst:%m/%d %H:%M} JST まで"
                               + ("" if h["parsed"] else "の仮の止め。過ぎたら 1 本だけ流して確かめる") + "）",
                               waiting_for="使用量の上限のリセット（工場が自分で再開する。早めに戻すなら factoryctl usage release）",
                               cause=h["text"][:200], started_at=h["since"])
    else:
        for k in open_keys:
            stoppage.close_stoppage(cur, k, remedy="使用量の上限の止めが明けた（取り出しを再開した）")
