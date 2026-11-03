"""衝突を避ける並べ方（ADR 0027 の 2）。決定的（LLM を使わない）。

「よく衝突するファイル」（工程の信号 conflict_fix の束。process_learn）に触る見込みのタスクは、同じファイルに触る PR・ジョブが
流れている間は投入しない（順番に並べる）。supervisor の enqueue_step が投入の直前に呼ぶ。並べた根拠は events の plan.serialized に残し、
factoryctl plan show で見られる。

変わる見込みのファイルは、プランナーの判定（Plan の expected_files）と、チケットの本文に書かれたパスから取る。
流れている PR は GitHub の変更ファイル、PR のまだないジョブは同じく見込みのファイルを使う。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

import process_learn

HOT_WINDOW_DAYS = 7
HOT_MIN_CONFLICTS = 3          # 7 日で 3 回以上衝突し
HOT_MIN_COST = 5.0             # 衝突の解消にエージェントの費用が $5 以上かかったファイル（機械的に解けたものは $0）
# 衝突しても機械的に解く・もう衝突しない作りにしたファイル（#148 の union と、ADR 0027 の 1 の断片）。並べる対象にしない
EXEMPT = {"CHANGELOG.md"}
PATH_RE = re.compile(r"(?<![\w/.\-])((?:[\w.\-]+/)+[\w.\-]+\.[A-Za-z0-9]+|[\w\-]+\.(?:ya?ml|ts|tsx|dart|rs|json|sql|py|md))(?![\w/])")


def hot_files(cur, now: datetime | None = None) -> dict[str, dict]:
    """よく衝突するファイル。{パス: {conflicts, cost}}"""
    now = now or datetime.now(timezone.utc)
    by: dict[str, dict] = {}
    for s in process_learn.conflict_fix(cur, now - timedelta(days=HOT_WINDOW_DAYS), now):
        if s["key"] in (process_learn.UNKNOWN, process_learn.NO_CONFLICT) or s["key"] in EXEMPT:
            continue
        g = by.setdefault(s["key"], {"conflicts": 0, "cost": 0.0})
        g["conflicts"] += 1
        g["cost"] += s["cost"]
    return {f: {"conflicts": g["conflicts"], "cost": round(g["cost"], 2)} for f, g in sorted(by.items())
            if g["conflicts"] >= HOT_MIN_CONFLICTS and g["cost"] >= HOT_MIN_COST}


def paths_in(text: str) -> set[str]:
    return {m.group(1) for m in PATH_RE.finditer(text or "")}


def expected_files(cur, corr: str, body: str | None) -> set[str]:
    """変わる見込みのファイル。最新の Plan の expected_files と、本文に書かれたパス"""
    cur.execute("select payload->'expected_files' as f from artifacts where type='Plan' and correlation_id=%s order by id desc limit 1", (corr,))
    r = cur.fetchone()
    return set((r or {}).get("f") or []) | paths_in(body or "")


def touches(files: set[str], hot: str) -> bool:
    """files のどれかが hot と同じファイルか。ファイル名だけの見込み（ディレクトリが分からない）は名前で比べる"""
    base = hot.rsplit("/", 1)[-1]
    return any(f == hot or ("/" not in f and f == base) or hot.endswith("/" + f) for f in files)


def blocker(expected: set[str], hot: dict[str, dict], inflight: dict[str, set[str]], me: str) -> tuple[str, str] | None:
    """expected が触るよく衝突するファイルに、ほかの流れているチケットも触っていれば (ファイル, そのチケット)"""
    for h in hot:
        if not touches(expected, h):
            continue
        for corr, files in sorted(inflight.items()):
            if corr != me and touches(files, h):
                return h, corr
    return None


def record(cur, corr: str, file: str, behind: str, stats: dict) -> bool:
    """並べた根拠を残す（チケット・ファイル・前のチケットごとに 1 回）"""
    cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values ('plan.serialized',%s,%s,%s) "
                "on conflict (dedupe_key) do nothing returning id",
                (corr, Jsonb({"file": file, "behind": behind, "hot": stats,
                              "why": f"{file} は直近 {HOT_WINDOW_DAYS} 日に {stats['conflicts']} 回衝突し、解消に ${stats['cost']:.2f} かかった。"
                                     f"{behind} が同じファイルに触っているので、終わるまで投入しない"}),
                 f"plan.serialized:{corr}:{file}:{behind}"))
    return cur.fetchone() is not None


def serialized(cur, corr: str) -> list[dict]:
    cur.execute("select occurred_at, payload from events where name='plan.serialized' and correlation_id=%s order by id", (corr,))
    return [{"at": r["occurred_at"], **r["payload"]} for r in cur.fetchall()]
