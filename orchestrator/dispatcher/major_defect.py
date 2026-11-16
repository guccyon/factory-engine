"""メジャー不具合の即時修正と再リリース（ADR 0053）。

メジャー不具合 = 利用者が先に進めない・業務が止まるレベルの不具合。QA 役の重要度 S1（止める）と S2（流れが exit まで通らない）がこれに当たる
（ADR 0053 の 1 の改訂。ラインの差し止めは今までどおり S1 だけ）。
工場は人の承認を待たずに、修正 → 再リリースまでを自分で進める。

- 起票: メジャーの Defect の Issue は factory:fix-proposed（人が流す 1 タップ）ではなく factory:plan にし、p0 を付ける（incident.from_qa から呼ぶ）。
  プランナーは着手判定だけをし、方針が要ればスパイクのレーン（System Architect）が原因と直し方を決め、同じ原因の不具合を束ねる（ADR 0055）
- 再リリース: メジャーの Incident の Defect の Issue が閉じ（修正の PR がマージされ）、まだ Release に入っていないマージ済みの PR があれば、
  supervisor が緊急便（train.emergency）を出す。Incident ごとに 1 回（major.rereleased）
- 本番の承認: その緊急便が S1 の修正を載せていて、本番を立てる段階の製品の Release が載っていれば、staging に出た後に supervisor が承認する
  （QA の関門の報告は待たない。release.approved の qa_skipped に理由が残る）。S2 だけの緊急便は staging まで出し、本番は QA の関門の報告と
  承認者の判断を待つ（ADR 0053 の追記 2026-11-14。緊急便はその時点の main をまるごと載せるので、S2 まで自動で本番に出すと
  ほかの変更も関門と承認を通らずに本番に出続ける）
- 直しだけの便（ADR 0064）: 本番に出ている版がある製品の S1 の緊急便は、main をまるごと載せず、本番の版のコミットに直しの PR のコミットだけを
  cherry-pick した版で出す（train.hotfix）。直しの PR は、その周回で閉じたメジャーの Incident の Defect の Issue に結びついたマージ済みの PR
  （pr.merged の correlation_id = github:<repo>#<Issue>）。見つからなければ、main をまるごと載せる便を本番の自動の承認なしで出す。
  cherry-pick がぶつかったら（release.ship_failed の hotfix_conflict）、fallback_step が直しだけの便を見送り、main をまるごと載せる便に切り替え、
  本番は QA の関門と承認者を待つ。どちらも受信トレイに承認者宛てのお知らせを出す（ADR 0064 の決定 2）
- 本番で見つかったメジャー不具合の revert（ADR 0053 の決定 4・ADR 0064 の決定 3）は次の PR で足す

LLM は使わない。失敗しても supervisor の周回は止めない（呼び出し側で握る）。
"""
from __future__ import annotations

from psycopg.types.json import Jsonb

import inbox
import incident
import ship
import train

MAJOR_SEVERITIES = ("s1", "s2")
AUTO_APPROVE_SEVERITIES = ("s1",)   # 工場が本番まで自分で承認する緊急便の重要度（ADR 0053 の追記 2026-11-14）
FIX_PROPOSED, PLAN = "factory:fix-proposed", "factory:plan"
REREL = "major.rereleased"
APPROVE_VIA = "メジャー不具合の即時再リリース（ADR 0053）"
FALLBACK = "major.hotfix_fallback"
FALLBACK_KIND = "hotfix_fallback"   # 受信トレイの見たら閉じるお知らせ（inbox.TASK_ACK）


def is_major(severity: str | None) -> bool:
    return severity in MAJOR_SEVERITIES


def issue_number(url: str | None) -> int | None:
    num = (url or "").rstrip("/").rsplit("/", 1)[-1].lstrip("#")
    return int(num) if num.isdigit() else None


def plan_fix(gh, repo: str, num: int) -> None:
    """メジャー不具合の Issue を人の 1 タップなしで流す（fix-proposed を外し、p0 とプランナーの着手判定を付ける）"""
    gh("POST", f"repos/{repo}/issues/{num}/labels", {"labels": [PLAN, incident.HOLD_PRIORITY_LABEL]})
    try:
        gh("DELETE", f"repos/{repo}/issues/{num}/labels/{FIX_PROPOSED.replace(':', '%3A')}")
    except Exception:  # noqa: BLE001, S110  付いていなければ 404
        pass


