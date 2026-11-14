"""受信トレイ（ADR 0026 の決定 7）。人が対応するものを制御プレーンから 1 つの一覧に集める。

操業コンソールのトップのベルとバッジ、受信トレイのパネル、通知（notify_inbox.py）が同じ一覧を読む。
一覧は毎回組み立て直し、表は持たない（状態は events にだけ置く。ADR 0020）。

集めるもの:
- stoppage: 開いている停止のうち分類が human のもの（マージ待ち・judge が人に回した・差し戻しを流さなかった・ラインの一時停止）
- choice: 開いている人の選択（HumanChoice。画面の案の選択・抜き取り・実装後の画面のキャプチャ）
- release: staging で確かめた版がまだ本番に出ていない（「本番に出す」を押すのは人。ADR 0012）
- judgment: judge が人に回した判断（停止 judge: の PR の、人の判断がまだの問い 1 つずつ。今の案・他の案・見立てと推奨を detail に）
- task: 他の工程が request() で積んだ依頼（レビューページの承認待ち、着手判定で起票者に返した問いなど）。resolve() で消える

1 件の形: key（通知の重複を防ぐ一意の名前）・kind・who（誰の出番か）・title・action（何をすればよいか）・url・since・age_sec・ref・
detail（問いの中身。label と text の並び。任意）・answer（答え方。この場で答えられない件は None）・answered（答え済みで反映を待っている件は、誰が・どの入口から・何と答えたか）

答え（ADR 0034 の決定 1）: どの入口（操業コンソール・レビューページ・のちの Slack・GitHub・窓口 MCP）から来た答えも answer() の 1 か所を通る。
件の状態の正本は受信トレイで、先に answer() を通った答えが勝つ（同じ件への後からの答えは「答え済み」と返す）。
記録は events の inbox.answered（key・答えの形・誰が・どの入口から・元の文・確信度）。通知を送った先への書き戻しは notify_inbox が行う
"""
from __future__ import annotations

import os
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
CHOICE_TITLES = {"design_variants": "画面の体験の案の選択", "design_look": "画面の見た目の案の選択", "concept_tone": "コンセプトとトーン＆マナーの選択", "taste_sample": "抜き取り",
                 "exit_capture": "実装後の画面"}   # choice.KINDS と同じ
KINDS = {"stoppage": "対応待ち", "judgment": "判断待ち", "choice": "選択待ち", "release": "リリース承認待ち", "task": "作業依頼"}
PR_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")
KEY = re.compile(r"^[A-Za-z0-9_.:/#@-]{1,200}$")
MAX_LEN = 300
MODEL_TIER = os.environ.get("MODEL_TIER", "tier-large")   # 人が案を戻したときに積み直すデザイナー役のモデル（dispatcher と同じ値）

# 答え（ADR 0034 の決定 2）。type: choose（選択肢から 1 つ）/ approve（承認）/ ack（見た）/ text（自由記述。まだ使う件はない）
ANSWER_TYPES = ("choose", "approve", "ack", "text")
VIAS = ("console", "review_page", "factoryctl", "mcp", "github", "slack")   # 入口（ADR 0034 の決定 3）
EXTRA = {"return": {"id": "return", "label": "どれも違う（戻す）", "note_required": True},
         "flag": {"id": "flag", "label": "違和感の印", "note_required": True, "closes": False}}   # 印は件を閉じない（何度でも付けられる）
CHOICE_EFFECTS = {
    "design_variants": "選んだ体験の案で実装のタスクに切って投入する。戻すとコメントを直しの指摘にしてデザイナー役が作り直す",
    "design_look": "選んだ見た目の案を実装に反映する。戻すとコメントを直しの指摘にしてデザイナー役が作り直す",
    "concept_tone": "選んだコンセプトとトーン＆マナーでデザインシステムの線が進む。戻すと作り手に作り直しを頼む",
    "taste_sample": "選んだ案を内部品質の教師信号として残す。戻すと作り手に戻す",
}
TASK_ACK = {"swap_rolled_back": "見たとして閉じる（直した版で更新を依頼し直すのは建設者）",
            "desk_waiting": "見たとして閉じる（依頼は閉じない。依頼者に直接確かめ、答えは依頼者が窓口か受信トレイから返す）",
            "desk_notice_failed": "見たとして閉じる（知らせはもう送り直さない。依頼者には直接知らせる）",
            "opportunity": "見たとして閉じる（拾うなら「起票する」で答える）"}   # 見たら閉じてよい依頼
