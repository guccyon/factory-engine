#!/usr/bin/env python3
"""工場の手動操作: 一覧・停止・再実行（ADR 0003）。

  docker compose exec dispatcher factoryctl list [--all]
  docker compose exec dispatcher factoryctl stop <job_id>
  docker compose exec dispatcher factoryctl rerun <job_id> [--continue]   # --continue: 途中終了のドラフト PR のブランチから続ける
  docker compose exec dispatcher factoryctl enqueue <repo> <issue> [--tier tier-small]
  docker compose exec dispatcher factoryctl events <job_id>

チケットの順番（ADR 0009）。操業コンソールと同じ操作。<ticket> はチケットの id か owner/repo#123:
  factoryctl tickets [--all]                      # 投入待ち・作業中のチケット（優先度の高い順）
  factoryctl priority <ticket> p0|p1|p2|p3|<0-100> [--reason ..]   # p0 は作業中のものを止めてでも先に流す
  factoryctl hold <ticket> [--reason ..]          # 順番待ちから一時的に外す
  factoryctl resume <ticket>

操業停止（ADR 0005）。番人が見えない停止（builder の端末の許可待ちなど）は手で記録する:
  factoryctl stoppage open <分類> <何が止まったか> [--key K] [--waiting-for ..] [--cause ..] [--prevention ..] [--at ISO] [--by builder] [--quiet]
  factoryctl stoppage close <key> [--remedy ..] [--prevention ..] [--at ISO] [--woken-by pr_event|watcher|schedule|human] [--quiet]   # --quiet は通知しない（過去分の取り込み）。--woken-by は builder を起こしたもの（D8）
  factoryctl stoppage list [--open]
  factoryctl report stoppages [--from YYYY-MM-DD] [--to YYYY-MM-DD]   # Markdown。既定は直近 7 日。週次レポートの stoppages.md はこの出力

来歴とロット（ADR 0007）。Release は後付けで、前の Release 以降にマージされた工場の PR をまとめて含める:
  factoryctl release create <repo> <app_version> [--prs URL,URL]

出荷と Rollback（ADR 0012）。依頼を events に書くだけで、手を動かすのは常駐の shipper:
  factoryctl release ship <release_id> [--by ..]            # イメージを作って staging に出す（staging は自動）
  factoryctl release approve <release_id> [--by ..]         # 承認ボタンと同じ。staging で確かめた版を prod に出す
  factoryctl release rollback <repo> [--env prod] [--by ..] # その環境を 1 つ前の版に戻す（DB は戻さない）
  factoryctl release status <repo>                          # 環境ごとのいまの版と前の版、出荷の記録
  factoryctl report provenance [--from YYYY-MM-DD] [--to YYYY-MM-DD]   # 来歴の付与率と Release への紐付け率

モードと判断（ADR 0006）:
  factoryctl mode show
  factoryctl mode set attended|unattended [--until ISO]       # 期限まで（なければ次に切り替えるまで）時間帯と既定より優先
  factoryctl judgment list [--ungraded] [--limit N]
  factoryctl judgment recategorize <remap.yaml>                 # 類型の振り直し（definitions/judgment-remap-*.yaml。元の類型は category_v1 に残す）
  factoryctl judgment grade <artifact_id> agree|disagree [--note ..]   # 人の採点（教師データ）。類型ごとの一致率になる
  factoryctl report judgments [--from YYYY-MM-DD] [--to YYYY-MM-DD]   # 類型・モード別の件数、確信度、一致率、差し戻し、類型の見直しの候補

Learn ループ（ADR 0013）:
  factoryctl report learning [--days 30]          # 教師信号の束（signature ごとの件数・チケット・段階の候補・eval の元）
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import judgment
import learn
import ops
import ship
import stoppage


def opt(argv, name, default=None):
    return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else default


def report_stoppages(cur, argv):
    today = datetime.now(timezone.utc).date()
    to = opt(argv, "--to", (today + timedelta(days=1)).isoformat())
    frm = opt(argv, "--from", (today - timedelta(days=6)).isoformat())
    # 期間に重なる停止（開いたままのものを含む）
    cur.execute("select *, coalesce(ended_at, now()) - started_at as dur from stoppages "
                "where started_at < %s::date and coalesce(ended_at, now()) >= %s::date order by started_at", (to, frm))
    rows = cur.fetchall()
    out = [f"# 操業停止の記録（{frm} 〜 {to}、{os.environ.get('REPORT_TZ', 'Asia/Tokyo')}、制御プレーンから生成）", "",
           "生成: `factoryctl report stoppages --from --to`。記録は events の stoppage.opened / closed（ADR 0005）", ""]
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(r["category"], []).append(r)
    out += ["## 内訳", "", "| 分類 | 件数 | 合計時間 | 開いたまま |", "|---|---|---|---|"]
    for cat in stoppage.CATEGORIES:
        rs = by.get(cat, [])
        if rs:
            mins = sum(int(r["dur"].total_seconds() // 60) for r in rs)
            out.append(f"| {cat} | {len(rs)} | {mins // 60} 時間 {mins % 60} 分 | {sum(1 for r in rs if r['ended_at'] is None)} |")
    # builder を起こしたもの（D8）。builder が閉じた停止のうち、人が起こした割合を自律の度合いとして見る
    cur.execute("select coalesce(payload->>'woken_by', '未記録') as w, count(*) as n from events where name='stoppage.closed' "
                "and payload->>'closed_by'='builder' and occurred_at >= %s::date and occurred_at < %s::date group by 1", (frm, to))
    woke = {r["w"]: r["n"] for r in cur.fetchall()}
    known = sum(n for w, n in woke.items() if w != "未記録")
    out += ["", "## builder を起こしたもの（D8）", "", "| 起こしたもの | 件数 |", "|---|---|"] + [f"| {w} | {n} |" for w, n in sorted(woke.items())]
    out += ["", f"- 人が起こした割合: {woke.get('human', 0)}/{known}" + (f"（{woke.get('human', 0) / known:.0%}）" if known else "（記録なし）")]
    out += ["", "## 一覧", "", "| key | 開始 | 終了 | 分類 | 検知 | 何が止まったか | 原因 | 手当て | 再発を防ぐ仕組み |", "|---|---|---|---|---|---|---|---|---|"]

    def cell(v):
        return (v or "").replace("|", "／").replace("\n", " ")
    for r in rows:
        end = f"{r['ended_at']:%m-%d %H:%M}" if r["ended_at"] else "**開いたまま**"
        out.append(f"| {r['key']} | {r['started_at']:%m-%d %H:%M} | {end} | {r['category']} | {r['detected_by']} | {cell(r['what'])} | "
                   f"{cell(r['cause'])} | {cell(r['remedy'])} | {cell(r['prevention'])} |")
    print("\n".join(out))


def window(argv):
    # 比較は REPORT_TZ（既定 Asia/Tokyo）の日付で行うので、既定の期間も同じ時刻帯の今日から作る。
    # UTC の今日だと、日本の朝（UTC の前日）に当日分が --to から外れていた
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo(os.environ.get("REPORT_TZ", "Asia/Tokyo"))).date()
    return opt(argv, "--from", (today - timedelta(days=6)).isoformat()), opt(argv, "--to", (today + timedelta(days=1)).isoformat())


def release_create(cur, argv):
    repo, version = argv[3], argv[4]
    prs = [u.strip() for u in opt(argv, "--prs").split(",") if u.strip()] if opt(argv, "--prs") else None
    rid, prs = ship.create_release(cur, repo, version, prs)
    if rid is None:
        print(f"{repo}@{version} already exists"); return 1
    print(f"created {rid} with {len(prs)} PRs")
    return 0


def release_status(cur, repo):
    st = ship.status(cur, repo)
    for env, v in sorted(st["environments"].items()):
        print(f"{env:<8} いま {v['release_id']}  前 {v['previous'] or '-'}")
    cur.execute("select id, occurred_at, name, payload from events where name like 'release.%%' and correlation_id like %s "
                "and name not in ('release.included') order by id desc limit 20", (f"release:{repo}@%",))
    for r in reversed(cur.fetchall()):
        p = r["payload"]
        extra = " ".join(f"{k}={p[k]}" for k in ("env", "seconds", "ok", "from", "by", "via", "error") if k in p)
        print(f"{r['id']:>6} {r['occurred_at']:%m-%d %H:%M:%S} {r['name']:<28} {p.get('release_id', '')} {extra[:160]}")
    return 0


def report_provenance(cur, argv):
    frm, to = window(argv)
    cur.execute("""select a.payload->>'pr_url' as pr_url, a.payload->'provenance' as prov,
                          (select r.payload->>'release_id' from events r where r.name='release.included' and r.payload->>'pr_url'=a.payload->>'pr_url' limit 1) as release_id,
                          (select count(*) from artifacts j where j.type='Judgment' and j.payload->>'pr_url'=a.payload->>'pr_url') as judgments
                     from artifacts a where a.type='PR' and a.created_at >= %s::date and a.created_at < %s::date""", (frm, to))
    rows = cur.fetchall()
    n = len(rows)

    def rate(k):
        return f"{k}/{n}（{k / n:.0%}）" if n else "0/0"
    with_prov = sum(1 for r in rows if r["prov"])
    mode_known = sum(1 for r in rows if (r["prov"] or {}).get("mode") not in (None, "unknown"))
    released = sum(1 for r in rows if r["release_id"])
    out = [f"# 来歴の紐付け（{frm} 〜 {to}、制御プレーンから生成）", "", "生成: `factoryctl report provenance --from --to`（ADR 0007）。属性はすべて optional なので、統計には必ずこの紐付け率を併記する", "",
           "| 項目 | 紐付け率 |", "|---|---|", f"| 工場の PR | {n} |", f"| 製造の来歴あり | {rate(with_prov)} |", f"| モードが分かる | {rate(mode_known)} |",
           f"| Release に入った | {rate(released)} |", f"| 仮決めを含む | {rate(sum(1 for r in rows if r['judgments']))} |"]
    # 出荷の来歴（ADR 0012）: Release ごとの作り・環境・承認・戻し
    cur.execute("""select payload->>'release_id' as rid,
                          (select count(*) from events i where i.name='release.included' and i.payload->>'release_id'=c.payload->>'release_id') as prs,
                          (select max((b.payload->>'seconds')::numeric) from events b where b.name='release.built' and b.payload->>'release_id'=c.payload->>'release_id') as built_s,
                          (select string_agg(distinct d.payload->>'env', '・') from events d where d.name='release.deployed' and d.payload->>'release_id'=c.payload->>'release_id') as envs,
                          (select string_agg(distinct a.payload->>'via', '・') from events a where a.name='release.approved' and a.payload->>'release_id'=c.payload->>'release_id') as approved_via,
                          (select max((r.payload->>'seconds')::numeric) from events r where r.name='release.rolled_back' and r.payload->>'from'=c.payload->>'release_id') as rollback_s,
                          (select count(*) from events b, jsonb_array_elements_text(coalesce(b.payload->'contract_migrations','[]'::jsonb)) x
                            where b.name='release.built' and b.payload->>'release_id'=c.payload->>'release_id') as contracts
                     from events c where c.name='release.created' and c.occurred_at >= %s::date and c.occurred_at < %s::date order by c.id""", (frm, to))
    rels = cur.fetchall()
    if rels:
        out += ["", "## 出荷（ADR 0012）", "", "| Release | PR | イメージ作り（秒） | 出した環境 | 承認の入口 | この版から戻した秒数 | contract |", "|---|---|---|---|---|---|---|"]
        out += [f"| {r['rid']} | {r['prs']} | {r['built_s'] or '-'} | {r['envs'] or '-'} | {r['approved_via'] or '-'} | {r['rollback_s'] or '-'} | {r['contracts']} |" for r in rels]
    print("\n".join(out))


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 1.0)
    p, d = k / n, 1 + z * z / n
    c, h = (p + z * z / (2 * n)) / d, z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (max(0.0, c - h), min(1.0, c + h))


def false_positive_lines(jd):
    """検査器の指摘 1 件ごとの judge の裁き（inspection 類型）から偽陽性率を出す（health.yaml の false_positive_rate）。
    judge の推定: 不採用 / （採用＋不採用）。人へ回したものは分母から除く。
    人の確認: 人が採点した指摘のうち、偽陽性だったもの（judge の不採用に人が同意、または judge の採用に人が不同意）"""
    adopt = sum(1 for r in jd if r["v"] == "adopt")
    reject = sum(1 for r in jd if r["v"] == "reject")
    n = adopt + reject
    lines = ["| 指標 | 値 | 件数 | Wilson 95% |", "|---|---|---|---|"]
    if n:
        lo, hi = wilson(reject, n)
        lines.append(f"| 偽陽性率（judge の推定） | {reject / n:.0%} | {reject}/{n} | {lo:.2f}〜{hi:.2f} |")
    else:
        lines.append("| 偽陽性率（judge の推定） | — | 0/0 | — |")
    graded = [r for r in jd if r["g"] in ("agree", "disagree") and r["v"] in ("adopt", "reject")]
    fp = sum(1 for r in graded if (r["v"] == "reject") == (r["g"] == "agree"))
    if graded:
        lo, hi = wilson(fp, len(graded))
        lines.append(f"| 偽陽性率（人の確認） | {fp / len(graded):.0%} | {fp}/{len(graded)} | {lo:.2f}〜{hi:.2f} |")
    else:
        lines.append("| 偽陽性率（人の確認） | — | 0/0（採点待ち） | — |")
    lines += ["", f"- 人へ回した指摘 {sum(1 for r in jd if r['v'] == 'escalate')} 件は分母から除く。見逃し（miss_rate）はまだ測っていない"]
    return lines


def report_judgments(cur, argv):
    frm, to = window(argv)
    cur.execute("""select payload->>'category' as c, payload->>'stage' as st, payload->>'mode' as m, payload->>'verdict' as v,
                          (payload->'score'->>'self_reported')::float as conf, payload->'human_grade'->>'grade' as g
                     from artifacts where type='Judgment' and created_at >= %s::date and created_at < %s::date""", (frm, to))
    rows = cur.fetchall()
    out = [f"# 判断の記録（{frm} 〜 {to}、制御プレーンから生成）", "", "生成: `factoryctl report judgments --from --to`（ADR 0006）。類型は definitions/judgments.yaml " + judgment.TAXONOMY_VERSION, ""]
    out += ["## 類型 × 段階", "", "| 類型 | 仮決め | judge | うち差し替え・不採用 | 人へ | 確信度の平均 | 採点済み | 一致率 |", "|---|---|---|---|---|---|---|---|"]
    cats = sorted({r["c"] for r in rows})
    for c in cats:
        rs = [r for r in rows if r["c"] == c]
        prov = [r for r in rs if r["st"] == "provisional"]
        jd = [r for r in rs if r["st"] == "judged"]
        confs = [r["conf"] for r in jd if r["conf"] is not None]
        cur.execute("select count(*) as n, avg(case when e.payload->>'grade'='agree' then 1.0 else 0.0 end) as a from events e "
                    "join artifacts a on a.id = (e.payload->>'artifact_id')::bigint "
                    "where e.name='judgment.graded' and a.payload->>'category'=%s and e.occurred_at >= %s::date and e.occurred_at < %s::date", (c, frm, to))
        g = cur.fetchone()
        avg = f"{sum(confs) / len(confs):.2f}" if confs else "—"
        agree = f"{float(g['a']):.0%}" if g["n"] else "—"
        changed = sum(1 for r in jd if r["v"] in ("replace", "reject"))
        escal = sum(1 for r in jd if r["v"] == "escalate")
        out.append(f"| {c} | {len(prov)} | {len(jd)} | {changed} | {escal} | {avg} | {g['n']} | {agree} |")
    cur.execute("select count(*) as n from artifacts where type='Judgment' and payload->>'stage'='provisional' and (payload->>'ticket_interpretation')::boolean "
                "and created_at >= %s::date and created_at < %s::date", (frm, to))
    out += ["", f"- うちチケットの受け入れ条件の解釈で決めた仮決め（印 ticket_interpretation）: {cur.fetchone()['n']} 件。多ければチケットの書き方を直す"]
    out += ["", "## 検査の偽陽性率", ""] + false_positive_lines([r for r in rows if r["c"] == "inspection" and r["st"] == "judged"])
    out += ["", "## モード別（judge）", "", "| モード | 件数 | 人へ |", "|---|---|---|"]
    for m in sorted({r["m"] for r in rows if r["st"] == "judged"}):
        rs = [r for r in rows if r["st"] == "judged" and r["m"] == m]
        out.append(f"| {m} | {len(rs)} | {sum(1 for r in rs if r['v'] == 'escalate')} |")
    cur.execute("select name, count(*) as n from events where name like 'sendback.%%' and occurred_at >= %s::date and occurred_at < %s::date group by 1", (frm, to))
    sb = {r["name"]: r["n"] for r in cur.fetchall()}
    cur.execute("select count(*) as n from events where name='sendback.filed'")
    total_sb = cur.fetchone()["n"]
    out += ["", "## 差し戻し", "", f"- 起票 {sb.get('sendback.filed', 0)} 件、上限で流さず {sb.get('sendback.skipped', 0)} 件、再検査で指摘が残った {sb.get('sendback.residual', 0)} 件",
            f"- 累計の起票 {total_sb} 件（20 件で「1 PR 1 回」の上限を見直す）"]
    # 類型の見直しの候補（データ量で起こす。分け直しは人）
    # 仮決め全体に占める割合で見る（definitions/judgments.yaml の split_candidate。分け直しは全件の振り直しで、人が決める）
    cur.execute("select payload->>'category' as c, count(*) as n from artifacts where type='Judgment' and payload->>'stage'='provisional' group by 1")
    counts = {r["c"]: r["n"] for r in cur.fetchall()}
    total = sum(counts.values())
    hints = [f"- {c}: {n} 件（仮決めの {n / total:.0%}）。全件の振り直しの候補" for c, n in sorted(counts.items()) if total and n / total > 0.4]
    out += ["", "## 類型の見直しの候補", ""] + (hints or ["- なし"])
    print("\n".join(out))


def find_ticket(cur, ref: str):
    if ref.isdigit():
        cur.execute("select * from tickets where id=%s", (int(ref),))
    else:
        cur.execute("select * from tickets where source_ref=%s order by id limit 1", (ref,))
    return cur.fetchone()


def main(argv):
    conn = psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row, autocommit=True)
    cur = conn.cursor()
    cur.execute("select set_config('timezone', %s, false)", (os.environ.get("REPORT_TZ", "Asia/Tokyo"),))   # 表示は建設者の時刻で
    cmd = argv[1] if len(argv) > 1 else "list"
    if cmd == "list":
        where = "" if "--all" in argv else "where state in ('queued','running') or finished_at > now() - interval '1 day'"
        cur.execute(f"select id, state, repo, issue_number, attempt, enqueued_at, started_at, finished_at, error, result->>'pr_url' as pr_url from jobs {where} order by id desc limit 50")
        for r in cur.fetchall():
            print(f"{r['id']:>4} {r['state']:<9} {r['repo']}#{r['issue_number']} a{r['attempt']} {r['enqueued_at']:%m-%d %H:%M} {r['error'] or ''} {r['pr_url'] or ''}")
    elif cmd == "stop":
        print("cancel requested" if ops.stop_job(cur, int(argv[2]), "factoryctl", opt(argv, "--reason", "")) else "job is not active")
    elif cmd == "rerun":
        job_id = int(argv[2])
        cur.execute("select repo, issue_number, attempt, kind, ticket_id, priority, result from jobs where id=%s", (job_id,))
        j = cur.fetchone()
        if not j:
            print("no such job"); return 1
        cont = None
        if "--continue" in argv:   # 途中終了のドラフト PR のブランチから続ける（最初からやり直さない）
            res = j["result"] or {}
            if j["kind"] != "implement" or not res.get("pr_number"):
                print("--continue needs an implement job that left a PR"); return 1
            cont = {"branch": f"factory/issue-{j['issue_number']}-job-{job_id}", "pr_number": int(res["pr_number"]), "reason": "incomplete"}
        try:
            cur.execute("insert into jobs(correlation_id, repo, issue_number, attempt, enqueued_by, model_tier, kind, ticket_id, priority) "
                        "values (%s,%s,%s,%s,'rerun',%s,%s,%s,%s) returning id",
                        (f"github:{j['repo']}#{j['issue_number']}", j["repo"], j["issue_number"], j["attempt"] + 1, os.environ.get("MODEL_TIER", "tier-large"),
                         j["kind"], j["ticket_id"], j["priority"]))
        except psycopg.errors.UniqueViolation:
            print("an active job already exists for this issue"); return 1
        new_id = cur.fetchone()["id"]
        cur.execute("insert into events(name, correlation_id, job_id, payload) values ('job.enqueued', %s, %s, %s)",
                    (f"github:{j['repo']}#{j['issue_number']}", new_id, Jsonb({"by": "rerun", "attempt": j["attempt"] + 1, "previous": job_id, **({"continue": cont} if cont else {})})))
        print(f"enqueued job {new_id} (attempt {j['attempt'] + 1}{', continue ' + cont['branch'] if cont else ''})")
    elif cmd == "enqueue":   # 手動投入（試験運転や、ラベルを使わない再投入）。--tier tier-small でモデル tier を変えられる（ペア比較の材料）
        repo, issue = argv[2], int(argv[3])
        tier = argv[argv.index("--tier") + 1] if "--tier" in argv else os.environ.get("MODEL_TIER", "tier-large")
        t = ops.upsert_ticket(cur, "github", f"{repo}#{issue}", repo, state="active")
        try:
            cur.execute("insert into jobs(correlation_id, repo, issue_number, enqueued_by, model_tier, ticket_id, priority) values (%s,%s,%s,'factoryctl',%s,%s,%s) returning id",
                        (f"github:{repo}#{issue}", repo, issue, tier, t["id"], t["priority"]))
        except psycopg.errors.UniqueViolation:
            print("an active job already exists for this issue"); return 1
        new_id = cur.fetchone()["id"]
        cur.execute("insert into events(name, correlation_id, job_id, payload) values ('job.enqueued', %s, %s, %s)",
                    (f"github:{repo}#{issue}", new_id, Jsonb({"by": "factoryctl"})))
        print(f"enqueued job {new_id}")
    elif cmd == "tickets":
        where = "" if "--all" in argv else "where state <> 'closed'"
        cur.execute(f"select t.*, (select string_agg(j.id || ':' || j.state, ' ') from jobs j where j.ticket_id=t.id and j.state in ('queued','running')) as jobs "
                    f"from tickets t {where} order by priority desc, created_at limit 100")
        for t in cur.fetchall():
            hold = " 保留" if t["held"] else ""
            wait = f" 依存待ち {' '.join(t['waiting_on'])}" if t["waiting_on"] else ""
            print(f"{t['id']:>4} {ops.level_of(t['priority'])}({t['priority']}) {t['state']:<7} {t['source_ref']} {t['title'][:40]}{hold}{wait} {t['jobs'] or ''}")
    elif cmd in ("priority", "hold", "resume"):
        t = find_ticket(cur, argv[2]) if len(argv) > 2 else None
        if not t:
            print("no such ticket"); return 1
        if cmd == "priority":
            v = argv[3]
            value = ops.LEVELS[v][0] if v in ops.LEVELS else int(v)
            t = ops.set_priority(cur, t["id"], value, "factoryctl", opt(argv, "--reason", ""))
            print(f"ticket {t['id']} priority {ops.level_of(t['priority'])}({t['priority']})")
        else:
            t = ops.set_held(cur, t["id"], cmd == "hold", "factoryctl", opt(argv, "--reason", ""))
            print(f"ticket {t['id']} {'held' if t['held'] else 'resumed'}")
    elif cmd == "events":
        job_id = int(argv[2])
        cur.execute("select name, occurred_at, payload from events where job_id=%s order by id", (job_id,))
        for r in cur.fetchall():
            print(f"{r['occurred_at']:%H:%M:%S} {r['name']:<22} {json.dumps(r['payload'], ensure_ascii=False)}")
    elif cmd == "stoppage":
        sub = argv[2] if len(argv) > 2 else "list"
        if sub == "open":
            category, what = argv[3], argv[4]
            key = opt(argv, "--key", f"manual:{datetime.now(timezone.utc):%Y%m%dT%H%M%S}")
            ok = stoppage.open_stoppage(cur, key, category, what, waiting_for=opt(argv, "--waiting-for", ""), cause=opt(argv, "--cause", ""),
                                        prevention=opt(argv, "--prevention", ""), started_at=opt(argv, "--at"), detected_by=opt(argv, "--by", "builder"), quiet="--quiet" in argv)
            print(f"opened {key}" if ok else f"{key} already exists")
        elif sub == "close":
            key = argv[3]
            ok = stoppage.close_stoppage(cur, key, remedy=opt(argv, "--remedy", ""), prevention=opt(argv, "--prevention", ""),
                                         ended_at=opt(argv, "--at"), closed_by="builder", quiet="--quiet" in argv, woken_by=opt(argv, "--woken-by"))
            print(f"closed {key}" if ok else f"{key} is not open")
        else:
            where = "where ended_at is null" if "--open" in argv else ""
            cur.execute(f"select * from stoppages {where} order by started_at")
            for r in cur.fetchall():
                end = f"{r['ended_at']:%m-%d %H:%M}" if r["ended_at"] else "open"
                print(f"{r['key']:<26} {r['category']:<11} {r['started_at']:%m-%d %H:%M} → {end:<11} {r['what']}")
    elif cmd == "report" and len(argv) > 2 and argv[2] == "stoppages":
        report_stoppages(cur, argv)
    elif cmd == "report" and len(argv) > 2 and argv[2] == "provenance":
        report_provenance(cur, argv)
    elif cmd == "report" and len(argv) > 2 and argv[2] == "judgments":
        report_judgments(cur, argv)
    elif cmd == "report" and len(argv) > 2 and argv[2] == "learning":
        retro = learn.retrospect(cur, window_days=int(opt(argv, "--days", str(learn.WINDOW_DAYS))))
        print("\n".join([f"# Learn ループ（{retro['from'][:10]} 〜 {retro['to'][:10]}、制御プレーンから生成）", "",
                         "生成: `factoryctl report learning`（ADR 0013）", ""] + learn.report_lines(retro) + [""]
                        + learn.rule_lines(learn.rule_stats(cur, datetime.fromisoformat(retro["from"]), datetime.fromisoformat(retro["to"])))))
    elif cmd == "mode":
        if len(argv) > 3 and argv[2] == "set" and argv[3] in judgment.MODES:
            cur.execute("insert into events(name, payload) values ('factory.mode_set', %s)", (Jsonb({"mode": argv[3], "until": opt(argv, "--until"), "by": "factoryctl"}),))
        mode, basis = judgment.current_mode(cur, os.environ.get("FACTORY_MODE") or "attended", os.environ.get("MODE_SCHEDULE", ""))
        print(f"mode={mode}（決め手: {basis}）")
    elif cmd == "judgment":
        sub = argv[2] if len(argv) > 2 else "list"
        if sub == "recategorize":   # 類型の振り直し（人が決めた分け直しを当てる。冪等）
            import yaml
            with open(argv[3]) as f:
                print(judgment.recategorize(cur, yaml.safe_load(f)))
        elif sub == "grade":
            aid, grade = int(argv[3]), argv[4]
            if grade not in ("agree", "disagree"):
                print("grade は agree か disagree"); return 2
            cur.execute("select payload from artifacts where id=%s and type='Judgment'", (aid,))
            r = cur.fetchone()
            if not r:
                print("no such judgment"); return 1
            cur.execute("update artifacts set payload = jsonb_set(payload, '{human_grade}', %s) where id=%s", (Jsonb({"grade": grade, "note": opt(argv, "--note")}), aid))
            cur.execute("insert into events(name, payload) values ('judgment.graded', %s)",
                        (Jsonb({"artifact_id": aid, "category": r["payload"].get("category"), "stage": r["payload"].get("stage"), "mode": r["payload"].get("mode"),
                                "grade": grade, "note": opt(argv, "--note")}),))
            print(f"graded {aid} {grade}")
        else:
            where = "and payload->'human_grade' = 'null'::jsonb" if "--ungraded" in argv else ""
            cur.execute(f"select id, created_at, payload from artifacts where type='Judgment' {where} order by id desc limit %s", (int(opt(argv, "--limit", "30")),))
            for r in cur.fetchall():
                p = r["payload"]
                conf = (p.get("score") or {}).get("self_reported")
                print(f"{r['id']:>5} {p.get('stage', ''):<11} {p.get('category', ''):<13} {p.get('verdict') or '':<9} conf={conf} {p.get('mode', '')} {(p.get('question') or '')[:60]}")
    elif cmd == "release" and len(argv) > 4 and argv[2] == "create":
        return release_create(cur, argv)
    elif cmd == "release" and len(argv) > 3 and argv[2] in ("ship", "approve", "rollback"):
        by = opt(argv, "--by", "builder")
        if argv[2] == "ship":
            ok, msg = ship.request_ship(cur, argv[3], by, "factoryctl")
        elif argv[2] == "approve":
            ok, msg = ship.request_approve(cur, argv[3], by, "factoryctl")
        else:
            ok, msg = ship.request_rollback(cur, argv[3], opt(argv, "--env", "prod"), by, "factoryctl")
        print(msg)
        return 0 if ok else 1
    elif cmd == "release" and len(argv) > 3 and argv[2] == "status":
        return release_status(cur, argv[3])
    else:
        print(__doc__); return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