def rereleased(cur, iid: str) -> dict | None:
    cur.execute("select payload from events where name=%s and payload->>'incident_id'=%s limit 1", (REREL, iid))
    r = cur.fetchone()
    return r["payload"] if r else None


def issue_closed(gh, repo: str, num: int) -> bool:
    return (gh("GET", f"repos/{repo}/issues/{num}") or {}).get("state") == "closed"


def mark(cur, inc: dict, repo: str, tid: str) -> None:
    cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values (%s,%s,%s,%s) on conflict (dedupe_key) do nothing",
                (REREL, tid, Jsonb({"incident_id": inc["incident_id"], "repo": repo, "issue": inc.get("issue"), "train_id": tid}),
                 f"{REREL}:{inc['incident_id']}"))


def fix_prs(cur, repo: str, nums: list[int]) -> list[dict]:
    """Defect の Issue に結びついた、まだ Release に入っていないマージ済みの PR（マージの順）。直しだけの便に載せる（ADR 0064）"""
    if not nums:
        return []
    cur.execute("select payload->>'pr_url' as pr_url, payload->>'merge_commit' as merge_commit from events e where name='pr.merged' "
                "and correlation_id = any(%s) and not exists (select 1 from events r where r.name='release.included' "
                "and r.payload->>'pr_url'=e.payload->>'pr_url') order by payload->>'merged_at', id",
                ([f"github:{repo}#{n}" for n in nums],))
    return [dict(r) for r in cur.fetchall()]


def notice(cur, repo: str, key: str, title: str) -> None:
    inbox.request(cur, f"{FALLBACK_KIND}:{key}", FALLBACK_KIND, "po", title,
                  action="本番に出すかは、QA の関門の報告を見て受信トレイの承認で決める（直しだけでは出せなかった。ADR 0064）",
                  correlation_id=f"release:{key}", by="supervisor")


def _hotfix_or_emergency(cur, gh, repo: str, inc: dict, closed: list[dict], log=None) -> tuple[bool, str, str | None]:
    """S1 で本番に出ている版があれば直しだけの便、なければ今までの緊急便。返り値は (出したか, 文, 便の id)"""
    reason = f"メジャー不具合の修正: {inc.get('issue')}（{inc.get('title') or ''}）"
    base = ship.prod_base(cur, repo) if inc.get("severity") in AUTO_APPROVE_SEVERITIES else None
    if base is None:
        ok, msg = train.emergency(cur, repo, "supervisor", reason)
        return ok, msg, _last_emergency(cur, repo) if ok else None
    picks = fix_prs(cur, repo, [n for n in (issue_number(c.get("issue")) for c in closed) if n is not None])
    if picks:
        return train.hotfix(cur, repo, base, picks, "supervisor", reason)
    # 直しの PR が見つからない（別の Issue の PR でまとめて閉じたなど）: main をまるごと載せ、本番は自動で承認しない
    ok, msg = train.emergency(cur, repo, "supervisor", reason + "（直しの PR が見つからず、main をまるごと載せる）",
                              extra={"mode": "fallback", "auto_approve": False})
    tid = _last_emergency(cur, repo) if ok else None
    if ok:
        notice(cur, repo, tid, f"{repo} の S1 の直しを、直しだけの版で出せなかった（直しの PR が見つからない）。本番は QA の関門と承認を待つ")
    return ok, msg, tid


def _last_emergency(cur, repo: str) -> str:
    cur.execute("select payload->>'train_id' as tid from events where name='release.train' and payload->>'kind'='emergency' "
                "and payload->'repos' ? %s order by id desc limit 1", (repo,))
    return cur.fetchone()["tid"]


def rerelease_step(cur, gh, log=None) -> list[str]:
    """メジャーの Incident のうち、Defect の Issue が閉じたものを緊急便で出す。返り値は出した便の id"""
    out, shipped = [], {}
    pending: list[tuple[dict, str, int]] = []
    for inc in incident.open_incidents(cur):
        if not is_major(inc.get("severity")) or inc.get("drill") or rereleased(cur, inc["incident_id"]):
            continue
        repo, num = inc["repo"], issue_number(inc.get("issue"))
        if num is None or not issue_closed(gh, repo, num):
            continue
        pending.append((inc, repo, num))
    # 同じ製品で同じ周回に閉じたものは 1 つの便に載せる。S1 を先に見る（S1 があれば直しだけの便になる）
    pending.sort(key=lambda x: (x[0].get("severity") not in AUTO_APPROVE_SEVERITIES))
    for inc, repo, _num in pending:
        if repo in shipped:   # 同じ原因をまとめて直した PR で閉じたもの: この周回の便に載っている
            mark(cur, inc, repo, shipped[repo])
            continue
        ok, msg, tid = _hotfix_or_emergency(cur, gh, repo, inc, [i for i, r, _ in pending if r == repo], log)
        if not ok:
            if log:
                log("major rerelease waiting", inc["incident_id"], msg)
            continue
        mark(cur, inc, repo, tid)
        shipped[repo] = tid
        out.append(tid)
        if log:
            log("major rerelease", inc["incident_id"], msg)
    return out