OPPORTUNITY_FILE = {"id": "file", "label": "起票する（プランナーへ）"}
OPPORTUNITY_EFFECT = "起票するなら工場がプランナー宛ての Issue（factory:plan）を作り、要否と経路はプランナーが決める。拾わないなら見たとして閉じる"
# 窓口の単発の依頼の確認と受取確認（ADR 0039 の決定 2・5）。答えは desk_line.answer に渡す。依頼者のほか、運用主（role なし）も答えられる
DESK_ANSWERS = {
    "desk_confirm": {"options": [{"id": "run", "label": "実行する"}, {"id": "cancel", "label": "やめる"}],
                     "effect": "実行するなら dispatcher が操作を 1 回動かして結果を提出する。やめるなら依頼を取り下げとして閉じる"},
    "desk_receipt": {"options": [{"id": "accept", "label": "受け取った"}],
                     "extra": [{"id": "more", "label": "続きを頼む", "note_required": True},
                               {"id": "return", "label": "差し戻す（頼んだことと違う）", "note_required": True}],
                     "effect": "受け取ったら依頼を完了にする。続き・差し戻しはコメントを添えて同じ会話で振り分けからやり直す"},
    "desk_decline": {"options": [{"id": "accept", "label": "納得した"}],
                     "extra": [{"id": "more", "label": "別案で進める（別の依頼として開く）"},
                               {"id": "return", "label": "断りを差し戻す", "note_required": True}],
                     "effect": "納得なら依頼を断りとして完了にする。別案なら代わりの案を別の依頼として開き、元の依頼と結ぶ。差し戻すと理由を添えて振り分けからやり直す"},
}   # desk_line の CONFIRM_OPTIONS・RECEIPT_OPTIONS・DECLINE_OPTIONS と同じ（食い違いはテストで見る）


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
# 更新の再開と差し戻しの扱い、ほかの PR を待つ保留（hold:）は建設者。マージ・judge の判断はオーナー
# （hold は人の判断が済んだあとの順番待ちで、オーナーのすることはない。11-02 rally #589 が「オーナーの出番」と出ていた）
STOPPAGE_WHO = {"drain": "builder", "sendback": "builder", "hold": "builder", "ci": "builder", "repeat": "builder"}


def _stoppages(cur, now) -> list[dict]:
    import alerts
    import workflow_guard
    # 人の停止に加えて、衝突の解消の上限に当たり工場がもう解かないと決めた PR（ci: の停止で conflict_human の記録があるもの）も拾う。
    # 種類が ticket なので載らず、建設日報でしか気づけなかった（11-11 rally #1117 が 39 時間止まっていた）
    cur.execute("select key, category, what, waiting_for, cause, started_at, correlation_id, job_id from stoppages "
                "where ended_at is null and (category='human' or key like 'ci:%' or key like 'repeat:%') order by started_at")
    out = []
    for s in cur.fetchall():
        prefix, _, rest = s["key"].partition(":")
        cap = None
        if s["category"] != "human" and prefix != "repeat":   # 繰り返すエラーを束ねた停止（#308）は建設者の件として載せる
            cap = workflow_guard.capped(cur, s["correlation_id"]) if s["correlation_id"] else None
            if not cap:
                continue   # 工場がまだ自分で直す CI 落ち・衝突は人に回さない
        url = rest if rest.startswith("https://") else ""
        if prefix == "merge" and s["correlation_id"]:
            cur.execute("select payload->>'pr_url' as u from events where name='supervisor.merge_decided' and correlation_id=%s order by id desc limit 1",
                        (s["correlation_id"],))
            r = cur.fetchone()
            url = (r or {}).get("u") or url
        if not url:   # 停止の文に PR の URL が入っていればそれを使う（マージ待ちの停止は what に入れている）
            m = PR_URL.search(s["what"] or "")
            url = m.group(0) if m else ""
        who = STOPPAGE_WHO.get(prefix, "owner")
        if prefix == "judge" and url:
            # judge が人に回した判断は、問いを 1 つずつ件にしてその場で答えられるようにする（ADR 0034。11-07 rally #921・#930・#970 は
            # PR へのリンクだけで、問いも答える場所も見えなかった）。記録が引けないとき（古い停止）は今までどおり停止の 1 件
            js = _judgment_items(cur, s, url, who)
            if js:
                out += js
                continue
        # この場で答えられるのはラインの一時停止の再開と、judge が人に回した判断（上の _judgment_items）。マージの判断は GitHub の PR で
        ans = {"type": "approve", "options": [{"id": "resume", "label": "ラインを再開する"}], "extra": [],
               "effect": "ラインの一時停止を解き、順番待ちの取り出しを再開する", "allowed": [who], "target": ("resume_line", None)} if prefix == "drain" else None
        if prefix == "sendback" and url:
            ans = _sendback_answer(cur, url, who)
        # 題と「すること」は停止のアラートと同じ読む文（alerts.py。記録の文は工場の内側の言葉で、事業主には読めない。11-08）
        al = alerts.describe(cur, s)
        if cap:
            al["todo"] = (f"工場は衝突の解消を上限（{cap.get('cap')} 回）まで試してやめた（計 {cap.get('conflicts')} 回）。"
                          "最新の main から作り直す・差し戻す・閉じるのどれかを決める。よく変わる同じファイルを触る PR どうしなら、そのファイルの直しも検討する")
        if prefix == "cost":   # 費用の上限（ADR 0038 の追記、2026-11-11）
            al["todo"] = ("工場は費用の上限でこのチケットのジョブを止めている。続けるなら上限に上乗せ（factoryctl plan cap <repo#番号> --add-usd <額>）、"
                          "やめるならチケットか PR を閉じる。大きすぎるなら切り直し（factoryctl plan resplit）も検討する")
        fixing = _sendback_fixing(cur, url) if prefix == "sendback" and url else None
        out.append({"key": f"stoppage:{s['key']}", "kind": "stoppage", "who": who, "fixing": fixing, "title": al["title"] + (f" — {al['target']['label']}" if al.get("target") else ""),
                    "action": al["todo"] or "判断して止まりを解く", "detail": al["detail"], "url": url, "since": s["started_at"],
                    "ref": (s["correlation_id"] or ":").split(":", 1)[1], "announced": True,   # 開いたときに停止の通知で一度知らせている
                    "answer": ans, "also": []})
    return out


