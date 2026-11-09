"""差し戻し（ADR 0006）の停止 sendback:<元 PR> を、人が直しの Issue を流した時点で閉じる。

attended の差し戻しは、直しの Issue を factory:fix-proposed で起票し、人がラベルを factory:planned に替えて流す。
直しの Issue は「依存: #元 Issue」で元 PR のマージを待つ。一方、同じ元 PR で差し戻しを流さなかった停止（sendback:）が開いている間は
supervisor が元 PR の衝突を解かず（awaiting_decision）、マージにも進めない。停止は PR が閉じたときにしか閉じなかったので、
元 PR が main と衝突していると「直しは元 PR のマージ待ち、元 PR は停止待ち」で循環して止まった（2026-11-09 rally #1228/#1234・#1251/#1254、19 時間以上）。

人がラベルを替えたことを「直す」の判断とみなし、停止を閉じる。受信トレイからも同じ判断（流す・受け入れる）を答えられるようにする
（流すは supervisor がラベルを替える。受信トレイの入口は GitHub のトークンを持たない）。
"""
from __future__ import annotations

import urllib.parse

import stoppage

FIX_PROPOSED, PLANNED = "factory:fix-proposed", "factory:planned"


def fix_issue(cur, pr_url: str) -> str | None:
    """元 PR の直近の差し戻しの Issue（"owner/repo#n"）。起票していなければ None"""
    cur.execute("select payload->>'fix_corr' as c from events where name='sendback.filed' and payload->>'origin_pr'=%s order by id desc limit 1", (pr_url,))
    ref = ((cur.fetchone() or {}).get("c") or "").removeprefix("github:")
    return ref or None


def fixing(cur, pr_url: str) -> dict | None:
    """差し戻しの Issue を直しに回した（factory:planned を付けて台帳で投入待ち・実装中になった）なら、その Issue と時刻"""
    ref = fix_issue(cur, pr_url)
    if not ref:
        return None
    cur.execute("select source_ref, state, updated_at from tickets where source='github' and source_ref=%s and state in ('planned','active') limit 1", (ref,))
    t = cur.fetchone()
    if not t:
        return None
    return {"ref": ref, "number": ref.rsplit("#", 1)[-1], "state": t["state"], "at": t["updated_at"]}


def release_if_fixing(cur, pr_url: str, corr: str | None = None) -> bool:
    """停止 sendback:<元 PR> が開いていて、差し戻しの Issue が直しに回っていれば閉じる。閉じたら True。
    閉じると、元 PR の衝突の解消とマージの判定が再開し、元 Issue が閉じて差し戻しの Issue が依存待ちから外れる"""
    key = f"sendback:{pr_url}"
    if not stoppage.open_keys(cur, key):
        return False
    f = fixing(cur, pr_url)
    if not f:
        return False
    closed = stoppage.close_stoppage(cur, key, remedy=f"差し戻しの Issue #{f['number']} を直しに回した（ラベルを {PLANNED} に替えた）。元 PR の衝突の解消とマージに進める",
                                     closed_by="supervisor", woken_by="human")
    if closed:
        _record(cur, "sendback.released", corr, {"origin_pr": pr_url, "fix_issue": f["ref"], "by": "label"})
    return closed


def request_flow(cur, pr_url: str, by: str) -> tuple[bool, str]:
    """受信トレイの「流す」。ラベルの張り替えは GitHub のトークンを持つ supervisor が次の周期に行う（apply_flow）。
    受信トレイの入口（コンソール・窓口）は GitHub のトークンを持たないので、ここでは頼みを記録するだけ"""
    ref = fix_issue(cur, pr_url)
    if not ref:
        return False, "流す差し戻しの Issue がない（上限で起票しなかった）。受け入れるか、建設者が Issue を切る"
    _record(cur, "sendback.flow_requested", None, {"origin_pr": pr_url, "fix_issue": ref, "by": by})
    return True, f"差し戻しの Issue #{ref.rsplit('#', 1)[-1]} を直しに回す。次の周期にラベルを {PLANNED} に替えて停止を閉じ、元 PR の衝突の解消とマージに進める"


def apply_flow(cur, pr_url: str, gh) -> bool:
    """「流す」の頼みがあり停止が開いていれば、差し戻しの Issue のラベルを factory:fix-proposed から factory:planned に替える。
    停止は、Issue が台帳に写った（planned）周期に release_if_fixing が閉じる。GitHub のラベルを正本にし、工場の側に二重に持たない"""
    if not stoppage.open_keys(cur, f"sendback:{pr_url}"):
        return False
    cur.execute("select r.payload from events r where r.name='sendback.flow_requested' and r.payload->>'origin_pr'=%s "
                "and not exists (select 1 from events f where f.name='sendback.flowed' and f.payload->>'origin_pr'=%s and f.id > r.id) "
                "order by r.id desc limit 1", (pr_url, pr_url))
    req = cur.fetchone()
    if not req:
        return False
    ref = req["payload"]["fix_issue"]
    repo, n = ref.rsplit("#", 1)
    try:
        gh("POST", f"repos/{repo}/issues/{n}/labels", {"labels": [PLANNED]})
    except Exception as e:  # noqa: BLE001  次の周期にもう一度試す
        print("[sendback] flow label failed", ref, repr(e), flush=True)
        return False
    try:
        gh("DELETE", f"repos/{repo}/issues/{n}/labels/{urllib.parse.quote(FIX_PROPOSED, safe='')}")
    except Exception:  # noqa: BLE001  付いていなければ 404。planned が付けば流れる
        pass
    _record(cur, "sendback.flowed", None, {"origin_pr": pr_url, "fix_issue": ref, "by": req["payload"].get("by")})
    return True


def accept(cur, pr_url: str, by: str, note: str = "") -> tuple[bool, str]:
    """受信トレイの「受け入れる」: 残った指摘を直さずに先へ進める（停止を閉じる）。起票済みの差し戻しの Issue はそのまま残す"""
    key = f"sendback:{pr_url}"
    if not stoppage.close_stoppage(cur, key, remedy="残った指摘を受け入れて先へ進めた" + (f"（{note}）" if note else ""), closed_by="human", woken_by="human"):
        return False, "停止はもう閉じている"
    _record(cur, "sendback.accepted", None, {"origin_pr": pr_url, "by": by, "note": note})
    return True, "停止を閉じた。元 PR の衝突の解消とマージに進める"


def _record(cur, name: str, corr: str | None, payload: dict):
    from psycopg.types.json import Jsonb
    cur.execute("insert into events(name, correlation_id, payload) values (%s,%s,%s)", (name, corr, Jsonb(payload)))
