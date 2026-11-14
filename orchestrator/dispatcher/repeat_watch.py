"""繰り返すエラーを束ねて 1 件の停止にする（番人。factory-engine#308）。

2 つを見る。どちらも停止の分類は factory-bug、key は repeat: で始まり、受信トレイに建設者の件として出る。

1. 工場の中で握りつぶした例外（fault.py が events の factory.fault に書いた数）。同じ印（プロセス・文言・例外の型）が
   WINDOW_MIN 分に FAULT_MIN 回を超えたら `repeat:<印>#<始まり>` を開く。QUIET_MIN 分出なければ閉じる。
   11-08 の `notify report failed IndexError(2)` は 1 時間に約 55 回出ていたので、数分で 1 件になる
2. 同じ種類のジョブが同じ失敗で落ち続ける（job.finished の失敗。種類と error の組）。WINDOW_MIN 分に JOB_MIN 件で
   `repeat:job:<種類>:<error>#<始まり>` を開く。開いている間、同じ組のジョブ 1 件ずつの停止（job:<id>）は通知せずに開く
   （記録・操業レポートには残る）。11-10 の investigate 30 件（10 分間）は 30 通でなく 1 通になる

束ねた停止は「原因を調べて直す」ための件で、1 件ずつの停止を閉じるものではない（再実行・チケットの片付けで今までどおり閉じる）。
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Callable

import stoppage

ENV = os.environ
WINDOW_MIN = int(ENV.get("REPEAT_WINDOW_MIN", "60"))   # この分数の中で数える
FAULT_MIN = int(ENV.get("REPEAT_FAULT_MIN", "5"))      # 握りつぶした同じ例外がこの回数で束ねる（仮置き）
JOB_MIN = int(ENV.get("REPEAT_JOB_MIN", "3"))          # 同じ種類・同じ失敗のジョブがこの件数で束ねる（仮置き）
QUIET_MIN = int(ENV.get("REPEAT_QUIET_MIN", "60"))     # この分数出なければ束ねた停止を閉じる
PREFIX = "repeat:"
JOB_PREFIX = "repeat:job:"


def _stamp(t) -> str:
    if isinstance(t, (int, float)):
        t = datetime.fromtimestamp(t, timezone.utc)
    return t.strftime("%Y%m%dT%H%M")


def _clean(s: str) -> str:
    return re.sub(r"[\s#]+", "_", (s or "").strip())[:80] or "?"


def job_signature(kind: str | None, error: str | None) -> str:
    return f"{_clean(kind or 'implement')}:{_clean(error or '')}"


def _open_by_sig(cur, prefix: str) -> dict[str, str]:
    """開いている束ねた停止を、印 → key で返す（key は <prefix><印>#<始まり>）"""
    out = {}
    for k in stoppage.open_keys(cur, prefix):
        sig = k[len(prefix):].rsplit("#", 1)[0]
        out[sig] = k
    return out


def bundled_job_signatures(cur) -> set[str]:
    """束ねた停止が開いているジョブの印（job_signature）。この印のジョブ 1 件ずつの停止は通知しない"""
    return set(_open_by_sig(cur, JOB_PREFIX))


