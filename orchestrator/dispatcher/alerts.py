"""操業コンソールの停止アラート（2026-11-08 オーナー）。開いている停止（stoppages）を、読む人が「何が止まっていて、どれのことで、
どこで詳しく見て、次に何をすればよいか」がわかる形にする。

- 停止の記録（stoppage.opened の what / waiting_for）は工場の内側の言葉で書かれていて、事業主が読んでもわからない。
  記録は変えずに、ここで停止の種類（key の頭）と関係する記録（ジョブ・チケット・PR・更新）から読む文を組み立てる
- 同じ文が並んで見えたのは、失敗したジョブ 1 件ごとに停止を開いていて、どのチケットのジョブかを出していなかったから。対象を必ず添える
- アラートは画面から消せる（alert.dismissed）。消しても停止そのものは閉じない（番人と factoryctl が閉じる）。消したものは一覧で見られ、戻せる
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from psycopg.types.json import Jsonb

import usage

CAT_JA = {"env": "実行環境", "perm": "権限", "human": "判断待ち", "factory-bug": "工場の不具合", "ticket": "チケット",
          "external": "外部", "budget": "予算・上限", "idle": "ラインが空"}
WHO_JA = {"owner": "オーナー", "builder": "建設者", "none": "対応は要らない"}
# 並べる順（人の出番を先に、待てば戻るものを後に）
GROUPS = ("判断待ち", "工場の更新", "ライン", "工場の不調", "PR が進めない", "作業の失敗", "予算・上限", "そのほか")
# 工場の更新を人に回す決まり（definitions/swap.yaml の escalate の id）を、エンジニアでなくても読める言葉に
SWAP_ESCALATE_JA = {"secrets_permissions": "工場の守りに関わる設定（パスワードや鍵の扱い、外からつながる入口）が変わる"}
PR_RE = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
OLD_WORDS = (("入れ替え", "更新"),)   # 古い記録の言葉（11-07 に「入れ替え」は「更新」に統一）


def _plain(s: str | None) -> str:
    s = s or ""
    for a, b in OLD_WORDS:
        s = s.replace(a, b)
    return s


def _iso(t) -> str | None:
    return t.isoformat() if hasattr(t, "isoformat") else t


def _pr(url: str) -> str:
    m = PR_RE.search(url or "")
    return f"PR #{m.group(2)}" if m else "PR"


def _ticket(cur, corr: str | None) -> dict:
    """correlation_id（github:owner/repo#123）からチケットの題と URL"""
    ref = (corr or ":").split(":", 1)[1]
    if not ref:
        return {}
    cur.execute("select title, url, state from tickets where source='github' and source_ref=%s", (ref,))
    t = cur.fetchone() or {}
    repo, _, n = ref.partition("#")
    url = t.get("url") or (f"https://github.com/{repo}/issues/{n}" if n.isdigit() else "")
    return {"ref": ref, "title": t.get("title") or "", "url": url, "state": t.get("state")}


def _target(t: dict) -> dict | None:
    if not t:
        return None
    return {"label": f"{t['title']}（{t['ref']}）" if t.get("title") else t["ref"], "url": t.get("url") or ""}