def _sendback_fixing(cur, pr_url: str) -> dict | None:
    """差し戻しを直しに回した（直しの Issue に factory:planned を付けて投入待ち・実装中にした）なら、その答えの形。
    受信トレイでは「回答済（反映待ち）」に移す。supervisor が次の周期に停止を閉じるまでの間（2026-11-09 rally #1228・#1251）"""
    import sendback
    f = sendback.fixing(cur, pr_url)
    return {"by": "Issue のラベル", "via": "github", "label": f"直しに回した（#{f['number']}）", "at": f["at"]} if f else None


SENDBACK_EFFECT = ("流す: 差し戻しの Issue のラベルを factory:planned に替える。次の周期に停止を閉じ、元 PR の衝突の解消とマージに進め、"
                   "元 PR が入ったら差し戻しを直す。受け入れる: 残った指摘を直さずに停止を閉じ、元 PR を先へ進める")


def _sendback_answer(cur, pr_url: str, who: str) -> dict:
    """流さなかった差し戻し（sendback:）の答え。起票済みの差し戻しの Issue があれば「流す」、なければ「受け入れる」だけ"""
    import sendback
    options = ([{"id": "flow", "label": f"差し戻しを流す（#{sendback.fix_issue(cur, pr_url).rsplit('#', 1)[-1]}）"}]
               if sendback.fix_issue(cur, pr_url) else [])
    options.append({"id": "accept", "label": "受け入れて先へ進める"})
    return {"type": "choose", "options": options, "recommended": options[0]["id"], "extra": [], "note_hint": "コメント（任意。受け入れる理由など）",
            "effect": SENDBACK_EFFECT, "allowed": [who], "target": ("sendback", pr_url)}


JUDGMENT_EFFECT = ("答えを判断として記録する。この PR の判断がすべて決まったら停止を閉じてマージの判定に進める。"
                   "直す答えは同じ担当の続きのジョブで直し、もう一度検査と judge を通す")
JUDGMENT_NOTE = "コメント（別の回答では必須。どう直すか）"
CATEGORY_JA = {"ui": "画面と操作", "aesthetic": "見た目と印象", "system_interface": "境界と契約", "implementation": "実装の選び方",
               "error_handling": "失敗の扱い", "data": "データ", "scope": "範囲", "inspection": "検査の指摘", "unknown": "判断"}


def _judgment_items(cur, s: dict, url: str, who: str) -> list[dict]:
    """judge が人に回した項目のうち、人の判断がまだのもの 1 つずつを受信トレイの 1 件にする"""
    import judgment
    m = re.search(r"github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)", url)
    pr = f"{m.group(1)}#{m.group(2)}" if m else url
    out = []
    for e in judgment.escalations(cur, url):
        p = e["payload"]
        defect = p.get("subject") == "defect"
        d = p.get("defect") or {}
        detail = ([{"label": "指摘", "text": _clip(d.get("evidence"), 600)}, {"label": "直し方の手がかり", "text": _clip(d.get("remediation_hint"), 600)}]
                  if defect else
                  [{"label": "今の案", "text": _clip(p.get("decision"), 600)}, {"label": "他の案", "text": _clip(e.get("alternatives"), 600)}])
        detail += [{"label": "見立て", "text": _clip(p.get("interpretation"), 800)}, {"label": "影響", "text": _clip(p.get("impact"), 400)}]
        if p.get("recommended_replacement"):
            detail.append({"label": "おすすめの案", "text": _clip(p["recommended_replacement"], 600)})
        rec = p.get("recommend")
        if defect:
            options = [{"id": "fix", "label": "指摘どおり直す"}, {"id": "dismiss", "label": "直さない（今のままでよい）"}]
            recommended = {"adopt": "fix", "reject": "dismiss"}.get(rec)
        else:
            options = [{"id": "keep", "label": "今の案のまま"}]
            if p.get("recommended_replacement"):
                options.append({"id": "replace", "label": "おすすめの案に直す"})
            recommended = "replace" if rec == "replace" and p.get("recommended_replacement") else "keep" if rec == "adopt" else None
        label = {o["id"]: o["label"] for o in options}.get(recommended)
        out.append({"key": f"judgment:{e['id']}", "kind": "judgment", "who": who,
                    "title": f"{pr} の判断（{CATEGORY_JA.get(p.get('category'), p.get('category') or '判断')}）: " + _clip(p.get("question"), 200),
                    "action": "今の案・他の案を読んで答える" + (f"（推奨: {label}）" if label else ""),
                    "detail": [x for x in detail if x["text"]], "url": url, "since": e["created_at"] or s["started_at"],
                    "ref": (e["correlation_id"] or s["correlation_id"] or ":").split(":", 1)[-1], "also": [],   # 停止の key を別名にしない（1 つ答えると同じ PR の残りの問いまで答え済みに見える）
                    "answer": {"type": "choose", "options": options, "recommended": recommended,
                               "extra": [{"id": "change", "label": "別の回答", "note_required": True}],
                               "note_hint": JUDGMENT_NOTE, "effect": JUDGMENT_EFFECT, "allowed": [who], "target": ("judgment", e["id"])}})
    return out


