"""プランナーの判定と結果の突き合わせ（ADR 0036 の 4a）。決定的（LLM を使わない）。

  工場の PR がマージされた・閉じた（supervisor の pr.merged / pr.closed_unmerged）
  → ここで最新の Plan と実績を突き合わせ、plan.outcome を 1 件残す（dedupe は plan.outcome:<pr_url>）
     実績: 実装のジョブの数・ターン（衝突の解消の続きを除く）・費用・途中終了・続きの理由、PR で変わったファイルと層、
           見込みのファイル（Plan の expected_files）の再現率と適合率、経路の覆り、judge が人に回したか
     判定: undersized（実績が切る基準を超えた・途中終了した）/ misrouted（経路が覆った）/ oversized（切った子が小さすぎた）/ ok
  → 実績の表（製品ごと・層の組ごとのファイル数とターン。プランナーの見積もりの材料。順番 2 で入力に渡す）
  → 合格線（ADR 0036 の 4c の実績の 4 つ）

プランナーを通らなかったチケット（建設者の手書きなど）も planned=false で同じように残す（切るべきだったかの出発点になる）。
定義は definitions/planner.yaml の sizing（dispatcher のコンテナには definitions/ がないので、使う値をここに写す。食い違いはテストで見る）。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from statistics import median

from psycopg.types.json import Jsonb

from globmatch import glob_re

LAYERS = [("db", ["**/migrations/**"]),
          ("contract", ["specs/contracts/**", "**/src/api/schema.ts"]),
          ("docs", ["specs/**", "docs/**", "changelog.d/**", "**/*.md"]),
          ("backend", ["apps/backend/**", "backend/**", "server/**"]),
          ("web", ["apps/*-web/**", "web/**"]),
          ("mobile", ["apps/*-app/**", "mobile/**"])]
UNCOUNTED_LAYERS = ("docs", "other")
FOLLOW_GLOBS = ["**/tests/**", "**/test/**", "**/screenshots/**", "**/*.test.*", "**/*_test.*"]
SPLIT = {"min_layers": 3, "max_files": 30, "max_turns": 60}
MIN_TURNS = 10
BANDS = {"S": 30, "M": 60, "L": 90}
PASS_LINES = {"window_days": 14, "min_tickets": 10, "incomplete_rate_max": 0.10, "turns_median_max": 60, "turns_p90_max": 120,
              "recall_median_min": 0.6, "misrouted_rate_max": 0.10}
STEP_LIMIT = 5          # 1 周回で突き合わせる PR の数（GitHub の呼び出しを抑える）
STEP_DAYS = 30          # この日数より前に閉じた PR は周回では拾わない（過去の分は factoryctl plan outcomes --backfill）
MAX_FILE_PAGES = 30     # PR のファイルの一覧は 100 件ずつ。GitHub の上限 3000 件まで
PR_URL = re.compile(r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)")

_COMPILED = [(layer, [glob_re(g) for g in globs]) for layer, globs in LAYERS]
_FOLLOW = [glob_re(g) for g in FOLLOW_GLOBS]


# ------------------------------------------------------------------ 決定的な部品
def layer_of(path: str) -> str:
    for layer, pats in _COMPILED:
        if any(p.match(path) for p in pats):
            return layer
    return "other"


def is_follow(path: str) -> bool:
    return any(p.match(path) for p in _FOLLOW)


def layers_of(files: list[str]) -> list[str]:
    """変わったファイルの層（重なりなし、LAYERS の順）。追従のファイル（試験・撮影の値）しか変わらない層は other に寄せる"""
    own = {layer_of(f) for f in files if not is_follow(f)}
    seen = {l if l in own else "other" for l in (layer_of(f) for f in files)}
    return [l for l, _ in LAYERS if l in seen] + (["other"] if "other" in seen else [])


def counted(layers: list[str]) -> list[str]:
    return [l for l in layers if l not in UNCOUNTED_LAYERS]


def layers_key(layers: list[str]) -> str:
    """実績の表の鍵（数える層を + でつなぐ。数える層がなければ docs）"""
    return "+".join(counted(layers)) or "docs"


def band(turns: int | float | None) -> str | None:
    if turns is None:
        return None
    for name, top in BANDS.items():
        if turns <= top:
            return name
    return "XL"


def over_split(layers: list[str], files: int, turns: int | float) -> list[str]:
    """切る基準（ADR 0036 の 2）のうち、当たったもの"""
    hit = []
    if len(counted(layers)) >= SPLIT["min_layers"]:
        hit.append("layers")
    if files > SPLIT["max_files"]:
        hit.append("files")
    if turns > SPLIT["max_turns"]:
        hit.append("turns")
    return hit


def _matches(expected: str, actual: str) -> bool:
    e = expected.strip().lstrip("./")
    return bool(e) and (e == actual or ("/" not in e and PurePosixPath(actual).name == e))


def expected_scores(expected: list[str], actual: list[str]) -> tuple[float | None, float | None]:
    """(再現率: 変わったファイルのうち見込みにあった割合, 適合率: 見込みのうち実際に変わった割合)。見込みも実績もなければ None"""
    expected = [e for e in expected or [] if isinstance(e, str) and e.strip()]
    if not expected or not actual:
        return None, None
    hit_actual = sum(1 for a in actual if any(_matches(e, a) for e in expected))
    hit_expected = sum(1 for e in expected if any(_matches(e, a) for a in actual))
    return round(hit_actual / len(actual), 3), round(hit_expected / len(expected), 3)


def judge(o: dict) -> tuple[str, list[str]]:
    """判定と、その理由。undersized を先に見る（費用に一番効く外れ）"""
    reasons = []
    if o["incomplete"]:
        reasons.append("incomplete")
    reasons += over_split(o["layers"], o["files"], o["turns"])
    if o["jobs"] and reasons:
        return "undersized", reasons
    if o["route_changed"]:
        return "misrouted", ["route_changed"]
    if o["split_child"] and o["jobs"] and o["turns"] < MIN_TURNS:
        return "oversized", ["small_child"]
    return "ok", []


def percentile(values: list[float], q: float) -> float | None:
    """最近接順位法の分位点（q は 0〜1）"""
    if not values:
        return None
    v = sorted(values)
    k = max(0, min(len(v) - 1, int(-(-q * len(v) // 1)) - 1))
    return v[k]


# ------------------------------------------------------------------ 記録から集める
def _plans(cur, corr: str) -> list[dict]:
    cur.execute("select id, payload from artifacts where type='Plan' and correlation_id=%s order by id", (corr,))
    return [{"id": r["id"], **r["payload"]} for r in cur.fetchall()]


def _jobs(cur, corr: str) -> list[dict]:
    """実装役のジョブ（衝突の解消の続きを除く）"""
    cur.execute("""select j.id, j.result from jobs j where j.correlation_id=%s and j.kind='implement' and j.state in ('succeeded','failed')
                     and not exists (select 1 from events c where c.name='continue.enqueued' and c.job_id=j.id and c.payload->>'reason'='conflict')
                   order by j.id""", (corr,))
    return cur.fetchall()


def _continues(cur, corr: str) -> dict[str, int]:
    cur.execute("select payload->>'reason' as r, count(*) as n from events where name='continue.enqueued' and correlation_id=%s group by 1", (corr,))
    return {r["r"] or "?": int(r["n"]) for r in cur.fetchall()}


def _split_child(cur, repo: str, issue: int) -> bool:
    """プランナーか窓口が切ったチケットか（切りすぎを数える対象）"""
    cur.execute("select 1 from events where name='plan.split' and payload->>'repo'=%s and (payload->>'child'=%s or payload->'children' @> %s::jsonb) limit 1",
                (repo, str(issue), f"[{int(issue)}]"))
    if cur.fetchone():
        return True
    cur.execute("select 1 from tickets where source='github' and source_ref=%s and desk_conversation_id is not null", (f"{repo}#{issue}",))
    return cur.fetchone() is not None


def pr_files(gh, repo: str, number: int) -> list[str]:
    files: list[str] = []
    for page in range(1, MAX_FILE_PAGES + 1):
        batch = gh("GET", f"repos/{repo}/pulls/{number}/files?per_page=100&page={page}") or []
        files += [f["filename"] for f in batch if isinstance(f, dict) and f.get("filename")]
        if len(batch) < 100:
            break
    return files


def build(cur, corr: str, pr_url: str, merged: bool, files: list[str], labels: list[str]) -> dict:
    """1 本の PR の突き合わせ（GitHub を呼ばない。files と labels は呼ぶ側が渡す）"""
    m = PR_URL.match(pr_url or "")
    repo, _, issue = corr.removeprefix("github:").partition("#")
    plans = _plans(cur, corr)
    ready = [p for p in plans if p.get("decision") == "ready"]
    last = ready[-1] if ready else (plans[-1] if plans else None)
    jobs = _jobs(cur, corr)
    turns = [int((j["result"] or {}).get("num_turns") or 0) for j in jobs]
    cost = sum(float((j["result"] or {}).get("cost_usd") or 0) for j in jobs)
    incomplete = sum(1 for j in jobs if (j["result"] or {}).get("incomplete"))
    layers = layers_of(files)
    recall, precision = expected_scores((last or {}).get("expected_files") or [], files)
    o = {"repo": repo, "issue": int(issue) if issue.isdigit() else None, "pr_url": pr_url, "pr_number": int(m.group(2)) if m else None,
         "merged": merged, "plan_id": (last or {}).get("id"), "planned": bool(plans), "steps": (last or {}).get("steps") or [],
         "design_scale": (last or {}).get("design_scale"), "jobs": len(jobs), "turns": sum(turns), "turns_max": max(turns) if turns else 0,
         "band": band(sum(turns)) if jobs else None, "cost_usd": round(cost, 4), "incomplete": incomplete, "continues": _continues(cur, corr),
         "files": len(files), "layers": layers, "layers_key": layers_key(layers), "expected_files": len((last or {}).get("expected_files") or []),
         "expected_recall": recall, "expected_precision": precision,
         "route_changed": len({tuple(p.get("steps") or []) for p in ready}) > 1, "escalated": sum(1 for p in plans if p.get("decision") == "escalate"),
         "needs_judgment": "factory:needs-judgment" in labels, "split_child": _split_child(cur, repo, int(issue)) if issue.isdigit() else False}
    o["judgment"], o["reasons"] = judge(o)
    return o


def record(cur, corr: str, o: dict, at: datetime | None = None) -> bool:
    """occurred_at は PR の結果が出た時刻にする（過去の分を後から残しても、窓で数えられるように）"""
    cur.execute("insert into events(name, occurred_at, correlation_id, payload, dedupe_key) values ('plan.outcome',coalesce(%s, now()),%s,%s,%s) "
                "on conflict (dedupe_key) do nothing returning id", (at, corr, Jsonb(o), f"plan.outcome:{o['pr_url']}"))
    return cur.fetchone() is not None


def pending(cur, days: int | None = STEP_DAYS, limit: int | None = STEP_LIMIT) -> list[dict]:
    """結果が出たのに、まだ突き合わせていない工場の PR"""
    cur.execute(f"""select e.correlation_id as corr, e.payload->>'pr_url' as pr_url, e.name = 'pr.merged' as merged, e.occurred_at as at from events e
                     where e.name in ('pr.merged','pr.closed_unmerged') and e.correlation_id like 'github:%%'
                       {"and e.occurred_at > now() - make_interval(days => %s)" if days else ""}
                       and not exists (select 1 from events o where o.dedupe_key = 'plan.outcome:' || (e.payload->>'pr_url'))
                     order by e.id {"limit %s" if limit else ""}""", tuple(x for x in (days, limit) if x))
    return cur.fetchall()


def step(cur, gh, days: int | None = STEP_DAYS, limit: int | None = STEP_LIMIT, log=lambda *a: None) -> int:
    """supervisor の周回（と factoryctl plan outcomes --backfill）。1 本ずつ GitHub から差分のファイルとラベルを引いて残す"""
    n = 0
    for r in pending(cur, days, limit):
        m = PR_URL.match(r["pr_url"] or "")
        if not m:
            continue
        repo, number = m.group(1), int(m.group(2))
        try:
            files = pr_files(gh, repo, number)
            labels = [l.get("name") for l in (gh("GET", f"repos/{repo}/pulls/{number}") or {}).get("labels") or []]
        except Exception as e:  # noqa: BLE001  1 本の失敗で周回を止めない。次の周回で拾い直す
            log("plan outcome skipped", r["pr_url"], repr(e)[:200])
            continue
        o = build(cur, r["corr"], r["pr_url"], bool(r["merged"]), files, labels)
        if record(cur, r["corr"], o, r["at"]):
            n += 1
            log("plan outcome", r["pr_url"], o["judgment"], o["reasons"])
    return n


# ------------------------------------------------------------------ 実績の表と合格線
def outcomes(cur, repo: str | None = None, days: int = 60, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    cur.execute("select occurred_at, payload from events where name='plan.outcome' and occurred_at > %s"
                + (" and payload->>'repo'=%s" if repo else "") + " order by id", (now - timedelta(days=days),) + ((repo,) if repo else ()))
    return [{"at": r["occurred_at"], **r["payload"]} for r in cur.fetchall()]


def size_table(rows: list[dict]) -> list[dict]:
    """製品ごと・層の組ごとの実績（マージされ、実装のジョブがあったものだけ）。プランナーの見積もりの材料"""
    by: dict[tuple[str, str], list[dict]] = {}
    for o in rows:
        if o.get("merged") and o.get("jobs"):
            by.setdefault((o.get("repo") or "?", o.get("layers_key") or "docs"), []).append(o)
    out = []
    for (repo, key), rs in sorted(by.items()):
        turns, files = [o["turns"] for o in rs], [o["files"] for o in rs]
        out.append({"repo": repo, "layers": key, "n": len(rs), "files_median": median(files), "turns_median": median(turns),
                    "turns_p90": percentile(turns, 0.9), "incomplete": sum(1 for o in rs if o.get("incomplete")),
                    "cost_median": round(median(o.get("cost_usd") or 0 for o in rs), 2),
                    "examples": [o["pr_url"] for o in sorted(rs, key=lambda o: -o["turns"])[:3]]})
    return out


def pass_status(rows: list[dict]) -> dict:
    """合格線の実績の 4 つ（ADR 0036 の 4c）。チケットが min_tickets に満たなければ measured=false"""
    rs = [o for o in rows if o.get("merged") and o.get("jobs")]
    n, pl = len(rs), PASS_LINES
    turns = [o["turns"] for o in rs]
    recalls = [o["expected_recall"] for o in rs if o.get("expected_recall") is not None]
    planned = [o for o in rs if o.get("planned")]
    metrics = {
        "incomplete_rate": {"value": round(sum(1 for o in rs if o.get("incomplete")) / n, 3) if n else None, "max": pl["incomplete_rate_max"]},
        "turns_median": {"value": median(turns) if turns else None, "max": pl["turns_median_max"]},
        "turns_p90": {"value": percentile(turns, 0.9), "max": pl["turns_p90_max"]},
        "recall_median": {"value": median(recalls) if recalls else None, "min": pl["recall_median_min"]},
        "misrouted_rate": {"value": round(sum(1 for o in planned if o.get("judgment") == "misrouted") / len(planned), 3) if planned else None,
                           "max": pl["misrouted_rate_max"]},
    }
    for m in metrics.values():
        v = m["value"]
        m["ok"] = None if v is None else (v <= m["max"] if "max" in m else v >= m["min"])
    measured = n >= pl["min_tickets"]
    return {"tickets": n, "planned": len(planned), "measured": measured, "metrics": metrics,
            "ok": measured and all(m["ok"] is not False for m in metrics.values()),
            "judgments": {j: sum(1 for o in rs if o.get("judgment") == j) for j in ("ok", "undersized", "misrouted", "oversized")}}