def _job(cur, s: dict, a: dict) -> None:
    """失敗したジョブの停止（job:<id>）"""
    jid = s.get("job_id") or int(s["key"].split(":", 1)[1])
    cur.execute("select id, kind, attempt, error, result, correlation_id from jobs where id=%s", (jid,))
    j = cur.fetchone() or {"id": jid, "kind": "", "attempt": 1, "error": s.get("cause"), "result": None, "correlation_id": s.get("correlation_id")}
    role = usage.KIND_JA.get(j["kind"], j["kind"] or "工場")
    err = j.get("error") or s.get("cause") or ""
    cur.execute("select payload from events where name='job.finished' and job_id=%s order by id desc limit 1", (jid,))
    fin = (cur.fetchone() or {}).get("payload") or {}
    t = _ticket(cur, j.get("correlation_id") or s.get("correlation_id"))
    pr_url = ((j.get("result") or {}).get("pr_url")) or ""
    a.update(group="作業の失敗", who="builder", job_id=jid, target=_target(t))
    rerun = {"type": "rerun", "job_id": jid, "label": "やり直す"}
    if fin.get("usage_limited"):
        a.update(title=f"{role}の作業が、契約の使用量の上限に当たって止まった",
                 todo="使用量の上限が戻ってから「やり直す」を押す（戻る時刻は使用量のページで見られる）", actions=[rerun])
    elif err == "timeout":
        a.update(title=f"{role}の作業が時間の上限で打ち切られた",
                 todo="作業の記録の最後を見る。作業が大きすぎるならチケットを分ける。そうでなければ「やり直す」を押す", actions=[rerun])
    elif err.startswith("incomplete"):
        a.update(title=f"{role}の作業が途中で終わった（下書きの PR が残っている）",
                 todo="「続きからやり直す」を押す。続きも途中で終わったら、プランナーにチケットを分けてもらう（建設者に頼む）",
                 actions=[{**rerun, "continue": True, "label": "続きからやり直す"}])
    elif err == "unresolved_after_max_rounds":
        a.update(title="要求がはっきりしないまま、質問のやり取りの上限に達した", who="owner",
                 todo="チケットに要求を書き足してから「やり直す」を押す", actions=[rerun])
    elif err == "lock_held_timeout":
        a.update(title=f"{role}の作業が、同じチケットの別の作業が終わるのを待ちきれなかった",
                 todo="先の作業が終わっていれば「やり直す」を押す", actions=[rerun])
    else:
        a.update(title=f"{role}の作業が失敗した（原因はまだ分けられていない）",
                 todo="「作業の記録」で失敗の原因を見る。通信や GitHub の一時的な失敗なら「やり直す」を押す。同じ失敗が続くなら工場の不具合なので建設者に回す",
                 actions=[rerun])
    a["detail"] = [{"label": "作業", "text": f"{role}（ジョブ {jid}、{j.get('attempt') or 1} 回目）"},
                   {"label": "失敗の記録", "text": (f"{err}（作業のプログラムが失敗の印を返して終わった）" if err.startswith("exit=") else err) or "なし"}]
    a["links"] = ([{"label": _pr(pr_url) + " を開く", "href": pr_url}] if pr_url else []) + [{"label": "作業の記録（最後の部分）", "log": jid}]


def _judge(cur, s: dict, a: dict, url: str) -> None:
    import judgment
    try:
        es = judgment.escalations(cur, url)
    except Exception:  # noqa: BLE001  記録の形が古くても一覧は出す
        es = []
    t = _ticket(cur, s.get("correlation_id"))
    a.update(group="判断待ち", who="owner", target=_target(t) or {"label": _pr(url), "url": url},
             title=f"{_pr(url)} で、工場が決めきれなかった点の判断を待っている")
    if es:
        a["todo"] = f"受信トレイで問いに答える（{len(es)} 件）。答えると工場が続きを進める"
        a["links"] = [{"label": "受信トレイで答える", "href": f"/inbox/judgment:{es[0]['id']}", "nav": True}, {"label": _pr(url) + " を開く", "href": url}]
    else:
        a["todo"] = "問いは回答済。工場が判定の続きを進めるのを待っている（しばらく消えなければ建設者に回す）"
        a["links"] = [{"label": _pr(url) + " を開く", "href": url}]


def _swap(cur, s: dict, a: dict, sid: str) -> None:
    cur.execute("select payload from events where correlation_id=%s and name='swap.escalated' order by id desc limit 1", (f"swap:{sid}",))
    esc = (cur.fetchone() or {}).get("payload") or {}
    cur.execute("select payload->>'target' as target from events where name='swap.requested' and id=%s", (int(sid) if sid.isdigit() else -1,))
    target = (cur.fetchone() or {}).get("target") or ""
    ver = esc.get("short") or target[:7]
    reasons = [SWAP_ESCALATE_JA.get(e.get("id"), "確認が要る決まりの変更") for e in esc.get("escalations") or []]
    files = sorted({f for e in esc.get("escalations") or [] for f in e.get("files") or []})
    a.update(group="工場の更新", who="owner", target={"label": f"更新の依頼 {sid}", "url": ""},
             title=f"工場の更新（版 {ver or '不明'}）を自動では進めず、確認に回した",
             todo="変更の中身を確かめてから、手で更新する。わからなければ建設者に「更新 " + sid + " を確かめて」と頼む")
    if reasons:
        a["detail"] = [{"label": "確認に回した理由", "text": "、".join(dict.fromkeys(reasons))}, {"label": "変わるファイル", "text": "、".join(files[:8])}]
    else:
        a["detail"] = [{"label": "確認に回した理由", "text": _plain(s.get("cause") or s.get("what"))}]
    a["links"] = [{"label": "受信トレイで見る", "href": f"/inbox/stoppage:{s['key']}", "nav": True}]


