"""事例型の分析役（ADR 0023）: 差し戻しと停止を 1 件ずつ、観点（仕様・伝達・検査・工程）ごとになぜなぜで掘る。

- supervisor が 1 日に数件、分析する出来事を選び（pick_incidents）、制御プレーンの記録だけで入力を組み立てて（build_input）、ジョブ（kind=case_review）を積む
- dispatcher はジョブを起こすときに、後片付け（ADR 0021）のアーカイブからセッションの抜粋をクレンジングして入力に足す（attach_archive）
- 結果は events の case.analyzed と artifacts の CaseAnalysis に残すだけ（record）。作用先にもワーカーのプロンプトにも入れない。
  知見の置き場と committee は設計中（ADR 0023 の「まだ決めていないこと」）
"""
from __future__ import annotations

import io
import json
import re
import tarfile
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

LOOKBACK_DAYS = 14             # これより前の出来事は選ばない
STOPPAGE_MIN_MINUTES = 60      # 分析する停止の長さの下限（definitions/learning_taxonomy.yaml の case_review.incidents）
SKIP_STOPPAGE_PREFIXES = ("budget:", "drain:")   # 予算の枠の使い切りと、人の一時停止は分析しない
TIMELINE_MAX = 80              # 入力に入れる出来事の数の上限
SESSION_MAX_CHARS = 120_000    # セッションの抜粋の上限
FILE_MAX_CHARS = 20_000        # アーカイブの 1 ファイルの上限
HANDOFF_FILES = ("summary.md", "provisional.md", "review-defects.md", "signal.json", "diffstat.txt", "worker.err")

# 秘密情報に見える文字列は伏せる（トークン、鍵、接続文字列のパスワード）
SECRET_RES = [re.compile(p) for p in (
    r"gh[pousr]_[A-Za-z0-9]{20,}", r"github_pat_[A-Za-z0-9_]{20,}", r"sk-ant-[A-Za-z0-9_\-]{20,}", r"sk-[A-Za-z0-9]{32,}",
    r"AKIA[0-9A-Z]{16}", r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
    r"(?i)(password|passwd|secret|token|api[_-]?key)\s*[=:]\s*\S+", r"://[^/\s:@]+:[^/\s@]+@")]


def mask(text: str) -> str:
    for r in SECRET_RES:
        text = r.sub("[伏せた]", text)
    return text


# ------------------------------------------------------------------ 出来事を選ぶ
def pick_incidents(cur, now: datetime | None = None, limit: int = 3) -> list[dict]:
    """まだ分析していない出来事を新しい順に選ぶ。差し戻し（会話の記録の保存の後）と、閉じた長い停止を交互に"""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=LOOKBACK_DAYS)
    cur.execute("select payload->>'incident' as k from events where name='case.enqueued'")
    done = {r["k"] for r in cur.fetchall()}
    # 差し戻し: judge が検査の指摘か仮決めの差し替えを採った PR。そのチケットの引き継ぎがアーカイブされていること
    cur.execute("""select a.payload->>'pr_url' as pr_url, a.correlation_id as corr, count(*) as n, max(a.created_at) as at
                     from artifacts a
                    where a.type='Judgment' and a.payload->>'stage'='judged' and a.payload->>'verdict' in ('adopt','replace')
                      and a.created_at >= %s and a.payload->>'pr_url' is not null
                      and exists (select 1 from events h where h.name='handoff.archived' and h.correlation_id=a.correlation_id)
                    group by 1, 2 order by max(a.created_at) desc""", (since,))
    sendbacks = [{"key": f"sendback:{r['pr_url']}", "kind": "sendback", "corr": r["corr"], "pr_url": r["pr_url"], "adopted": r["n"]}
                 for r in cur.fetchall() if f"sendback:{r['pr_url']}" not in done]
    cur.execute("""select key, category, what, correlation_id as corr, started_at, ended_at from stoppages
                    where ended_at is not null and ended_at >= %s and ended_at - started_at >= make_interval(mins => %s)
                    order by ended_at desc""", (since, STOPPAGE_MIN_MINUTES))
    stops = [{"key": f"stoppage:{r['key']}", "kind": "stoppage", "corr": r["corr"], "stoppage_key": r["key"], "category": r["category"]}
             for r in cur.fetchall() if not r["key"].startswith(SKIP_STOPPAGE_PREFIXES) and f"stoppage:{r['key']}" not in done]
    out = []
    while len(out) < limit and (sendbacks or stops):
        for q in (sendbacks, stops):
            if q and len(out) < limit:
                out.append(q.pop(0))
    return out


# ------------------------------------------------------------------ 入力（制御プレーンの記録だけ）
def _short(p: dict, keep: int = 400) -> dict:
    s = json.dumps(p or {}, ensure_ascii=False, default=str)
    return json.loads(s) if len(s) <= keep else {"_truncated": mask(s[:keep])}


