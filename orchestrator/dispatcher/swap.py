"""工場の入れ替えの依頼と記録（ADR 0031 の決定 1・4〜7）。dispatcher・factoryctl・入れ替え役（swapper.py）が同じ関数を通る。

- 状態の正本は events（swap.*）。1 件の入れ替えは swap.requested の id を swap_id にし、correlation_id は swap:<id>
- 依頼は同時に 1 件（終わっていない入れ替えがあれば新しい依頼を受けない）
- 入れ替え役は常駐させない。dispatcher が周回で、受けた（confirmed）依頼の時刻になったら使い捨てのコンテナ（factory-swap-<id>）を起動する。
  入れ替え役は compose のサービスではないので、dispatcher を作り直しても止まらない。結果を書かずに消えたら、dispatcher が failed にして人に回す
- 順番 2 では時刻の合意はまだない（factoryctl swap run が依頼と同時に「いま」で受ける）。プランナーの提案は順番 4
- 順番 3 で依頼の入口に Issue の郵便受け（swap_mailbox.py）を足した。依頼の source に Issue（owner/repo#n）を残し、結果を同じ Issue に返す
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone

import yaml
from psycopg.types.json import Jsonb

from globmatch import glob_re

ENV = os.environ
KINDS = ("code", "control_plane", "db")   # 軽い順
TERMINAL = ("swap.completed", "swap.rolled_back", "swap.escalated", "swap.failed", "swap.preflight_failed", "swap.cancelled")
STATUS = {"swap.requested": "requested", "swap.confirmed": "confirmed", "swap.launched": "launched", "swap.started": "started",
          "swap.drained": "drained", "swap.applied": "applied", "swap.verified": "verified", "swap.completed": "completed",
          "swap.rolled_back": "rolled_back", "swap.escalated": "escalated", "swap.failed": "failed",
          "swap.preflight_failed": "preflight_failed", "swap.cancelled": "cancelled"}
STATUS_JA = {"requested": "依頼を受けた", "confirmed": "時刻を受けた", "launched": "入れ替え役を起動した", "started": "事前の確かめ中",
             "drained": "一時停止した", "applied": "上げ直した（確かめ中）", "verified": "確かめが通った", "completed": "入れ替えた",
             "rolled_back": "前の版に戻した", "escalated": "人に回した", "failed": "失敗した", "preflight_failed": "事前の確かめで止めた",
             "cancelled": "取り消した"}
REF = re.compile(r"^(main|[0-9a-f]{7,40})$")
CONTAINER_PREFIX = "factory-swap-"
LABEL = "factory.swap"
LOST_AFTER_SEC = 90   # 起動してからこの秒数たってもコンテナがなく、結果もなければ、入れ替え役が消えたとみなす
DEFINITIONS = "definitions/swap.yaml"
DEFAULT_RULES = {"kinds": [{"kind": "db", "paths": ["orchestrator/postgres/init/**"]}, {"kind": "control_plane", "paths": ["orchestrator/compose.yaml"]}],
                 "escalate": [], "drain_wait_sec": 2700, "verify": {"timeout_sec": 300, "interval_sec": 10}}


class SwapError(Exception):
    pass


def corr(sid: int) -> str:
    return f"swap:{sid}"


def record(cur, sid: int, name: str, payload: dict | None = None) -> None:
    if name not in STATUS:
        raise ValueError(name)
    cur.execute("insert into events(name, correlation_id, payload) values (%s, %s, %s)", (name, corr(sid), Jsonb({"swap_id": sid, **(payload or {})})))


# ------------------------------------------------------------------ 依頼・取り消し・状態
def request(cur, target: str, by: str, *, kind: str = "code", urgency: str = "normal", note: str = "", confirm_now: bool = True,
            source: str = "manual", basis: str = "manual") -> int:
    """入れ替えの依頼。target は main かコミット（main に入っているかは入れ替え役が事前の確かめで見る）。
    confirm_now は時刻の合意のない手での実行（factoryctl swap run。順番 2）で、依頼と同時に「いま」で受ける。
    source は依頼の出どころ（manual か Issue の owner/repo#n）。basis は受けた決め手（confirmed の記録に残す）"""
    target = (target or "").strip().lower() if target != "main" else "main"
    if not REF.match(target):
        raise SwapError("target は main か 7〜40 文字のコミット")
    if kind not in KINDS:
        raise SwapError(f"kind は {'・'.join(KINDS)} のどれか")
    if urgency not in ("normal", "urgent"):
        raise SwapError("urgency は normal か urgent")
    with cur.connection.transaction():   # 同時に 2 件出さない（確かめと書き込みを 1 つのトランザクションで、ロックを取って）
        return _request(cur, target, by, kind, urgency, note, confirm_now, source, basis)