def job_step(cur, classify: Callable[[dict], tuple | None]) -> list[str]:
    """同じ種類・同じ失敗のジョブを束ねる。開いた key を返す。classify は dispatcher.classify_failure（自動で回復する失敗は None）"""
    cur.execute("select e.job_id, e.occurred_at, e.payload, j.kind, "
                "e.occurred_at > now() - make_interval(mins => %s) as in_window, "
                "e.occurred_at > now() - make_interval(mins => %s) as in_quiet "
                "from events e left join jobs j on j.id=e.job_id "
                "where e.name='job.finished' and e.payload->>'state'='failed' "
                "and e.occurred_at > now() - make_interval(mins => %s) order by e.id",
                (WINDOW_MIN, QUIET_MIN, max(WINDOW_MIN, QUIET_MIN)))
    groups: dict[str, dict] = {}
    for r in cur.fetchall():
        p = r["payload"] or {}
        if not classify(p):
            continue
        sig = job_signature(r["kind"], p.get("error"))
        g = groups.setdefault(sig, {"kind": r["kind"] or "implement", "error": p.get("error") or "", "jobs": [], "first": None, "recent": False})
        if r["in_window"]:
            g["jobs"].append(r["job_id"])
            g["first"] = g["first"] or r["occurred_at"]
        g["recent"] = g["recent"] or bool(r["in_quiet"])
    opened = []
    open_now = _open_by_sig(cur, JOB_PREFIX)
    for sig, g in groups.items():
        if sig in open_now or len(g["jobs"]) < JOB_MIN:
            continue
        key = f"{JOB_PREFIX}{sig}#{_stamp(g['first'])}"
        ids = ", ".join(str(j) for j in g["jobs"][:10]) + (" ほか" if len(g["jobs"]) > 10 else "")
        if stoppage.open_stoppage(cur, key, "factory-bug",
                                  f"同じ種類のジョブが同じ失敗を繰り返している（{g['kind']} が「{g['error']}」で、直近 {WINDOW_MIN} 分に {len(g['jobs'])} 件）",
                                  waiting_for="建設者が原因を調べて直す（同じ失敗のジョブ 1 件ずつの停止は、この件が開いている間は通知しない）",
                                  cause=f"job {ids}", started_at=g["first"], job_id=g["jobs"][-1]):
            opened.append(key)
    for sig, key in open_now.items():
        if not (groups.get(sig) or {}).get("recent"):
            stoppage.close_stoppage(cur, key, remedy=f"同じ失敗が {QUIET_MIN} 分出ていない")
    return opened


def fault_step(cur) -> list[str]:
    """工場の中で握りつぶした同じ例外を束ねる。開いた key を返す"""
    cur.execute("select payload->>'signature' as sig, "
                "coalesce(sum((payload->>'count')::int) filter (where occurred_at > now() - make_interval(mins => %s)), 0) as n, "
                "bool_or(occurred_at > now() - make_interval(mins => %s)) as recent, "
                "min((payload->>'first_at')::float) filter (where occurred_at > now() - make_interval(mins => %s)) as first_at, "
                "(array_agg(payload order by id desc))[1] as last "
                "from events where name='factory.fault' and occurred_at > now() - make_interval(mins => %s) group by 1",
                (WINDOW_MIN, QUIET_MIN, WINDOW_MIN, max(WINDOW_MIN, QUIET_MIN)))
    rows = {r["sig"]: r for r in cur.fetchall()}
    opened = []
    open_now = {s: k for s, k in _open_by_sig(cur, PREFIX).items() if not k.startswith(JOB_PREFIX)}
    for sig, r in rows.items():
        if sig in open_now or r["n"] < FAULT_MIN:
            continue
        last = r["last"] or {}
        key = f"{PREFIX}{sig}#{_stamp(r['first_at'] or datetime.now(timezone.utc))}"
        if stoppage.open_stoppage(cur, key, "factory-bug",
                                  f"工場の中で同じエラーが繰り返している（{last.get('component')} の「{last.get('label')}」が {last.get('exc')}、直近 {WINDOW_MIN} 分に {r['n']} 回）",
                                  waiting_for="建設者が原因を調べて直す（工場は止まらずに回っているが、この工程は動いていない）",
                                  cause=last.get("sample") or "",
                                  started_at=datetime.fromtimestamp(r["first_at"], timezone.utc) if r["first_at"] else None):
            opened.append(key)
    for sig, key in open_now.items():
        if not (rows.get(sig) or {}).get("recent"):
            stoppage.close_stoppage(cur, key, remedy=f"同じエラーが {QUIET_MIN} 分出ていない")
    return opened


def step(cur) -> list[str]:
    return fault_step(cur)