def _choice_item(r: dict) -> dict:
    """開いている人の選択 1 つを受信トレイの 1 件にする（実装後の画面もここでは 1 つずつ。まとめるのは _choices）"""
    p = r["payload"]
    design = p.get("kind") in ("design_variants", "design_look")
    # 体験の承認は PO、見た目の判断はデザインのチーフ（ADR 0026 の 4・6。Gen1 はどちらもオーナーが兼ねる）
    # 実装後の画面への違和感の印（ADR 0025 の 4）は見た目の判断なのでデザインのチーフ
    who = "po" if p.get("kind") == "design_variants" else "design_chief" if p.get("kind") in ("design_look", "concept_tone", "exit_capture") else "owner"
    action = ("画面を見て、違和感があれば画面ごとにコメントで印を付ける。なければ「見た」で閉じる" if p.get("kind") == "exit_capture" else
              "案を選ぶか、どれも違えば戻す" + (f"（推奨 {p['recommended']}）" if p.get("recommended") else ""))
    uxspec = (p.get("subject") or {}).get("uxspec")
    also = [f"task:{'design_review' if p.get('kind') == 'design_variants' else 'design_look'}:{uxspec}"] if design and uxspec else []
    return {"key": f"choice:{r['id']}", "kind": "choice", "who": who,
            "title": f"{CHOICE_TITLES.get(p.get('kind'), '選択')}: " + _clip(p.get("question"), 200),
            "action": action,
            "url": f"/#choice-{r['id']}", "since": r["created_at"], "ref": (r["correlation_id"] or ":").split(":", 1)[-1],
            "answer": _choice_answer(r["id"], p, who), "also": also,
            "uxspec": (p.get("kind"), uxspec) if design else None}


def _choices(cur) -> list[dict]:
    cur.execute("select id, correlation_id, created_at, payload from artifacts where type='HumanChoice' and payload->>'status'='open' order by id")
    out, captures = [], []
    for r in cur.fetchall():
        if r["payload"].get("kind") == "exit_capture":
            captures.append(r)
            continue
        out.append(_choice_item(r))
    if captures:
        # 実装後の画面は PR・アプリごとに出るので数が多い（更新直後は 40 件を超えた）。印は任意なので 1 件にまとめ、通知も最初の 1 回だけ
        prs = sorted({(r["payload"].get("subject") or {}).get("pr_url", "").rsplit("/", 1)[-1] for r in captures} - {""}, key=lambda x: int(x) if x.isdigit() else 0)
        head = "・".join(f"#{n}" for n in prs[-3:]) + (f" ほか {len(prs) - 3} 件" if len(prs) > 3 else "")
        out.append({"key": "choice:exit_capture", "kind": "choice", "who": "design_chief",
                    "title": f"{CHOICE_TITLES['exit_capture']}: {len(captures)} 件（{head}）",
                    "action": "画面を見て、違和感があれば画面ごとにコメントで印を付ける。なければ「見た」で閉じる（任意）",
                    "url": "/#choices", "since": min(r["created_at"] for r in captures), "ref": "", "once": True, "uxspec": None,
                    "answer": {"type": "ack", "options": [{"id": "seen", "label": f"{len(captures)} 件とも見た（違和感なし）"}], "extra": [],
                               "effect": f"実装後の画面 {len(captures)} 件を、違和感のなかった画面としてまとめて閉じる（印は画面ごとに下の選択で付ける）",
                               "allowed": ["design_chief"], "target": ("captures", [r["id"] for r in captures])}})
    return out


