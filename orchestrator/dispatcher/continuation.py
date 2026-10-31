"""続きのジョブと引き継ぎのボリューム（ADR 0016・0017）。

実装役（implement のワーカー）は PR を作ったら終わるが、作業の文脈はチケットごとのボリュームに残る。
合図（衝突・CI 落ち・検査役がマージ前に採用した指摘）が来たら、同じチケットの続きのジョブを積み、
ワーカーはボリュームの記録と PR のブランチから続きを直して同じブランチに push する。

- 続ける理由: incomplete（途中終了。factoryctl rerun --continue）・conflict（衝突）・ci_failed（CI 落ち）・review_fix（検査の指摘）
- 合図ごとの上限: ci_failed と review_fix は同じ PR で CONTINUE_MAX 回まで（既定 2）。超えたら呼び出し側が人へ回す（停止・差し戻し）。
  conflict は head ごとに 1 回（ADR 0016。既定ブランチが進むたびに起き得るので回数では切らない）
- ボリューム: チケットごとの Docker の名前付きボリューム（factory-ticket-<owner>-<repo>-<issue>）。中身はエージェントに依存しない形
  （ticket.json と jobs/<job>/ の Markdown・JSON。書くのはワーカーの entrypoint.sh）。エージェントのセッション記録（claude --resume）は
  agent/ の下の「あれば使う近道」で、なくても続きは作れる
- 片付け: PR がマージか閉じられたら後片付け役（ADR 0018）が記録をオブジェクトストアに移してボリュームを消す。
  後片付け役が動かなかったときの受け皿として、HANDOFF_KEEP_DAYS 日たって残っているボリュームも消す

続ける先は工場のブランチ名と PR の番号だけで、シェルにもプロンプトにも PR の本文は渡さない。合図の中身（CI のチェック名、指摘）は
job.enqueued の input に残し、ワーカーには標準入力で JSON として渡す（ワーカーはデータとしてファイルに置き、指示としては扱わせない）。
"""
from __future__ import annotations

import os
import re
import subprocess

import psycopg
from psycopg.types.json import Jsonb

ENV = os.environ
REASONS = ("incomplete", "conflict", "ci_failed", "review_fix")
SIGNALED = ("ci_failed", "review_fix")   # 合図の中身（input）を標準入力で渡す理由
CONTINUE_MAX = int(ENV.get("CONTINUE_MAX", "2"))   # 合図の種類ごと・PR ごとの続きの上限。値は仮置き
HANDOFF_ENABLED = ENV.get("HANDOFF_VOLUMES", "1") == "1"   # 0 でボリュームを付けない（続きは PR のブランチと本文だけで作る）
HANDOFF_KEEP_DAYS = int(ENV.get("HANDOFF_KEEP_DAYS", "7"))   # マージ後の後追い検査の差し戻しに備えて少し残す
BRANCH_RE = re.compile(r"^factory/issue-(\d+)-job-\d+$")


def volume_name(repo: str, issue: int) -> str:
    """チケットのボリュームの名前。Docker の名前に使えない文字は - にする"""
    return "factory-ticket-" + re.sub(r"[^A-Za-z0-9_.-]", "-", repo.replace("/", "-")) + f"-{int(issue)}"


def attempts(cur, pr_url: str, reason: str) -> int:
    cur.execute("select count(*) as n from events where name='continue.enqueued' and payload->>'pr_url'=%s and payload->>'reason'=%s",
                (pr_url, reason))
    return cur.fetchone()["n"]


def pending(cur, pr_url: str, sha: str, reason: str | None = None) -> dict | None:
    """その head の続きのジョブが待ち・実行中なら、その job.enqueued を返す（再起動・割り込みで積み直したジョブも carry_over で同じ形になる）"""
    cur.execute("select e.payload, j.id as job_id, j.state from events e join jobs j on j.id=e.job_id where e.name='job.enqueued' "
                "and e.payload ? 'continue' and e.payload->>'pr_url'=%s and e.payload->>'head_sha'=%s "
                "and (%s::text is null or e.payload->'continue'->>'reason'=%s) "
                "and j.state in ('queued','running') order by e.id desc limit 1", (pr_url, sha, reason, reason))
    return cur.fetchone()


def carry_over(cur, job_id: int) -> dict:
    """ジョブを積み直すとき（dispatcher の再起動・割り込み）に、続ける先と合図の中身を引き継ぐ。なければ空"""
    cur.execute("select payload from events where name='job.enqueued' and job_id=%s order by id limit 1", (job_id,))
    p = (cur.fetchone() or {}).get("payload") or {}
    return {k: p[k] for k in ("continue", "input", "pr_url", "head_sha") if k in p}


