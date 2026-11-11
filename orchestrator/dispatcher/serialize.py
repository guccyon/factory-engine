"""衝突を避ける並べ方（ADR 0027 の 2）と、同じ目的の仕事を同時に流さない（ADR 0044 の決定 8 の 4）。決定的（LLM を使わない）。

「衝突しやすいファイル」（Learn ループが付ける印。hot_marks.py）に触る見込みのタスクは、プランナーが着手を宣言していなければ、同じファイルに触る PR・ジョブが
流れている間は投入しない（順番に並べる）。supervisor の enqueue_step が投入の直前に呼ぶ。並べた根拠は events の plan.serialized に残し、
factoryctl plan show で見られる。

変わる見込みのファイルは、プランナーの判定（Plan の expected_files）と、チケットの本文に書かれたパスから取る。
流れている PR は GitHub の変更ファイル、PR のまだないジョブは同じく見込みのファイルを使う。
"""
from __future__ import annotations

import re
from datetime import datetime

from psycopg.types.json import Jsonb

import hot_marks

# 衝突しても機械的に解く・もう衝突しない作りにしたファイル（#148 の union と、ADR 0027 の 1 の断片）。並べる対象にしない。
# 製品が取りまとめに宣言したファイルは、hot_marks が印を外す（ADR 0051）
EXEMPT = hot_marks.EXEMPT
PATH_RE = re.compile(r"(?<![\w/.\-])((?:[\w.\-]+/)+[\w.\-]+\.[A-Za-z0-9]+|[\w\-]+\.(?:ya?ml|ts|tsx|dart|rs|json|sql|py|md))(?![\w/])")


def hot_files(cur, now: datetime | None = None) -> dict[str, dict]:
    """衝突しやすいファイル。{パス: {conflicts, changes, why}}。Learn ループが付け外しする印（hot_marks.py、ADR 0038 の追記 2026-11-11）。
    以前は直近 7 日の衝突の回数（3 回以上）と費用（$5 以上）で毎回数えていた"""
    return {f: {"conflicts": m.get("conflicts"), "changes": m.get("changes"), "why": m.get("why") or ""}
            for f, m in sorted(hot_marks.active(cur).items()) if f not in EXEMPT}


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
                              "why": f"{file} は衝突しやすいファイル（{stats.get('why') or '印'}）。"
                                     f"{behind} が同じファイルに触っているので、終わるまで投入しない"}),
                 f"plan.serialized:{corr}:{file}:{behind}"))
    return cur.fetchone() is not None


def serialized(cur, corr: str) -> list[dict]:
    cur.execute("select occurred_at, payload from events where name in ('plan.serialized','plan.duplicate_held') and correlation_id=%s order by id", (corr,))
    return [{"at": r["occurred_at"], **r["payload"]} for r in cur.fetchall()]


# ------------------------------------------------------------------ 同じ目的の仕事（ADR 0044 の決定 8 の 4）
# 2026-11-08 rally #1052（PR #1116）と #1122（PR #1192）は同じ「担保: K-0012 を ci_check で決定論にする」で、同時に流れて
# 同じ新規ファイルを作った。題が同じチケットが流れている間は、後のものを投入しない
TITLE_TAIL = re.compile(r"\s*\(#\d+\)\s*$")


def purpose(title: str) -> str:
    """比べる題。前後の空白と、PR の題の末尾に付く (#番号) を落とす"""
    return TITLE_TAIL.sub("", (title or "").strip()).strip()


def same_purpose(cur, repo: str, me: str, title: str) -> str | None:
    """同じ製品で、同じ目的（題が同じ）のチケットが流れていれば（台帳で active）、その correlation_id"""
    want = purpose(title)
    if not want:
        return None
    cur.execute("select source_ref, title from tickets where source='github' and repo=%s and state='active' order by id", (repo,))
    for t in cur.fetchall():
        corr = f"github:{t['source_ref']}"
        if corr != me and purpose(t["title"]) == want:
            return corr
    return None


def record_duplicate(cur, corr: str, title: str, behind: str) -> bool:
    cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values ('plan.duplicate_held',%s,%s,%s) "
                "on conflict (dedupe_key) do nothing returning id",
                (corr, Jsonb({"title": purpose(title), "behind": behind,
                              "why": f"同じ目的（{purpose(title)}）の {behind} が流れているので、終わるまで投入しない。"
                                     "重複なら起票元でどちらかを閉じる"}),
                 f"plan.duplicate_held:{corr}:{behind}"))
    return cur.fetchone() is not None