def _release_item(cur, repo: str) -> dict | None:
    """staging で確かめた版がまだ本番に出ていなければ 1 件。本番に出す承認を受けて処理中なら processing を付ける"""
    import ship   # 出荷の状態の読み方は ship に 1 か所
    if ship.prod_held(cur, repo):
        return None   # 本番に出さない段階（development）の製品は本番待ちにしない
    envs = ship.status(cur, repo)["environments"]
    st, pr = envs.get("staging") or {}, envs.get("prod") or {}
    rid = st.get("release_id")
    if not rid or rid == pr.get("release_id"):
        return None
    cur.execute("select occurred_at from events where name='release.deployed' and payload->>'release_id'=%s and payload->>'env'='staging' "
                "order by id desc limit 1", (rid,))
    d = cur.fetchone()
    # 承認を受けて shipper の結果を待っている（失敗したら結果が付くので、また開いた件に戻る）
    cur.execute("select payload from events e where name='release.approved' and payload->>'release_id'=%s and not exists "
                "(select 1 from events r where r.name = any(%s) and (r.payload->>'request_id')::bigint = e.id) order by id desc limit 1",
                (rid, list(ship.RESULTS)))
    pending = cur.fetchone()
    ver = rid.split("@")[-1]
    import qa_gate   # QA の関門（ADR 0049）: 報告ができるまで出さない。報告を添え、ブロッカーにするかは承認者が決める（「見送る」）
    g = qa_gate.gate(cur, rid)
    if g and (g["state"] == "open" or g["blocked"]):
        return None
    if g and g["state"] == "decided":
        n = g["decided"].get("candidates") or 0
        detail = list(g["decided"].get("report") or [])
        action = (f"QA の報告にブロッカーの候補が {n} 件ある。ブロッカーにするなら「見送る」、承知で出すなら「本番に出す」" if n
                  else "QA の報告にブロッカーの候補はない。staging で確かめて「本番に出す」を押す")
    else:
        detail = [{"label": "QA の関門", "text": "この版は関門を通っていない（判定と探索の報告がない）"}]
        action = "staging で確かめて「本番に出す」を押す"
    contracts = _contracts_between(cur, repo, pr.get("release_id"), rid)
    if contracts:   # ADR 0057 の決定 4: 出すと前の版に戻せなくなることを、押す前に知らせる
        detail.append({"label": "DB の contract", "text": f"この版を出すと DB から列・表を消すマイグレーションが当たり、{pr.get('release_id') or '前の版'} "
                       f"より前には戻せなくなる（{'・'.join(contracts)}）"})
    return {"key": f"release:{rid}", "kind": "release", "who": "owner", "title": f"{repo} の {ver} が本番待ち",
            "action": action, "detail": detail, "url": "/#ship", "since": d["occurred_at"] if d else None, "ref": repo,
            "processing": pending["payload"] if pending else None,
            "answer": {"type": "approve", "options": [{"id": "approve", "label": f"本番に出す（{ver}）"}],
                       "extra": [{"id": "block", "label": "見送る（ブロッカーにする）", "note_required": True}] if g else [],
                       "effect": f"staging で確かめた {ver} を本番に出す", "irreversible": True, "allowed": ["owner"],
                       "target": ("release", rid)}}


def _contracts_between(cur, repo: str, prod_rid: str | None, rid: str) -> list[str]:
    """本番のいまの版の後から rid までの Release で足された contract のマイグレーション（ship.contracts_of の記録）"""
    import ship
    cur.execute("select payload->>'release_id' as rid from events where name='release.created' and payload->>'repo'=%s order by id", (repo,))
    order = [r["rid"] for r in cur.fetchall()]
    if rid not in order:
        return []
    start = order.index(prod_rid) + 1 if prod_rid in order else 0
    found = ship.contracts_of(cur, repo)
    return [m for r in order[start:order.index(rid) + 1] for m in found.get(r, [])]


def _releases(cur) -> list[dict]:
    cur.execute("select distinct payload->>'repo' as repo from events where name='release.created' order by 1")
    return [i for i in (_release_item(cur, r["repo"]) for r in cur.fetchall()) if i]


def _tasks(cur) -> list[dict]:
    cur.execute("select o.occurred_at, o.correlation_id, o.payload from events o where o.name='inbox.requested' "
                "and not exists (select 1 from events r where r.name='inbox.resolved' and r.payload->>'key'=o.payload->>'key' and r.id > o.id) "
                "order by o.id")
    out = []
    for r in cur.fetchall():
        p = r["payload"]
        i = {"key": f"task:{p['key']}", "kind": "task", "task_kind": p.get("kind"), "who": p.get("who") or "owner",
             "title": p.get("title") or "", "action": p.get("action") or "", "url": p.get("url") or "",
             "since": r["occurred_at"], "ref": (r["correlation_id"] or ":").split(":", 1)[-1], "answer": None, "also": []}
        if p.get("kind") == "review_page":   # レビューページの依頼は、同じ UXSpec の人の選択に答える（レビューページのボタンと同じ）
            m = re.fullmatch(r"(design_review|design_look):(\d+)", p["key"])
            c = _review_choice(cur, m.group(1), m.group(2)) if m else None
            if c:
                i["answer"], i["also"] = c["answer"], [c["key"]]
        elif p.get("kind") in DESK_ANSWERS:
            d = DESK_ANSWERS[p["kind"]]
            i["answer"] = {"type": "choose", "options": d["options"], "extra": d.get("extra", []), "effect": d["effect"],
                           "allowed": ["requester"], "target": ("desk", p["key"])}
        elif p.get("kind") == "opportunity" and _opportunity_can_file(cur, p["key"]):   # 仕込みに当たった提案は「見た」だけ
            i["answer"] = {"type": "choose", "options": [OPPORTUNITY_FILE, {"id": "seen", "label": "見た（閉じる）"}], "extra": [],
                           "effect": OPPORTUNITY_EFFECT, "allowed": [i["who"]], "target": ("task", p["key"])}
        elif p.get("kind") in TASK_ACK:
            i["answer"] = {"type": "ack", "options": [{"id": "seen", "label": "見た（閉じる）"}], "extra": [], "effect": TASK_ACK[p["kind"]],
                           "allowed": [i["who"]], "target": ("task", p["key"])}
        out.append(i)
    return out


