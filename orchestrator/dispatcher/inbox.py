"""受信トレイ（ADR 0026 の決定 7）。人が対応するものを制御プレーンから 1 つの一覧に集める。

操業コンソールのトップのベルとバッジ、受信トレイのパネル、通知（notify_inbox.py）が同じ一覧を読む。
一覧は毎回組み立て直し、表は持たない（状態は events にだけ置く。ADR 0020）。

集めるもの:
- stoppage: 開いている停止のうち分類が human のもの（マージ待ち・judge が人に回した・差し戻しを流さなかった・ラインの一時停止）
- choice: 開いている人の選択（HumanChoice。画面の案の選択・抜き取り）
- release: staging で確かめた版がまだ本番に出ていない（「本番に出す」を押すのは人。ADR 0012）
- task: 他の工程が request() で積んだ依頼（レビューページの承認待ち、着手判定で起票者に返した問いなど）。resolve() で消える

1 件の形: key（通知の重複を防ぐ一意の名前）・kind・who（誰の出番か）・title・action（何をすればよいか）・url・since・age_sec・ref
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from psycopg.types.json import Jsonb

# 誰の出番か（ADR 0026 の決定 6）。担当者と連絡先は Blueprint で決める。Gen1 はどれもオーナーで、builder は建設者が代理で受ける
WHO = {
    "owner": "オーナー",
    "po": "プロダクトオーナー",
    "design_chief": "デザインのチーフ",
    "builder": "建設者",
    "requester": "起票者",
}
CHOICE_TITLES = {"design_variants": "画面の体験の案の選択", "design_look": "画面の見た目の案の選択", "concept_tone": "コンセプトとトーン＆マナーの選択", "taste_sample": "抜き取り"}   # choice.KINDS と同じ
KINDS = {"stoppage": "人の判断待ちの停止", "choice": "人の選択", "release": "本番への出荷", "task": "依頼"}
PR_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")
KEY = re.compile(r"^[A-Za-z0-9_.:/#@-]{1,200}$")
MAX_LEN = 300


def _clip(v, n=MAX_LEN) -> str:
    return str(v or "")[:n]


def _iso(t) -> str | None:
    return t.isoformat() if isinstance(t, datetime) else t


def _age(t, now: datetime) -> int | None:
    return int((now - t).total_seconds()) if isinstance(t, datetime) else None


# ------------------------------------------------------------------ 他の工程から積む・消す
def request(cur, key: str, kind: str, who: str, title: str, *, url: str = "", action: str = "", correlation_id: str | None = None,
            by: str = "factory") -> bool:
    """受信トレイに依頼を積む。同じ key がすでに開いていれば何もしない（False）。kind は呼ぶ側の種類（review_page・ready_question など）"""
    if not KEY.match(key or ""):
        raise ValueError("key は英数字と _.:/#@- の 200 文字以内")
    if who not in WHO:
        raise ValueError(f"who は {sorted(WHO)} のどれか")
    if url and not url.startswith(("https://", "/")):
        raise ValueError("url は https:// か / で始まる")
    if key in _open_task_keys(cur, key):
        return False
    cur.execute("insert into events(name, correlation_id, payload) values ('inbox.requested', %s, %s)",
                (correlation_id, Jsonb({"key": key, "kind": _clip(kind, 40), "who": who, "title": _clip(title), "url": _clip(url, 500),
                                        "action": _clip(action), "by": _clip(by, 80)})))
    return True


def resolve(cur, key: str, by: str, outcome: str = "") -> bool:
    """依頼を消す（承認・直し・回答などで済んだ）。開いていなければ False"""
    if key not in _open_task_keys(cur, key):
        return False
    cur.execute("insert into events(name, payload) values ('inbox.resolved', %s)",
                (Jsonb({"key": key, "by": _clip(by, 80), "outcome": _clip(outcome)}),))
    return True


def _open_task_keys(cur, key: str | None = None) -> set[str]:
    cur.execute("select o.payload->>'key' as key from events o where o.name='inbox.requested' "
                "and (%(k)s::text is null or o.payload->>'key' = %(k)s) "
                "and not exists (select 1 from events r where r.name='inbox.resolved' and r.payload->>'key'=o.payload->>'key' and r.id > o.id)",
                {"k": key})
    return {r["key"] for r in cur.fetchall()}


# ------------------------------------------------------------------ 集める
STOPPAGE_WHO = {"drain": "builder", "sendback": "builder"}   # 入れ替えの再開と差し戻しの扱いは建設者。マージ・judge の判断はオーナー


def _stoppages(cur, now) -> list[dict]:
    cur.execute("select key, what, waiting_for, started_at, correlation_id from stoppages where ended_at is null and category='human' order by started_at")
    out = []
    for s in cur.fetchall():
        prefix, _, rest = s["key"].partition(":")
        url = rest if rest.startswith("https://") else ""
        if prefix == "merge" and s["correlation_id"]:
            cur.execute("select payload->>'pr_url' as u from events where name='supervisor.merge_decided' and correlation_id=%s order by id desc limit 1",
                        (s["correlation_id"],))
            r = cur.fetchone()
            url = (r or {}).get("u") or url
        if not url:   # 停止の文に PR の URL が入っていればそれを使う（マージ待ちの停止は what に入れている）
            m = PR_URL.search(s["what"] or "")
            url = m.group(0) if m else ""
        out.append({"key": f"stoppage:{s['key']}", "kind": "stoppage", "who": STOPPAGE_WHO.get(prefix, "owner"), "title": s["what"],
                    "action": s["waiting_for"] or "判断して止まりを解く", "url": url, "since": s["started_at"],
                    "ref": (s["correlation_id"] or ":").split(":", 1)[1], "announced": True})   # 開いたときに停止の通知で一度知らせている
    return out


def _choices(cur) -> list[dict]:
    cur.execute("select id, correlation_id, created_at, payload from artifacts where type='HumanChoice' and payload->>'status'='open' order by id")
    out = []
    for r in cur.fetchall():
        p = r["payload"]
        design = p.get("kind") in ("design_variants", "design_look")
        # 体験の承認は PO、見た目の判断はデザインのチーフ（ADR 0026 の 4・6。Gen1 はどちらもオーナーが兼ねる）
        who = "po" if p.get("kind") == "design_variants" else "design_chief" if p.get("kind") in ("design_look", "concept_tone") else "owner"
        out.append({"key": f"choice:{r['id']}", "kind": "choice", "who": who,
                    "title": f"{CHOICE_TITLES.get(p.get('kind'), '人の選択')}: " + _clip(p.get("question"), 200),
                    "action": "案を選ぶか、どれも違えば戻す" + (f"（推奨 {p['recommended']}）" if p.get("recommended") else ""),
                    "url": f"/#choice-{r['id']}", "since": r["created_at"], "ref": (r["correlation_id"] or ":").split(":", 1)[-1],
                    "uxspec": (p.get("kind"), (p.get("subject") or {}).get("uxspec")) if design else None})
    return out


def _releases(cur) -> list[dict]:
    import ship   # 出荷の状態の読み方は ship に 1 か所
    cur.execute("select distinct payload->>'repo' as repo from events where name='release.created' order by 1")
    out = []
    for repo in [r["repo"] for r in cur.fetchall()]:
        envs = ship.status(cur, repo)["environments"]
        st, pr = envs.get("staging") or {}, envs.get("prod") or {}
        rid = st.get("release_id")
        if not rid or rid == pr.get("release_id"):
            continue
        cur.execute("select occurred_at from events where name='release.deployed' and payload->>'release_id'=%s and payload->>'env'='staging' "
                    "order by id desc limit 1", (rid,))
        d = cur.fetchone()
        out.append({"key": f"release:{rid}", "kind": "release", "who": "owner", "title": f"{repo} の {rid.split('@')[-1]} が本番待ち",
                    "action": "staging で確かめて「本番に出す」を押す", "url": "/#ship", "since": d["occurred_at"] if d else None, "ref": repo})
    return out


def _tasks(cur) -> list[dict]:
    cur.execute("select o.occurred_at, o.correlation_id, o.payload from events o where o.name='inbox.requested' "
                "and not exists (select 1 from events r where r.name='inbox.resolved' and r.payload->>'key'=o.payload->>'key' and r.id > o.id) "
                "order by o.id")
    return [{"key": f"task:{r['payload']['key']}", "kind": "task", "task_kind": r["payload"].get("kind"), "who": r["payload"].get("who") or "owner",
             "title": r["payload"].get("title") or "", "action": r["payload"].get("action") or "", "url": r["payload"].get("url") or "",
             "since": r["occurred_at"], "ref": (r["correlation_id"] or ":").split(":", 1)[-1]} for r in cur.fetchall()]


def items(cur, now: datetime | None = None) -> list[dict]:
    """人が対応するもの。待ちの長い順。1 つの集め方が失敗しても残りは返す（受信トレイが空に見えるより一部でも出す）"""
    now = now or datetime.now(timezone.utc)
    out: list[dict] = []
    for collect in (lambda: _stoppages(cur, now), lambda: _choices(cur), lambda: _releases(cur), lambda: _tasks(cur)):
        try:
            out += collect()
        except Exception as e:  # noqa: BLE001
            out.append({"key": f"error:{type(e).__name__}", "kind": "error", "who": "builder", "title": "受信トレイの一部を読めなかった",
                        "action": repr(e)[:200], "url": "", "since": None, "ref": ""})
    # 画面の案の選択は、レビューページ（review_page.py）が同じ UXSpec の依頼を積んでいればそちらに任せる（二重に出さない）
    # 体験の案（design_variants）は task:design_review:<id>、見た目の案（design_look）は task:design_look:<id>
    prefix = {"task:design_review:": "design_variants", "task:design_look:": "design_look"}
    reviews = {(k, i["key"].removeprefix(pre)) for i in out for pre, k in prefix.items() if i["key"].startswith(pre)}
    out = [i for i in out if not (i["kind"] == "choice" and i.get("uxspec") and i["uxspec"] in reviews)]
    for i in out:
        i.pop("uxspec", None)
        i["age_sec"] = _age(i["since"], now)
    out.sort(key=lambda i: -(i["age_sec"] or 0))
    return out


def view(cur, now: datetime | None = None) -> dict:
    """操業コンソールの /api/inbox の形（JSON にできる値だけ）"""
    xs = items(cur, now)
    by_who: dict[str, int] = {}
    for i in xs:
        by_who[i["who"]] = by_who.get(i["who"], 0) + 1
    return {"count": len(xs), "by_who": by_who, "who": WHO, "kinds": KINDS,
            "items": [{**{k: v for k, v in i.items() if k != "announced"}, "since": _iso(i["since"])} for i in xs]}