def enqueue(cur, *, repo: str, pr_number: int, pr_url: str, branch: str, head_sha: str, reason: str, by: str,
            signal: dict | None = None, priority: int | None = None) -> int | str | None:
    """続きのジョブを積む。返り値は積んだジョブの id、上限に達したら "limit"、積まなかった（工場のブランチでない・
    同じ head で積み済み・同じ Issue のジョブが動いている）なら None。動いているジョブがあるときは記録を残さないので、次の周期で積み直せる"""
    if reason not in REASONS:
        raise ValueError(reason)
    m = BRANCH_RE.match(branch or "")
    if not m:
        return None   # 工場のブランチでない PR（人が作り直した PR など）は触らない
    issue = int(m.group(1))
    key = f"continue:{pr_url}@{head_sha}:{reason}"
    cur.execute("select 1 from events where dedupe_key=%s", (key,))
    if cur.fetchone():
        return None
    n = attempts(cur, pr_url, reason)
    if reason in SIGNALED and n >= CONTINUE_MAX:
        return "limit"
    corr = f"github:{repo}#{issue}"
    cur.execute("select id, priority from tickets where source='github' and source_ref=%s", (f"{repo}#{issue}",))
    t = cur.fetchone() or {}
    try:
        cur.execute("insert into jobs(correlation_id, repo, issue_number, enqueued_by, model_tier, kind, ticket_id, priority) "
                    "values (%s,%s,%s,%s,'tier-large','implement',%s,%s) returning id",
                    (corr, repo, issue, by, t.get("id"), priority or t.get("priority") or 50))
    except psycopg.errors.UniqueViolation:
        return None   # 同じ Issue のジョブがすでに動いている
    jid = cur.fetchone()["id"]
    cont = {"branch": branch, "pr_number": int(pr_number), "reason": reason}
    payload = {"pr_url": pr_url, "head_sha": head_sha, "reason": reason, "attempt": n + 1, "continue": cont, "by": by}
    cur.execute("insert into events(name, correlation_id, job_id, payload, dedupe_key) values ('continue.enqueued',%s,%s,%s,%s)",
                (corr, jid, Jsonb(payload), key))
    cur.execute("insert into events(name, correlation_id, job_id, payload) values ('job.enqueued',%s,%s,%s)",
                (corr, jid, Jsonb({"by": by, "kind": "implement", "continue": cont, "pr_url": pr_url, "head_sha": head_sha,
                                   **({"input": signal} if signal is not None else {})})))
    return jid


def remove_volume(cur, corr: str, name: str, why: str, log=print) -> bool:
    """チケットのボリュームを消して handoff.removed を記録する。同じチケットに後でまたボリュームができたら、それも消せるように
    記録の重複よけは直前の handoff.saved ごとにする"""
    p = subprocess.run(["docker", "volume", "rm", "-f", name], capture_output=True, text=True)
    if p.returncode != 0:
        log("handoff remove failed", name, p.stderr[-200:])
        return False
    cur.execute("select max(id) as id from events where name='handoff.saved' and correlation_id=%s", (corr,))
    last = (cur.fetchone() or {}).get("id")
    cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values ('handoff.removed',%s,%s,%s) on conflict (dedupe_key) do nothing",
                (corr, Jsonb({"volume": name, "keep_days": HANDOFF_KEEP_DAYS, "why": why}), f"handoff.removed:{name}@{last}"))
    return True


def archive(volume: str, image: str) -> bytes | None:
    """ボリュームの引き継ぎの記録を tar.gz で取り出す（エージェントの会話の記録 agent/ は含めない）。取れなければ None"""
    p = subprocess.run(["docker", "run", "--rm", "--entrypoint", "tar", "-v", f"{volume}:/h:ro", image,
                        "czf", "-", "--exclude=./agent", "-C", "/h", "."], capture_output=True, timeout=300)
    return p.stdout if p.returncode == 0 and p.stdout else None


def cleanup(cur, log=print) -> int:
    """PR がマージか閉じられてから HANDOFF_KEEP_DAYS 日たって、まだ残っているチケットのボリュームを消す（後片付け役が動かなかった・失敗したときの受け皿）。
    消した数を返す"""
    if not HANDOFF_ENABLED:
        return 0
    cur.execute("""select distinct e.correlation_id as corr from events e
                    where e.name in ('pr.merged','pr.closed_unmerged') and e.occurred_at < now() - make_interval(days => %s)
                      and e.correlation_id like 'github:%%#%%'
                      and exists (select 1 from events h where h.name='handoff.saved' and h.correlation_id=e.correlation_id
                                   and not exists (select 1 from events r where r.name='handoff.removed' and r.correlation_id=h.correlation_id and r.id > h.id))
                      and not exists (select 1 from jobs j where j.correlation_id=e.correlation_id and j.state in ('queued','running'))""",
                (HANDOFF_KEEP_DAYS,))
    n = 0
    for r in cur.fetchall():
        repo, _, issue = r["corr"].split(":", 1)[1].rpartition("#")
        if issue.isdigit() and remove_volume(cur, r["corr"], volume_name(repo, int(issue)), "keep_days", log):
            n += 1
    return n