def _opportunity_can_file(cur, key: str) -> bool:
    import opportunity
    return opportunity.can_file(cur, key)


def _review_choice(cur, axis: str, uxspec: str) -> dict | None:
    kind = "design_variants" if axis == "design_review" else "design_look"
    cur.execute("select id, correlation_id, created_at, payload from artifacts where type='HumanChoice' and payload->>'status'='open' "
                "and payload->>'kind'=%s and payload->'subject'->>'uxspec'=%s order by id desc limit 1", (kind, uxspec))
    r = cur.fetchone()
    return _choice_item(r) if r else None


def _notify_config(cur) -> list[dict]:
    """通知の設定（製品の .factory-config/notifications.yaml、ADR 0032）のいちばん新しい版が検査を通らなかった。前の写しのまま送っている"""
    cur.execute("select name, occurred_at, payload from events where name in ('notify.config_changed','notify.config_rejected') order by id desc limit 1")
    r = cur.fetchone()
    if not r or r["name"] != "notify.config_rejected":
        return []
    p = r["payload"]
    return [{"key": f"error:notify-config:{(p.get('hash') or '')[:12]}", "kind": "error", "who": "builder",
             "title": "通知の設定が検査を通らない（前に通った設定のまま送っている）",
             "action": "; ".join(p.get("problems") or [])[:MAX_LEN], "url": "", "since": r["occurred_at"], "ref": p.get("repo") or ""}]


def _collect(cur, now: datetime) -> list[dict]:
    out: list[dict] = []
    for collect in (lambda: _stoppages(cur, now), lambda: _choices(cur), lambda: _releases(cur), lambda: _tasks(cur),
                    lambda: _notify_config(cur)):
        try:
            out += collect()
        except Exception as e:  # noqa: BLE001
            out.append({"key": f"error:{type(e).__name__}", "kind": "error", "who": "builder", "title": "受信トレイの一部を読めなかった",
                        "action": repr(e)[:200], "url": "", "since": None, "ref": ""})
    for i in out:
        i.setdefault("answer", None)
        i.setdefault("also", [])
    _mark_answered(cur, out)
    return out


def _mark_answered(cur, xs: list[dict]) -> None:
    """答え済みで反映を待っている件に answered を付ける（件が開いてから後の inbox.answered。件の別名 also でも引く）"""
    if not xs:
        return
    cur.execute("select occurred_at, payload from events where name='inbox.answered' and coalesce((payload->>'closes')::boolean, true) "
                "and occurred_at > now() - interval '30 days' order by id")
    last: dict[str, dict] = {}
    for r in cur.fetchall():
        a = {**r["payload"], "at": r["occurred_at"]}
        for k in [a.get("key"), *(a.get("also") or [])]:
            if k:
                last[k] = a
    for i in xs:
        a = next((last[k] for k in [i["key"], *i.get("also", [])] if k in last), None)
        if a and i.get("since") and isinstance(a["at"], datetime) and a["at"] < i["since"]:
            a = None   # 前に開いていたときの答え（同じ key で積み直した依頼）
        if i["kind"] == "release":   # 本番への出荷は shipper の結果を待っている間だけ答え済み。失敗したらまた開く
            p = i.pop("processing", None)
            a = (a or {"by": p.get("by"), "via": p.get("via"), "label": "本番に出す"}) if p else None
        fixing = i.pop("fixing", None)
        if not a and fixing:   # 差し戻しを直しに回した件は、直しが入って停止が閉じるまで回答済（反映待ち）に置く
            a = fixing
        i["answered"] = {k: (_iso(v) if k == "at" else v) for k, v in a.items() if k in ("by", "via", "option", "label", "at")} if a else None


def items(cur, now: datetime | None = None, *, include_answered: bool = False) -> list[dict]:
    """人が対応するもの。待ちの長い順。1 つの集め方が失敗しても残りは返す（受信トレイが空に見えるより一部でも出す）。
    答え済みで反映を待っている件は人の出番ではないので、include_answered のときだけ返す"""
    now = now or datetime.now(timezone.utc)
    out = _collect(cur, now)
    # 画面の案の選択は、レビューページ（review_page.py）が同じ UXSpec の依頼を積んでいればそちらに任せる（二重に出さない）
    # 体験の案（design_variants）は task:design_review:<id>、見た目の案（design_look）は task:design_look:<id>
    prefix = {"task:design_review:": "design_variants", "task:design_look:": "design_look"}
    reviews = {(k, i["key"].removeprefix(pre)) for i in out for pre, k in prefix.items() if i["key"].startswith(pre)}
    out = [i for i in out if not (i["kind"] == "choice" and i.get("uxspec") and i["uxspec"] in reviews)]
    if not include_answered:
        out = [i for i in out if not i.get("answered")]
    for i in out:
        i.pop("uxspec", None)
        i["age_sec"] = _age(i["since"], now)
    out.sort(key=lambda i: -(i["age_sec"] or 0))
    return out


def _public(a: dict | None) -> dict | None:
    return {k: v for k, v in a.items() if k != "target"} if a else None


