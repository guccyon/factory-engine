"""後片付け（ADR 0021）。ラインが終わったチケットの引き継ぎのボリューム（ADR 0017）を、機械だけで片付ける。

- きっかけ: 工場の PR がマージか閉じられ（pr.merged / pr.closed_unmerged）、そのチケットのボリュームが残っていて、
  同じチケットのジョブが待ち・実行中でないこと
- 永続化: ボリュームの中身を選り分けずにそのまま tar.gz にし、オブジェクトストアの tickets/<repo>/<issue>/handoff-<日時>.tar.gz に置く
  （handoff.archived）。エージェントの会話の記録（agent/）も含める
- 破棄: 置けたらボリュームを消す（handoff.removed、why=tidy）。置けなければ消さずに次の回でやり直す。
  置けないまま日がたったものは、ADR 0017 の期限の片付け（continuation.cleanup、why なし）が受け皿になる

ラインのジョブとしては積まず、dispatcher の別の糸で動く。ジョブの枠を使わず、PR にも Issue にも何も足さない（片付けは成果物に含めない）。
LLM は使わない。選り分け（クレンジング）・申し送り・保留した判断の再起票は、Learn ループ（ADR 0013）と健康指標が要る入力が
分かった時点で、この ADR ごと見直す（2026-10-31 オーナー）。
"""
from __future__ import annotations

import os
import subprocess
import time

from psycopg.types.json import Jsonb

import continuation

ENV = os.environ
TIDY_ENABLED = ENV.get("TIDY_ENABLED", "1") == "1" and continuation.HANDOFF_ENABLED   # 0 で後片付けを止める（期限の片付けだけ残る）
TIDY_INTERVAL = int(ENV.get("TIDY_INTERVAL_SEC", "300"))   # 値は仮置き

DONE_TICKETS = """select distinct on (e.correlation_id) e.correlation_id as corr, e.payload->>'pr_url' as pr_url, e.name,
                         (select max(h.id) from events h where h.name='handoff.saved' and h.correlation_id=e.correlation_id) as saved_id
                    from events e
                   where e.name in ('pr.merged','pr.closed_unmerged') and e.correlation_id like 'github:%%#%%'
                     and exists (select 1 from events h where h.name='handoff.saved' and h.correlation_id=e.correlation_id
                                  and not exists (select 1 from events r where r.name='handoff.removed' and r.correlation_id=h.correlation_id and r.id > h.id))
                     and not exists (select 1 from jobs j where j.correlation_id=e.correlation_id and j.state in ('queued','running'))
                   order by e.correlation_id, e.id desc"""


def archive(volume: str, image: str) -> bytes | None:
    """ボリュームの中身をそのまま tar.gz で取り出す。取れなければ None"""
    try:
        p = subprocess.run(["docker", "run", "--rm", "--entrypoint", "tar", "-v", f"{volume}:/h:ro", image, "czf", "-", "-C", "/h", "."],
                           capture_output=True, timeout=300)
    except subprocess.TimeoutExpired:
        return None
    return p.stdout if p.returncode == 0 and p.stdout else None


def remove(cur, corr: str, name: str, saved_id: int, log=print) -> bool:
    """ボリュームを消して handoff.removed を記録する。重複よけは continuation.cleanup と同じく直前の handoff.saved ごと"""
    p = subprocess.run(["docker", "volume", "rm", "-f", name], capture_output=True, text=True)
    if p.returncode != 0:
        log("tidy: volume remove failed", name, p.stderr[-200:])
        return False
    cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values ('handoff.removed',%s,%s,%s) on conflict (dedupe_key) do nothing",
                (corr, Jsonb({"volume": name, "keep_days": continuation.HANDOFF_KEEP_DAYS, "why": "tidy"}), f"handoff.removed:{name}@{saved_id}"))
    return True


def run(cur, image: str, put, log=print) -> int:
    """済んだチケットのボリュームをオブジェクトストアに置き（put(key, data)）、置けたら消す。片付けた数を返す"""
    if not TIDY_ENABLED:
        return 0
    cur.execute(DONE_TICKETS)
    n = 0
    for r in cur.fetchall():
        repo, _, issue = r["corr"].split(":", 1)[1].rpartition("#")
        if not issue.isdigit():
            continue
        name = continuation.volume_name(repo, int(issue))
        dedupe = f"handoff.archived:{name}@{r['saved_id']}"
        cur.execute("select 1 from events where dedupe_key=%s", (dedupe,))
        if not cur.fetchone():   # 前の回で置けたが消せなかったものは、置き直さずに消すだけにする
            data = archive(name, image)
            if not data:
                log("tidy: archive failed", name)
                continue
            key = f"tickets/{repo}/{issue}/handoff-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.tar.gz"
            try:
                put(key, data)
            except Exception as e:  # noqa: BLE001  置けなければ消さずに次の回へ。ほかのチケットは続ける
                log("tidy: put failed", name, repr(e))
                continue
            cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values ('handoff.archived',%s,%s,%s) on conflict (dedupe_key) do nothing",
                        (r["corr"], Jsonb({"volume": name, "blob_key": key, "bytes": len(data), "pr_url": r["pr_url"],
                                           "outcome": "merged" if r["name"] == "pr.merged" else "closed_unmerged"}), dedupe))
        if remove(cur, r["corr"], name, r["saved_id"], log):
            n += 1
    return n