def build_input(cur, inc: dict) -> dict:
    inp: dict = {"incident": {k: v for k, v in inc.items() if k != "corr"}, "correlation_id": inc.get("corr")}
    if inc["kind"] == "stoppage":
        cur.execute("select key, category, what, waiting_for, cause, remedy, prevention, detected_by, started_at, ended_at, job_id "
                    "from stoppages where key=%s", (inc["stoppage_key"],))
        r = cur.fetchone() or {}
        inp["stoppage"] = {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items()}
        around = (r.get("started_at"), r.get("ended_at"))
    else:
        cur.execute("""select payload from artifacts where type='Judgment' and payload->>'pr_url'=%s and payload->>'stage'='judged' order by id""",
                    (inc["pr_url"],))
        inp["judgments"] = [_short({k: j["payload"].get(k) for k in ("category", "verdict", "defect", "decision", "interpretation", "mode")}, 1500)
                            for j in cur.fetchall()]
        around = (None, None)
    if inc.get("corr"):   # そのチケットの出来事の流れ（名前・時刻・中身の抜粋）
        cur.execute("select name, occurred_at, job_id, payload from events where correlation_id=%s order by id", (inc["corr"],))
        rows = cur.fetchall()
    elif around[0]:       # チケットのない停止（ラインが空など）: その前後の工場の出来事
        cur.execute("select name, occurred_at, job_id, payload from events where occurred_at between %s and %s "
                    "and name not in ('supervisor.heartbeat') order by id", (around[0] - timedelta(minutes=30), around[1]))
        rows = cur.fetchall()
    else:
        rows = []
    inp["timeline"] = [{"name": e["name"], "at": e["occurred_at"].isoformat(), "job_id": e["job_id"], "payload": _short(e["payload"])}
                       for e in rows[-TIMELINE_MAX:]]
    return inp


# ------------------------------------------------------------------ セッションの抜粋（後片付けのアーカイブから）
def latest_archive_key(cur, corr: str | None) -> str | None:
    if not corr:
        return None
    cur.execute("select payload->>'blob_key' as k from events where name='handoff.archived' and correlation_id=%s order by id desc limit 1", (corr,))
    r = cur.fetchone()
    return r["k"] if r else None


def cleanse_session(jsonl: bytes, cap: int = SESSION_MAX_CHARS) -> str:
    """エージェントの会話の記録（Claude Code の jsonl）から、エージェントの文と使ったツールの名前・引数の要約だけを残す。
    ツールの出力（ファイルの中身・コマンドの出力）は落とす。秘密情報に見える文字列は伏せる"""
    out: list[str] = []
    for line in jsonl.decode("utf-8", "replace").splitlines():
        try:
            m = json.loads(line)
        except ValueError:
            continue
        msg = m.get("message") or {}
        if m.get("type") != "assistant" or not isinstance(msg.get("content"), list):
            continue
        for c in msg["content"]:
            if c.get("type") == "text" and c.get("text", "").strip():
                out.append(c["text"].strip())
            elif c.get("type") == "tool_use":
                arg = c.get("input") or {}
                brief = arg.get("command") or arg.get("file_path") or arg.get("pattern") or arg.get("description") or ""
                out.append(f"[{c.get('name')}] {str(brief)[:200]}")
    text = mask("\n".join(out))
    return text if len(text) <= cap else text[: cap // 3] + "\n…（中略）…\n" + text[-(cap * 2 // 3):]


def attach_archive(inp: dict, tar_gz: bytes) -> dict:
    """アーカイブ（ボリュームの tar.gz）から、ジョブごとの報告・仮決め・指摘と、会話の記録の抜粋を入力に足す"""
    files: dict[str, str] = {}
    sessions: list[str] = []
    with tarfile.open(fileobj=io.BytesIO(tar_gz), mode="r:gz") as tf:
        for m in tf.getmembers():
            if not m.isfile():
                continue
            name = m.name.lstrip("./")
            base = name.rsplit("/", 1)[-1]
            data = tf.extractfile(m)
            if data is None:
                continue
            if name.startswith("jobs/") and base in HANDOFF_FILES:
                files[name] = mask(data.read(FILE_MAX_CHARS * 4).decode("utf-8", "replace")[:FILE_MAX_CHARS])
            elif base == "ticket.json":
                files[name] = mask(data.read(FILE_MAX_CHARS * 4).decode("utf-8", "replace")[:FILE_MAX_CHARS])
            elif name.startswith("agent/") and base.endswith(".jsonl"):
                sessions.append(cleanse_session(data.read()))
    inp["handoff_files"] = files
    inp["session_excerpt"] = "\n\n---\n\n".join(sessions)[:SESSION_MAX_CHARS]
    return inp


# ------------------------------------------------------------------ 記録
def record(cur, corr: str, job_id: int, inp: dict, result: dict) -> bool:
    """分析の結果を残す。同じ出来事は 1 度だけ"""
    rep = (result or {}).get("report") or {}
    inc = inp.get("incident") or {}
    payload = {"incident": inc.get("key"), "kind": inc.get("kind"), "ticket": inp.get("correlation_id"),
               "summary": rep.get("summary", ""), "viewpoints": rep.get("viewpoints", []), "root_causes": rep.get("root_causes", []),
               "learnings": rep.get("learnings", []), "had_session": bool(inp.get("session_excerpt"))}
    cur.execute("insert into events(name, correlation_id, job_id, payload, dedupe_key) values ('case.analyzed',%s,%s,%s,%s) "
                "on conflict (dedupe_key) do nothing returning id", (corr, job_id, Jsonb(payload), f"case.analyzed:{inc.get('key')}"))
    if not cur.fetchone():
        return False
    cur.execute("insert into artifacts(type, correlation_id, job_id, produced_by, payload) values ('CaseAnalysis',%s,%s,'case_review',%s)",
                (corr, job_id, Jsonb(payload)))
    return True