def view(cur, now: datetime | None = None) -> dict:
    """操業コンソールの /api/inbox の形（JSON にできる値だけ）。count・by_who は人の出番（答え済みを除く）の数"""
    xs = items(cur, now, include_answered=True)
    by_who: dict[str, int] = {}
    for i in xs:
        if not i.get("answered"):
            by_who[i["who"]] = by_who.get(i["who"], 0) + 1
    return {"count": sum(by_who.values()), "by_who": by_who, "who": WHO, "kinds": KINDS,
            "items": [{**{k: v for k, v in i.items() if k not in ("announced", "once", "also")}, "since": _iso(i["since"]), "answer": _public(i.get("answer"))}
                      for i in xs]}


# ------------------------------------------------------------------ 答える（ADR 0034 の決定 1・4・5）
def _choice_answer(aid: int, p: dict, who: str) -> dict:
    if p.get("kind") == "exit_capture":
        return {"type": "ack", "options": [{"id": "seen", "label": "見た（ほかに違和感なし）"}], "extra": [EXTRA["flag"]],
                "effect": "印のない画面を違和感のなかった画面として閉じる", "allowed": [who], "target": ("choice", aid)}
    return {"type": "choose", "options": [{"id": o["id"], "label": o.get("label") or o["id"]} for o in p.get("options") or []],
            "recommended": p.get("recommended"), "extra": [EXTRA["return"], EXTRA["flag"]],
            "effect": CHOICE_EFFECTS.get(p.get("kind"), "選んだ案を記録する"), "allowed": [who], "target": ("choice", aid)}


def find(cur, key: str, now: datetime | None = None) -> dict | None:
    """key の件（答え済みも含む）。人の選択は受信トレイでまとめた件（実装後の画面）や、レビューページに任せて隠した件も 1 つずつ引ける"""
    m = re.fullmatch(r"choice:(\d+)", key or "")
    if m:
        cur.execute("select id, correlation_id, created_at, payload from artifacts where id=%s and type='HumanChoice' and payload->>'status'='open'",
                    (int(m.group(1)),))
        r = cur.fetchone()
        if not r:
            return None
        xs = [_choice_item(r)]
        _mark_answered(cur, xs)
        return xs[0]
    return next((i for i in _collect(cur, now or datetime.now(timezone.utc)) if i["key"] == key), None)


def last_answer(cur, key: str) -> dict | None:
    cur.execute("select occurred_at, payload from events where name='inbox.answered' and (payload->>'key'=%(k)s or payload->'also' ? %(k)s) "
                "and coalesce((payload->>'closes')::boolean, true) order by id desc limit 1", {"k": key})
    r = cur.fetchone()
    return {**r["payload"], "at": r["occurred_at"]} if r else None


def answered_text(a: dict) -> str:
    """「誰が・どの入口で・何と」答えたか（ほかの入口に返す文と同じ言葉）"""
    via = VIA_JA.get(a.get("via"), a.get("via") or "")
    return f"{a.get('by') or '?'} が{via}で『{a.get('label') or a.get('option') or '?'}』と回答済"


VIA_JA = {"console": "操業コンソール", "review_page": "レビューページ", "factoryctl": " factoryctl ", "mcp": " Claude（窓口）", "github": " GitHub ", "slack": " Slack "}


def answer(cur, key: str, option: str, by: str, via: str, *, note: str = "", raw: str = "", confidence: float | None = None,
           on: str | None = None, explicit: bool = True, role: str | None = None, viewpoint: str | None = None) -> tuple[str, str]:
    """受信トレイの 1 件に答える唯一の口（ADR 0034 の決定 1）。返り値は (結果, 文)。
    結果は answered（答えた）・noted（印を付けた。件は開いたまま）・already（答え済み）・not_found・invalid・failed（作用の側が受けなかった）。

    - option は件の answer.options か extra の id。note は理由・違和感の一言（戻す・印では必須）。on は違和感の印を付ける案（画面）の id
    - explicit: ボタンや道具の呼び出しのように選択肢がはっきりした答えか。自由文を解釈した答え（False）では取り返しのつかない作用を確定しない（決定 4）
    - role: 答えた人の役割（who と同じ語）。渡したときは件の allowed と照らす。Gen1 のコンソールはログインがないので渡さない（運用主。決定 7）
    - 同じ件への答えは件ごとのロックで 1 つずつ通し、先に通った方が勝つ（決定 5）
    """
    if via not in VIAS:
        return "invalid", f"via は {' / '.join(VIAS)} のどれか"
    note = _clip(note)
    try:
        return _answer(cur, key, option, by, via, note, raw, confidence, on, explicit, role, viewpoint)
    except _Failed as e:   # 作用の途中で受けなかったら、書いた分も戻す（件は開いたまま）
        return "failed", str(e)


class _Failed(Exception):
    pass