def _request(cur, target, by, kind, urgency, note, confirm_now, source="manual", basis="manual") -> int:
    cur.execute("select pg_advisory_xact_lock(hashtext('swap.request'))")
    act = active(cur)
    if act:
        raise SwapError(f"入れ替え {act['id']}（{act['target']}、{STATUS_JA[act['status']]}）が終わっていない。終わるか取り消してから")
    if target != "main" and rolled_back_before(cur, target):
        raise SwapError(f"{target} の入れ替えは前の版に戻った。直した新しい版で依頼する（同じ版では自動で入れ替えない。決定 6）")
    cur.execute("insert into events(name, payload) values ('swap.requested', %s) returning id",
                (Jsonb({"target": target, "kind": kind, "urgency": urgency, "by": by, "note": note[:300], "source": source}),))
    sid = cur.fetchone()["id"]
    cur.execute("update events set correlation_id=%s, payload = payload || %s where id=%s", (corr(sid), Jsonb({"swap_id": sid}), sid))
    if confirm_now:
        record(cur, sid, "swap.confirmed", {"at": datetime.now(timezone.utc).isoformat(), "by": by, "basis": basis})
    return sid


def cancel(cur, sid: int, by: str, reason: str = "") -> bool:
    """始まる前（入れ替え役を起動する前）だけ取り消せる"""
    s = get(cur, sid)
    if not s or s["status"] not in ("requested", "confirmed"):
        return False
    record(cur, sid, "swap.cancelled", {"by": by, "reason": reason[:300]})
    return True


def get(cur, sid: int) -> dict | None:
    cur.execute("select id, occurred_at, payload from events where name='swap.requested' and id=%s", (sid,))
    r = cur.fetchone()
    if not r:
        return None
    cur.execute("select id, name, occurred_at, payload from events where correlation_id=%s and name like 'swap.%%' order by id", (corr(sid),))
    evs = [dict(e) for e in cur.fetchall()]
    last = evs[-1] if evs else {"name": "swap.requested", "occurred_at": r["occurred_at"]}
    p = r["payload"]
    conf = next((e for e in reversed(evs) if e["name"] == "swap.confirmed"), None)
    return {"id": sid, "target": p.get("target"), "kind": p.get("kind"), "urgency": p.get("urgency"), "by": p.get("by"), "source": p.get("source") or "manual",
            "requested_at": r["occurred_at"],
            "status": STATUS[last["name"]], "last_at": last["occurred_at"], "at": (conf or {}).get("payload", {}).get("at"), "events": evs}


def active(cur) -> dict | None:
    """終わっていない入れ替え（あれば 1 件）"""
    cur.execute("select r.id from events r where r.name='swap.requested' and not exists "
                "(select 1 from events t where t.correlation_id = 'swap:' || r.id and t.name = any(%s)) order by r.id limit 1", (list(TERMINAL),))
    r = cur.fetchone()
    return get(cur, r["id"]) if r else None


def recent(cur, limit: int = 10) -> list[dict]:
    cur.execute("select id from events where name='swap.requested' order by id desc limit %s", (limit,))
    return [get(cur, r["id"]) for r in cur.fetchall()]


def rolled_back_before(cur, sha: str) -> bool:
    cur.execute("select 1 from events where name='swap.rolled_back' and (payload->>'target_sha' like %s or %s like (payload->>'target_sha') || '%%') limit 1",
                (sha + "%", sha))
    return cur.fetchone() is not None