def describe(cur, s: dict) -> dict:
    """開いている停止 1 件を、読む文に組み立てる。s は stoppages の行（key, category, what, waiting_for, cause, started_at, correlation_id, job_id）"""
    key = s["key"]
    prefix, _, rest = key.partition(":")
    url = rest if rest.startswith("https://") else ""
    a = {"key": key, "category": s["category"], "category_ja": CAT_JA.get(s["category"], s["category"]), "started_at": _iso(s.get("started_at")),
         "group": "そのほか", "who": "builder", "title": _plain(s.get("what")), "todo": _plain(s.get("waiting_for")), "target": None,
         "detail": [], "links": [], "actions": [], "job_id": None,
         "record": {"what": s.get("what") or "", "waiting_for": s.get("waiting_for") or ""}}
    try:
        if prefix == "job":
            _job(cur, s, a)
        elif prefix == "judge" and url:
            _judge(cur, s, a, url)
        elif prefix == "swap":
            _swap(cur, s, a, rest)
        elif prefix in ("merge", "sendback", "ci", "inspect", "hold"):
            if not url:
                m = PR_RE.search(s.get("what") or "")
                url = m.group(0) if m else ""
            t = _ticket(cur, s.get("correlation_id"))
            pr = _pr(url) if url else "PR"
            a.update(group="PR が進めない" if prefix != "merge" else "判断待ち", target=_target(t) or ({"label": pr, "url": url} if url else None))
            if prefix == "merge":
                a.update(who="owner", title=f"{pr} がマージを待っている（工場が自分でマージしない決まりの変更）",
                         todo=f"{pr} を見て、マージするか差し戻す")
            elif prefix == "sendback":
                a.update(title=f"{pr} の検査の指摘を、自動では直しに回さなかった", todo="建設者が、直すか差し戻すかを決める")
            elif prefix == "ci":
                conflict = "衝突" in (s.get("what") or "")
                a.update(title=f"{pr} が" + ("ほかの変更とぶつかって（衝突）先に進めない" if conflict else "自動テスト（CI）に落ちて先に進めない"),
                         todo="工場が自分で直せなかった。建設者が直すか、差し戻す")
            elif prefix == "inspect":
                a.update(group="工場の不調", title=f"{pr} の検査が始まらず、マージに進めない（工場の不具合）",
                         todo="工場が自分で積み直すことがある。しばらく消えなければ建設者に回す")
            else:   # hold: ほかの PR を待つ保留（人が記録したもの）
                a.update(group="PR が進めない")
            if url:
                a["links"] = [{"label": pr + " を開く", "href": url}]
            if prefix in ("merge", "sendback"):
                a["links"].insert(0, {"label": "受信トレイで見る", "href": f"/inbox/stoppage:{key}", "nav": True})
        elif prefix == "idle":
            a.update(group="ライン", who="owner", title="ラインが空（流す作業がない）",
                     todo="次のチケットを積む（窓口かプランナーに頼む）か、承認待ちの PR を判断する")
        elif prefix == "budget":
            a.update(group="予算・上限", who="none", title="その日の予算の枠を使い切ったので、新しい作業を始めていない",
                     todo="何もしなくてよい。次の枠に入ると工場が自分で再開する")
        elif prefix == "drain":
            a.update(group="ライン", who="builder", title="ラインが一時停止のまま",
                     todo="工場の更新が済んでいれば、画面右上の「ライン」のスイッチで再開する",
                     links=[{"label": "受信トレイで見る", "href": f"/inbox/stoppage:{key}", "nav": True}])
        elif prefix == "heartbeat":
            a.update(group="工場の不調", who="owner", title="工場長（supervisor）からの連絡が途絶えている",
                     todo="工場を動かしている Mac がスリープや停止をしていないか確かめる")
        elif prefix in ("stuck", "stall"):
            a.update(group="工場の不調", job_id=int(rest) if rest.isdigit() else None,
                     title=f"作業（ジョブ {rest}）が" + ("時間の上限を過ぎても終わらない" if prefix == "stuck" else "しばらく何も出力していない"),
                     todo="ラインの「作業中」でそのジョブを「止める」。止めたあと必要ならやり直す",
                     links=[{"label": "ラインで見る", "href": "/line", "nav": True}] + ([{"label": "作業の記録（最後の部分）", "log": int(rest)}] if rest.isdigit() else []))
            t = _ticket(cur, s.get("correlation_id"))
            a["target"] = _target(t)
        elif prefix == "prwait" and url:   # 判定に進まない PR（#308 の追加）
            t = _ticket(cur, s.get("correlation_id"))
            a.update(group="PR が進めない", who="builder", target=_target(t) or {"label": _pr(url), "url": url},
                     title=f"{_pr(url)} がマージの判定に進まず、同時に流す枠をふさいでいる",
                     todo="建設者が PR の必須のチェックと状態を確かめ、CI の再実行・作り直し・差し戻しのどれかをする",
                     links=[{"label": _pr(url) + " を開く", "href": url}])
        elif prefix == "slot":
            a.update(group="ライン", who="none", title="同時に流せる枠が開いた PR でふさがっていて、新しい作業を待っている",
                     todo="何もしなくてよい。PR が進まないときは「判定に進まない PR」の件が別に出る")
        elif prefix == "repeat":   # 繰り返すエラーを束ねた停止（repeat_watch.py、#308）
            a.update(group="工場の不調", who="builder",
                     todo=("工場を動かしている機械の不調（名前解決・接続）。Docker の VM のディスク・メモリを確かめ、続くなら再起動する。収まれば自動で閉じる"
                           if rest.startswith("env:") else
                           "建設者が原因を調べて直す。工場は止まらずに回っているが、この工程は同じところで落ち続けている"),
                     links=[{"label": "受信トレイで見る", "href": f"/inbox/stoppage:{key}", "nav": True}])
        elif prefix == "ship":
            a.update(group="工場の不調", title=_plain(s.get("what")).replace("出荷の失敗", "出荷に失敗した"),
                     todo="出荷のページで状態を見て、もう一度出すか前の版に戻す", links=[{"label": "出荷を開く", "href": "/ship", "nav": True}])
    except Exception as e:  # noqa: BLE001  1 件の組み立ての失敗で一覧を止めない（記録の文のまま出す）
        a["detail"].append({"label": "組み立てに失敗", "text": repr(e)[:200]})
    if s.get("cause") and prefix not in ("job", "swap"):
        a["detail"].append({"label": "原因の記録", "text": _plain(s["cause"])})
    return a