def _answer(cur, key, option, by, via, note, raw, confidence, on, explicit, role, viewpoint=None) -> tuple[str, str]:
    with cur.connection.transaction():
        cur.execute("select pg_advisory_xact_lock(hashtext(%s))", (f"inbox.answer:{key}",))
        it = find(cur, key)
        if it is None or it.get("answered"):
            a = (it or {}).get("answered") or last_answer(cur, key)
            if a:
                return "already", f"この件はすでに{answered_text(a)}。変えたいときは受信トレイで答え直す"
            return "not_found", "その件は受信トレイに開いていない"
        spec = it.get("answer")
        if not spec:
            return "invalid", f"この件はここでは答えられない（{it.get('action') or 'リンク先で答える'}）"
        opts = {o["id"]: o for o in spec["options"]} | {e["id"]: e for e in spec.get("extra") or []}
        o = opts.get(option)
        if o is None:
            return "invalid", f"答えは {' / '.join(opts)} のどれか"
        if o.get("note_required") and not note.strip():
            return "invalid", "コメント（理由・違和感）が要る"
        if spec.get("irreversible") and not explicit:
            return "invalid", "取り返しのつかない作用なので、ボタンか明示した答え（approve）で確定する"
        if role is not None and role not in (spec.get("allowed") or [it["who"]]):
            return "invalid", f"この件に答えられるのは {'・'.join(WHO.get(w, w) for w in spec.get('allowed') or [it['who']])}"
        ok, msg = _apply(cur, spec["target"], option, by, via, note, on, viewpoint)
        if not ok:
            raise _Failed(msg)
        closes = o.get("closes", True)
        cur.execute("insert into events(name, payload) values ('inbox.answered', %s)",
                    (Jsonb({"key": it["key"], "also": it.get("also") or [], "kind": it["kind"], "who": it["who"], "title": _clip(it["title"]),
                            "type": spec["type"], "option": option, "label": _clip(o.get("label"), 80), "note": note, "closes": closes,
                            "by": _clip(by, 80), "via": via, "raw": _clip(raw, 1000), "confidence": confidence, "explicit": explicit}),))
        return ("answered" if closes else "noted"), msg


def _apply(cur, target: tuple, option: str, by: str, via: str, note: str, flag_option: str | None,
           viewpoint: str | None = None) -> tuple[bool, str]:
    """件の種類ごとの今の関数を呼ぶ（入口の側は件の種類を知らない）"""
    import choice
    kind, ref = target
    if kind == "choice":
        if option == "flag":
            return choice.flag(cur, ref, note, by, flag_option, viewpoint)
        return _answer_choice(cur, ref, option, by, note)
    if kind == "captures":
        n = sum(1 for aid in ref if choice.mark_seen(cur, aid, by, note)[0])
        return (True, f"{n} 件を見たとして閉じた") if n else (False, "閉じられる画面がなかった")
    if kind == "release":
        if option == "block":   # QA の報告の候補をブロッカーにして、この版は本番に出さない（ADR 0049）
            import qa_gate
            return qa_gate.block(cur, ref, by, via, note)
        import ship
        return ship.request_approve(cur, ref, by, via)
    if kind == "resume_line":
        import drain
        drain.set_drain(cur, False, by, note or "受信トレイから再開")
        return True, "ラインを再開した"
    if kind == "task":
        if option == "file":   # 改善の提案を起票に回す（Issue は supervisor が次の周期に作る。入口は GitHub のトークンを持たない）
            import opportunity
            ok, msg = opportunity.request_ticket(cur, ref, by, note)
            if not ok:
                return ok, msg
            return (True, msg) if resolve(cur, ref, by, option) else (False, "もう閉じている")
        return (True, "閉じた") if resolve(cur, ref, by, option) else (False, "もう閉じている")
    if kind == "desk":
        import desk_line
        return desk_line.answer(cur, ref, option, by, via, note)
    if kind == "sendback":
        import sendback
        if option == "flow":
            return sendback.request_flow(cur, ref, by)
        return sendback.accept(cur, ref, by, note)
    if kind == "judgment":
        import judgment
        return judgment.decide(cur, ref, option, by, via, note)
    return False, f"答えの作用が分からない: {kind}"


def _answer_choice(cur, aid: int, option: str, by: str, note: str) -> tuple[bool, str]:
    """人の選択への答え。デザインの案はデザインの線に渡す（Issue への反映は dispatcher の周回。体験は ADR 0025、見た目は ADR 0026 の決定 4）"""
    import choice
    import design_line
    c = choice.get(cur, aid)
    sub = (c or {}).get("subject") or {}
    design = bool(c and c.get("kind") in ("design_variants", "design_look") and c.get("status") == "open" and sub.get("repo")
                  and str(sub.get("ticket", "")).isdigit())
    if option == "return":
        ok, msg = choice.return_to_maker(cur, aid, by, note)
        if ok and design:
            design_line.return_to_designer(cur, sub["repo"], int(sub["ticket"]), c["correlation_id"], note, MODEL_TIER)
        return ok, msg
    if option == "seen":
        return choice.mark_seen(cur, aid, by, note)
    if design:
        pick = design_line.choose if c["kind"] == "design_variants" else design_line.choose_look
        try:
            pick(cur, sub["repo"], int(sub["ticket"]), option, by, note)
        except ValueError as e:
            return False, str(e)
    return choice.choose(cur, aid, option, by, note)