def fallback_step(cur, log=None) -> list[str]:
    """直しだけの便の cherry-pick がぶつかったら（release.ship_failed の hotfix_conflict）、その便を見送り、
    main をまるごと載せる便に切り替える。本番は自動で承認せず、QA の関門と承認者を待つ（ADR 0064 の決定 2）。返り値は切り替えた便の id"""
    cur.execute("select t.payload as t, f.payload as f from events t join lateral (select payload from events f where f.name='release.ship_failed' "
                "and (f.payload->>'hotfix_conflict')::boolean and f.payload->>'release_id' = t.payload->'releases'->0->>'release_id' limit 1) f on true "
                "where t.name='release.train' and t.payload->>'mode'='hotfix' and not exists "
                "(select 1 from events x where x.name=%s and x.payload->>'hotfix_train'=t.payload->>'train_id')", (FALLBACK,))
    out = []
    for r in cur.fetchall():
        t, f = r["t"], r["f"]
        repo, hid = t["releases"][0]["repo"], t["train_id"]
        ok, msg = train.emergency(cur, repo, "supervisor", f"{t.get('reason') or ''}（直しだけの版にぶつかりがあり、main をまるごと載せる）",
                                  allow_empty=True, extra={"mode": "fallback", "auto_approve": False, "fallback_of": hid})
        if not ok:
            if log:
                log("hotfix fallback waiting", hid, msg)
            continue
        tid = _last_emergency(cur, repo)
        train.dismiss(cur, hid, "supervisor", "直しだけの版にぶつかりがあり、main をまるごと載せる便に切り替えた（ADR 0064 の決定 2）")
        cur.execute("insert into events(name, correlation_id, payload, dedupe_key) values (%s,%s,%s,%s) on conflict (dedupe_key) do nothing",
                    (FALLBACK, tid, Jsonb({"hotfix_train": hid, "train_id": tid, "repo": repo, "error": str(f.get("error") or "")[:500]}),
                     f"{FALLBACK}:{hid}"))
        notice(cur, repo, tid, f"{repo} の S1 の直しが本番の版にぶつかり、直しだけでは出せなかった。main をまるごと載せた {tid} は、本番を QA の関門と承認で決める")
        out.append(tid)
        if log:
            log("hotfix fallback", hid, msg)
    return out


def carries_auto_approved(cur, tid: str) -> bool:
    """その緊急便が、工場が本番まで承認する重要度（S1）の Incident の修正を載せているか"""
    cur.execute("select 1 from events r join events o on o.name='incident.opened' and o.payload->>'incident_id' = r.payload->>'incident_id' "
                "where r.name=%s and r.payload->>'train_id'=%s and o.payload->>'severity' = any(%s) limit 1",
                (REREL, tid, list(AUTO_APPROVE_SEVERITIES)))
    return cur.fetchone() is not None


def approve_step(cur, log=None) -> list[str]:
    """メジャー不具合の緊急便のうち、本番の承認が要り、まだ承認していないものを承認する（staging に出る前は次の周回で）"""
    cur.execute("select r.payload->>'train_id' as tid from events r where r.name=%s and not exists "
                "(select 1 from events a where a.name='release.train_approved' and a.payload->>'train_id' = r.payload->>'train_id')", (REREL,))
    out = []
    for row in cur.fetchall():
        t = train.train_of(cur, row["tid"])
        if not t or not t["approval"].get("required") or t.get("auto_approve") is False or not carries_auto_approved(cur, row["tid"]):
            continue
        staged = all(ship.status(cur, r["repo"])["environments"].get("staging", {}).get("release_id") == r["release_id"] for r in t["releases"])
        if not staged:
            continue
        ok, msg = train.approve(cur, row["tid"], "supervisor", APPROVE_VIA, skip_qa=APPROVE_VIA)
        if log:
            log("major approve", row["tid"], ok, msg)
        if ok:
            out.append(row["tid"])
    return out