# ------------------------------------------------------------------ 変更の種類と人に回す条件（決定 5・7）
def load_rules(root: str) -> dict:
    try:
        with open(os.path.join(root, DEFINITIONS)) as f:
            return {**DEFAULT_RULES, **(yaml.safe_load(f) or {})}
    except FileNotFoundError:
        return DEFAULT_RULES


def heavier(a: str, b: str) -> str:
    return max(a, b, key=KINDS.index)


def classify(files: list[str], rules: dict) -> str:
    """差分のファイルから種類を判定する。当たったものでいちばん重い種類。どれにも当たらなければ code"""
    kind = "code"
    for rule in rules.get("kinds") or []:
        pats = [glob_re(p) for p in rule["paths"]]
        if any(p.match(f) for f in files for p in pats):
            kind = heavier(kind, rule["kind"])
    return kind


def escalations(files: list[str], diff_of, rules: dict) -> list[dict]:
    """人に回す条件のうち、差分で見られるもの。diff_of(path) はそのファイルの差分（unified diff）"""
    out = []
    for rule in rules.get("escalate") or []:
        pats = [glob_re(p) for p in rule["paths"]]
        hit = [f for f in files if any(p.match(f) for p in pats)]
        if not hit:
            continue
        pat = re.compile(rule["diff_pattern"]) if rule.get("diff_pattern") else None
        if pat:
            hit = [f for f in hit if pat.search(diff_of(f) or "")]
        if hit:
            out.append({"id": rule["id"], "why": rule.get("why") or rule["id"], "files": hit})
    return out