def dismissed(cur) -> dict[str, dict]:
    """消したアラート（key → 消した記録）。消したあと戻したものは入らない"""
    cur.execute("select distinct on (payload->>'key') payload->>'key' as key, name, occurred_at, payload->>'by' as by from events "
                "where name in ('alert.dismissed','alert.restored') order by payload->>'key', id desc")
    return {r["key"]: {"at": _iso(r["occurred_at"]), "by": r["by"]} for r in cur.fetchall() if r["name"] == "alert.dismissed"}


def set_dismissed(cur, key: str, on: bool, by: str) -> bool:
    cur.execute("select 1 from stoppages where key=%s", (key,))
    if cur.fetchone() is None:
        return False
    cur.execute("insert into events(name, payload) values (%s, %s)", ("alert.dismissed" if on else "alert.restored", Jsonb({"key": key, "by": by})))
    return True


def view(cur, now: datetime | None = None) -> dict:
    """開いている停止のアラート。open は画面に出すもの、dismissed は消したもの（停止は開いたまま）"""
    now = now or datetime.now(timezone.utc)
    cur.execute("select key, category, what, waiting_for, cause, started_at, correlation_id, job_id from stoppages where ended_at is null order by started_at")
    rows = cur.fetchall()
    gone = dismissed(cur)
    out = {"open": [], "dismissed": []}
    for s in rows:
        a = describe(cur, s)
        a["who_ja"] = WHO_JA.get(a["who"], a["who"])
        a["inbox"] = s["category"] == "human"   # 人の判断待ちの停止は受信トレイにも出る。画面は「判断待ち」に寄せ、「工場の不調」には出さない（11-08）
        st = s.get("started_at")
        a["age_sec"] = int((now - st).total_seconds()) if hasattr(st, "tzinfo") and st and st.tzinfo else None
        if s["key"] in gone:
            a["dismissed_at"] = gone[s["key"]]["at"]
            out["dismissed"].append(a)
        else:
            out["open"].append(a)
    order = {g: i for i, g in enumerate(GROUPS)}
    for k in out:
        out[k].sort(key=lambda a: (order.get(a["group"], len(GROUPS)), a["started_at"] or ""))
    return out
