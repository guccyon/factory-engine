"""定期レポート（ADR 0032 の決定 9）。中身の型と、文面と、毎朝の組み立て・送信。

- 型: Report は Section の並び、Section は Item の並び。Item は数（Metric）・一覧（Entry）・文（Note）のどれか。
  値が取れなければ None（不明）として持つ（ADR 0008 の決定 8。0 で埋めない）
- 3 つに分ける: 組み立て（種類ごとの build(cur, start, end) -> Report。report_health.py など）、文面（render。種類を知らず型だけを見る）、
  届け方（notify.py の宛先と割り当て）
- 何を載せるか（節の一覧と載せてよい組織）は definitions/reports.yaml。今の中身は仮置き（status: provisional）
- 組み立てた Report は events の report.built（種類・期間・中身の JSON）に残す。後から何を載せたかを確かめられ、
  ダッシュボードも同じ JSON から描ける（任意の期間は build を期間を変えて呼ぶ）
- 送ったかどうかは宛先ごとに notify.sent。失敗した宛先は次の周回で送り直す（MAX_ATTEMPTS まで）。通知の失敗でラインを止めない
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

import notify

ENV = os.environ
TZ = ZoneInfo(ENV.get("REPORT_TZ", "Asia/Tokyo"))
REPORT_HOUR = int(ENV.get("DAILY_REPORT_HOUR", "5"))   # この時（REPORT_TZ）を過ぎたら前日の分を出す
MAX_ATTEMPTS = 3                                        # 1 つの宛先に送り直すのはここまで


@dataclass
class Item:
    kind: str                       # metric / entry / note
    label: str = ""
    value: float | int | str | None = None   # metric の値。None は不明
    unit: str = ""
    delta: float | int | None = None          # 前の期間との差（metric）
    state: str = ""                 # entry の状態
    url: str = ""                   # entry のリンク
    text: str = ""                  # note の本文


def metric(label: str, value, unit: str = "", delta=None) -> Item:
    return Item("metric", label, value, unit, delta)


def entry(label: str, state: str = "", url: str = "") -> Item:
    return Item("entry", label, state=state, url=url)


def note(text: str) -> Item:
    return Item("note", text=text)


@dataclass
class Section:
    id: str
    title: str
    parties: tuple = ("provider", "business_owner")
    items: list[Item] = field(default_factory=list)


@dataclass
class Report:
    kind: str
    title: str
    start: datetime
    end: datetime
    sections: list[Section] = field(default_factory=list)
    provisional: bool = True
    built_at: datetime | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("start", "end", "built_at"):
            d[k] = d[k].isoformat() if d[k] else None
        for s in d["sections"]:
            s["parties"] = list(s["parties"])
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Report:
        secs = [Section(s["id"], s["title"], tuple(s.get("parties") or ()), [Item(**i) for i in s.get("items") or []]) for s in d.get("sections") or []]
        at = lambda v: datetime.fromisoformat(v) if v else None   # noqa: E731
        return cls(d["kind"], d["title"], at(d["start"]), at(d["end"]), secs, bool(d.get("provisional", True)), at(d.get("built_at")))


# ------------------------------------------------------------------ 文面
def _num(v, unit: str) -> str:
    if v is None:
        return "不明"
    if isinstance(v, float):
        v = round(v, 1) if abs(v) < 100 else round(v)
    return f"{v}{unit}" if unit in ("%", "件", "分", "時間", "回") else (f"{v} {unit}" if unit else f"{v}")


def _delta(d, unit: str) -> str:
    if d is None or d == 0:
        return ""
    if isinstance(d, float):
        d = round(d, 1) if abs(d) < 100 else round(d)
    return f"（前日比 {'+' if d > 0 else ''}{d}{unit if unit in ('%', '件', '分', '時間', '回') else ''}）"


def visible(report: Report, party: str) -> list[Section]:
    """その組織の宛先に出してよい節だけ（決定 3）"""
    return [s for s in report.sections if party in s.parties]


def render(report: Report, via: str, party: str = "provider", factory: str = "") -> str:
    slack = via in ("slack_webhook", "slack_app")
    b = (lambda t: f"*{t}*") if slack else (lambda t: f"**{t}**")   # noqa: E731
    period = report.start.astimezone(TZ).strftime("%m/%d") if report.end - report.start <= timedelta(days=1) else \
        f"{report.start.astimezone(TZ):%m/%d}〜{(report.end - timedelta(seconds=1)).astimezone(TZ):%m/%d}"
    head = f"{f'[{factory}] ' if factory and party == 'provider' else ''}{report.title}（{period}）"
    lines = [b(head)]
    if report.provisional:
        lines.append("中身は仮置きです（決まりしだい変わります）")
    for s in visible(report, party):
        lines.append("")
        lines.append(b(s.title))
        for i in s.items:
            if i.kind == "metric":
                lines.append(f"• {i.label}: {_num(i.value, i.unit)}{_delta(i.delta, i.unit)}")
            elif i.kind == "entry":
                link = i.url if i.url.startswith("https://") else ""
                label = (f"<{link}|{i.label}>" if slack else f"[{i.label}]({link})") if link else i.label
                lines.append(f"• {label}" + (f" — {i.state}" if i.state else ""))
            else:
                lines.append(i.text)
    return "\n".join(lines)


# ------------------------------------------------------------------ 毎朝の組み立てと送信
def previous_day(now: datetime) -> tuple[datetime, datetime]:
    """REPORT_TZ の前日の 0〜24 時"""
    local = now.astimezone(TZ)
    end = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return end - timedelta(days=1), end


def _with_delta(cur, rep: Report) -> Report:
    """前の期間の report.built があれば、数の前日比を付ける"""
    span = rep.end - rep.start
    cur.execute("select payload from events where name='report.built' and payload->>'kind'=%s and payload->>'start'=%s order by id desc limit 1",
                (rep.kind, (rep.start - span).isoformat()))
    r = cur.fetchone()
    if not r:
        return rep
    prev = {(s["id"], i["label"]): i.get("value") for s in r["payload"]["report"]["sections"] for i in s["items"] if i["kind"] == "metric"}
    for s in rep.sections:
        for i in s.items:
            p = prev.get((s.id, i.label))
            if i.kind == "metric" and isinstance(i.value, (int, float)) and isinstance(p, (int, float)):
                i.delta = round(i.value - p, 3)
    return rep


def built(cur, kind: str, start: datetime) -> Report | None:
    cur.execute("select payload from events where name='report.built' and payload->>'kind'=%s and payload->>'start'=%s order by id desc limit 1",
                (kind, start.isoformat()))
    r = cur.fetchone()
    return Report.from_dict(r["payload"]["report"]) if r else None


def build_and_record(cur, kind: str, builder, start: datetime, end: datetime) -> Report:
    rep = _with_delta(cur, builder(cur, start, end))
    rep.built_at = datetime.now(timezone.utc)
    cur.execute("insert into events(name, payload, dedupe_key) values ('report.built', %s, %s) on conflict (dedupe_key) do nothing",
                (Jsonb({"kind": kind, "start": start.isoformat(), "end": end.isoformat(), "provisional": rep.provisional, "report": rep.to_dict()}),
                 f"report.built:{kind}:{start.isoformat()}"))
    return built(cur, kind, start) or rep


def _attempts(cur, kind: str, start: datetime) -> dict[str, dict]:
    cur.execute("select payload from events where name='notify.sent' and payload->>'kind'=%s and payload->>'start'=%s", (kind, start.isoformat()))
    out: dict[str, dict] = {}
    for r in cur.fetchall():
        s = out.setdefault(r["payload"]["destination"], {"ok": False, "tries": 0})
        s["tries"] += 1
        s["ok"] = s["ok"] or bool(r["payload"].get("ok"))
    return out


def deliver(cur, rep: Report, config: notify.Config, now: datetime, poster=None) -> dict:
    """その種類の宛先に送る。送れた宛先・夜で待った宛先・あきらめた宛先を返す"""
    poster = poster or notify.post
    done = _attempts(cur, rep.kind, rep.start)
    out = {"sent": [], "quiet": [], "failed": [], "gave_up": []}
    for d in notify.targets(config, rep.kind):
        a = done.get(d.name, {"ok": False, "tries": 0})
        if a["ok"]:
            continue
        if a["tries"] >= MAX_ATTEMPTS:
            out["gave_up"].append(d.name)
            continue
        if notify.quiet(now, d.quiet):
            out["quiet"].append(d.name)
            continue
        if not visible(rep, d.party):
            continue
        try:
            ok = bool(poster(d, render(rep, d.via, d.party, ENV.get("FACTORY_NAME", ""))))
        except Exception as e:  # noqa: BLE001  通知の失敗でラインを止めない
            notify._log("report send failed", rep.kind, d.name, repr(e)[:200])
            ok = False
        cur.execute("insert into events(name, payload) values ('notify.sent', %s)",
                    (Jsonb({"kind": rep.kind, "start": rep.start.isoformat(), "destination": d.name, "party": d.party, "via": d.via, "ok": ok}),))
        (out["sent"] if ok else out["failed"]).append(d.name)
    return out


def daily_step(cur, now: datetime | None = None, config: notify.Config | None = None, builders: dict | None = None, poster=None) -> dict:
    """supervisor の周回から呼ぶ。REPORT_HOUR を過ぎたら前日の分を種類ごとに 1 回組み立てて残し、宛先に送る"""
    now = now or datetime.now(timezone.utc)
    if now.astimezone(TZ).hour < REPORT_HOUR:
        return {}
    if builders is None:
        import report_health
        import report_product
        builders = {"report.factory_health": report_health.build, "report.product": report_product.build}
    config = config or notify.current(cur)
    start, end = previous_day(now)
    out = {}
    for kind, builder in builders.items():
        if notify.KINDS.get(kind, {}).get("status") != "ready":
            continue
        rep = built(cur, kind, start) or build_and_record(cur, kind, builder, start, end)
        out[kind] = deliver(cur, rep, config, now, poster)
    return out