# ------------------------------------------------------------------ 入れ替え役の起動（dispatcher の周回）
def _docker(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def container(sid: int) -> str:
    return f"{CONTAINER_PREFIX}{sid}"


def alive(sid: int) -> bool:
    return _docker(["container", "inspect", container(sid)]).returncode == 0


def host_root(project: str = "factory") -> str:
    """工場のリポジトリの作業ツリー（ホストの絶対パス）。compose が付けるラベル（orchestrator/ のパス）の 1 つ上。SWAP_ROOT で上書きできる"""
    if ENV.get("SWAP_ROOT"):
        return ENV["SWAP_ROOT"].rstrip("/")
    out = _docker(["ps", "-q", "--filter", f"label=com.docker.compose.project={project}", "--filter", "label=com.docker.compose.service=dispatcher"])
    ids = out.stdout.split()
    if not ids:
        raise SwapError("dispatcher のコンテナが見つからない（作業ツリーの場所が分からない）")
    wd = _docker(["inspect", "--format", '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}', ids[0]]).stdout.strip()
    if not wd.startswith("/"):
        raise SwapError("compose の作業ディレクトリのラベルがない")
    return os.path.dirname(wd.rstrip("/"))


def git_mounts(dotgit: str, root: str) -> list[str]:
    """作業ツリーの .git がファイル（git worktree）なら、指している git の置き場もマウントする。
    gitdir: <共通の置き場>/worktrees/<名前> の形なら共通の置き場ごと、そうでなければ gitdir をそのまま"""
    dotgit = dotgit.strip()
    if not dotgit.startswith("gitdir:"):
        return []   # 普通のクローン（.git がディレクトリ）。作業ツリーのマウントに入っている
    path = dotgit.split(":", 1)[1].strip()
    if not path.startswith("/"):
        path = os.path.normpath(os.path.join(root, path))
    common = path.split("/worktrees/")[0] if "/worktrees/" in path else path
    return [] if common.startswith(root + "/") else [common]


def launch_args(sid: int, root: str, extra_mounts: list[str], image: str, network: str, project: str) -> list[str]:
    """入れ替え役のコンテナの docker run の引数。作業ツリーは同じ絶対パスでマウントする（compose の相対パスがホストでそのまま効く）"""
    args = ["run", "-d", "--rm", "--name", container(sid), "--label", f"{LABEL}={sid}", "--network", network,
            "-v", "/var/run/docker.sock:/var/run/docker.sock", "-v", f"{root}:{root}"]
    for m in extra_mounts:
        args += ["-v", f"{m}:{m}"]
    # 値を書かない -e は dispatcher の環境から写す（停止の通知に GH_TOKEN を使う。入れ替え役は子のプロセスには渡さない）
    args += ["-w", f"{root}/orchestrator", "-e", f"SWAP_ROOT={root}", "-e", f"COMPOSE_PROJECT={project}"]
    for k in ("DATABASE_URL", "GH_TOKEN", "NOTIFIER", "STATUS_REPO", "STATUS_NUMBER", "REPORT_TZ", "FACTORY_MODE", "MODE_SCHEDULE"):
        args += ["-e", k]
    args += [image, "python", "/app/swapper.py", "run", str(sid)]
    return args


def launch(sid: int) -> str:
    project = ENV.get("COMPOSE_PROJECT", "factory")
    image = ENV.get("SWAP_IMAGE", "factory-dispatcher:p0")
    root = host_root(project)
    extra = [m for m in ENV.get("SWAP_EXTRA_MOUNTS", "").split(",") if m.strip()]
    probe = _docker(["run", "--rm", "-v", f"{root}:{root}:ro", "--entrypoint", "cat", image, f"{root}/.git"])   # .git がディレクトリなら失敗する（それでよい）
    if probe.returncode == 0:
        extra += git_mounts(probe.stdout, root)
    r = _docker(launch_args(sid, root, extra, image, ENV.get("SWAP_NETWORK", ENV.get("JOB_DB_NETWORK", f"{project}_default")), project))
    if r.returncode != 0:
        raise SwapError((r.stderr or r.stdout).strip()[-500:])
    return container(sid)


def tick(cur, log=print, *, launch_fn=launch, alive_fn=alive, now: datetime | None = None) -> None:
    """dispatcher の周回。受けた依頼の時刻になったら入れ替え役を起動する。起動した入れ替え役が結果を書かずに消えていたら失敗にして人に回す"""
    import stoppage
    s = active(cur)
    if not s:
        return
    now = now or datetime.now(timezone.utc)
    sid = s["id"]
    if s["status"] == "confirmed":
        at = datetime.fromisoformat(s["at"]) if s["at"] else now
        if at > now:
            return
        try:
            name = launch_fn(sid)
        except Exception as e:  # noqa: BLE001
            record(cur, sid, "swap.failed", {"error": f"入れ替え役を起動できなかった: {e}"[:500]})
            log("swap launch failed", sid, repr(e))
            return
        record(cur, sid, "swap.launched", {"container": name})
        log("swap launched", sid, name)
        return
    if s["status"] == "requested":
        return   # 時刻の合意待ち（順番 4）
    if (now - s["last_at"]).total_seconds() < LOST_AFTER_SEC or alive_fn(sid):
        return
    record(cur, sid, "swap.failed", {"error": f"入れ替え役が結果を書かずに終わった（最後は {STATUS_JA[s['status']]}）"})
    stoppage.open_stoppage(cur, f"swap:{sid}", "human", f"工場の入れ替え {sid}（{s['target']}）の入れ替え役が途中で消えた。工場の版が中途半端かもしれない",
                           waiting_for="factoryctl version で版を確かめ、手で上げ直すか前の版に戻す", correlation_id=corr(sid))
    log("swap lost", sid)


def summary(s: dict) -> str:
    last = s["events"][-1]["payload"] if s["events"] else {}
    detail = last.get("error") or last.get("reason") or ""
    tgt = s["target"] + (f"（{last['target_sha']}）" if last.get("target_sha") and s["target"] == "main" else "")
    return f"{s['id']:>5} {s['requested_at']:%m-%d %H:%M} {tgt:<20} {s['kind']:<13} {STATUS_JA[s['status']]}{' — ' + detail if detail else ''}"


def event_lines(s: dict) -> list[str]:
    return [f"{e['occurred_at']:%H:%M:%S} {e['name']:<22} {json.dumps({k: v for k, v in e['payload'].items() if k != 'swap_id'}, ensure_ascii=False)[:400]}"
            for e in s["events"]]
