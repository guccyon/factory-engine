"""デザインのレビューページ（ADR 0026 の決定 3・5）。

デザイナー役のデザインのパッケージ（PRD の要約・ユーザーフロー・画面定義書・案・設計の理由）と、デザイン検査役の判断を 1 枚にまとめ、
判断する人（プロダクトオーナー・デザインのチーフ）がそこで承認・直し・違和感を返せるようにする。操業コンソールが /review/<UXSpec の id> で配る。

- 組み立ては純粋な関数（render）。読むのは制御プレーン（artifacts・events）だけで、GitHub には触らない（load）
- 操作は操業コンソールの人の選択の API（/api/choices/<id>/choose・return・flag）をそのまま使う。人に回っていない案は読むだけ
- デザイナー役の文は外部の入力と同じに扱う: すべてエスケープし、案の HTML はこのページでは開かない（画像だけを出す）
- 通知は受信トレイ（inbox）が持つ。ここは URL を渡すだけ
"""
from __future__ import annotations

import html
import json

PATH = "/review/{aid}"
IMAGE_PATH = "/review/{aid}/image/{name}"
STATUS_JA = {"open": "判断待ち", "decided": "決定済み", "reviewing": "検査中", "returned": "デザイナー役に戻した"}
DECISION_JA = {"approve": "承認", "revise": "直し", "human": "人へ"}
REASON_JA = {"new_screen": "新しい画面", "brand_impact": "ブランドと印象に効く画面", "low_confidence": "検査役の確信度が低い",
             "revisions_exhausted": "直しを 1 回使った後も直しが要る", "inspector": "検査役が人の判断が要るとした"}
PRD_JA = {"goal": "誰が何を達成したいか", "success": "達成した状態", "scope": "範囲", "source": "出どころ"}
SECTION_JA = {"purpose": "1 目的", "route": "0 ルート", "audience": "0 誰が見るか", "transitions": "3 画面遷移", "elements": "4 レイアウトと要素",
              "states": "5 状態", "behaviors": "6 操作と振る舞い", "microinteractions": "7 マイクロインタラクション", "copy": "8 文言",
              "accessibility": "9 アクセシビリティ", "analytics": "10 計測", "contract_needs": "11 契約への要求"}
MAX_DEPTH = 5


def url(aid: int) -> str:
    return PATH.format(aid=int(aid))


def esc(v) -> str:
    return html.escape(str(v), quote=True)


def fmt(v, depth: int = 0) -> str:
    """デザイナー役の自由な形（文・一覧・対応表）を、エスケープした HTML にする"""
    if depth > MAX_DEPTH:
        return esc(json.dumps(v, ensure_ascii=False)[:500])
    if v is None or v == "" or v == [] or v == {}:
        return '<span class="m">（なし）</span>'
    if isinstance(v, bool):
        return "はい" if v else "いいえ"
    if isinstance(v, (str, int, float)):
        return esc(v)
    if isinstance(v, list):
        return "<ul>" + "".join(f"<li>{fmt(x, depth + 1)}</li>" for x in v) + "</ul>"
    if isinstance(v, dict):
        return "<dl>" + "".join(f"<dt>{esc(k)}</dt><dd>{fmt(x, depth + 1)}</dd>" for k, x in v.items()) + "</dl>"
    return esc(v)


# ------------------------------------------------------------------ 読む（制御プレーン）
def load(cur, aid: int) -> dict | None:
    """ページに要るものを集める。UXSpec がなければ None"""
    cur.execute("select id, correlation_id, payload, created_at from artifacts where id=%s and type='UXSpec'", (aid,))
    r = cur.fetchone()
    if not r:
        return None
    d = {"id": r["id"], "corr": r["correlation_id"], "created_at": r["created_at"], **r["payload"]}
    cur.execute("select id, payload from artifacts where type='DesignVerdict' and (payload->>'design_artifact_id')::bigint=%s order by id desc limit 1", (aid,))
    v = cur.fetchone()
    choices = {}
    for kind in ("design_variants", "design_look"):   # 体験（プロダクトオーナー）と見た目（デザインのチーフ）。ADR 0026 の決定 4・6
        cur.execute("select id, payload from artifacts where type='HumanChoice' and payload->>'kind'=%s "
                    "and payload->'subject'->>'uxspec'=%s order by id desc limit 1", (kind, str(aid)))
        c = cur.fetchone()
        choices[kind] = {"id": c["id"], **c["payload"]} if c else None
    cur.execute("select payload from events where dedupe_key=%s", (f"ui_design.handed_off:{aid}",))
    h = cur.fetchone()
    cur.execute("select payload from events where dedupe_key=%s", (f"ui_design.look_chosen:{aid}",))
    lk = cur.fetchone()
    return {"design": d, "verdict": {"id": v["id"], **v["payload"]} if v else None,
            "choice": choices["design_variants"], "look_choice": choices["design_look"],
            "handed_off": h["payload"] if h else None, "look_chosen": lk["payload"] if lk else None}


def _open(c) -> bool:
    return bool(c and c.get("status") == "open" and c.get("id") is not None)


def status(ctx: dict) -> str:
    # 体験が引き渡し済みなら体験の選択は締め切り（factoryctl 経由で選ばれた場合も）
    flow_open = _open(ctx.get("choice")) and not ctx.get("handed_off")
    if flow_open or _open(ctx.get("look_choice")):
        return "open"
    if ctx.get("handed_off"):
        return "decided"
    if any((ctx.get(k) or {}).get("status") == "returned" for k in ("choice", "look_choice")):
        return "returned"
    return "reviewing"


# ------------------------------------------------------------------ 組み立て（純粋）
def render(ctx: dict, token: str) -> str:
    d, v, h = ctx["design"], ctx.get("verdict") or {}, ctx.get("handed_off")
    c, lc = ctx.get("choice") or {}, ctx.get("look_choice") or {}
    spec = d.get("uxspec") or {}
    st = status(ctx)
    flow_open, look_open = _open(c) and not h, _open(lc)
    actionable = flow_open or look_open
    flow_vs = [x for x in spec.get("variants") or [] if isinstance(x, dict) and x.get("axis", "flow") == "flow"]
    look_vs = [x for x in spec.get("variants") or [] if isinstance(x, dict) and x.get("axis") == "look"]
    rec = v.get("chosen_variant") if v.get("chosen_variant") in [str(x.get("id")) for x in flow_vs] else spec.get("recommended")
    look = (ctx.get("look_chosen") or {}).get("variant")
    files = d.get("files") or {}
    title = f"{d.get('repo')}#{d.get('issue')} の画面の設計"

    reasons = "、".join(REASON_JA.get(x, x) for x in v.get("human_reasons") or [])
    head = [f'<p class="m">{esc(d.get("repo"))}#{esc(d.get("issue"))} · 体験の案 {len(flow_vs)} つ · {esc(d.get("round") or 1)} 回目の案</p>',
            f"<h1>{esc(title)}</h1>",
            f'<p><span class="pill {st}">{STATUS_JA[st]}</span>'
            + (f' 人に回った理由: {esc(reasons)}' if reasons and st == "open" else "")
            + (f' 体験の案: <b>{esc(h.get("variant"))}</b>（{"人が承認" if h.get("decided_by") == "human" else "デザイン検査役が承認"}、実装に渡した）' if h else "")
            + (f' 見た目の案: <b>{esc(look)}</b>（デザインのチーフが選んだ）' if look else (f' 見た目は仮に <b>{esc(h.get("look"))}</b>' if h and h.get("look") else ""))
            + "</p>"]
    if flow_open:
        head.append('<p class="m">プロダクトオーナーへ: 体験の案（流れ・要素・振る舞い）を見比べて「この案で承認」を押すと実装に流れます。'
                    'どれも違うときは一言を書いて「直しを頼む」。気になる点は「違和感」で残せます（工場の学習に使います）。</p>')
    if look_open:
        head.append('<p class="m">デザインのチーフへ: 見た目の案から 1 つ選んでください。実装は推す見た目で先に進んでいて、違う見た目を選ぶと合わせるタスクが切られます。</p>')

    prd = spec.get("prd") or {}
    prd_html = "<dl>" + "".join(f"<dt>{esc(PRD_JA.get(k, k))}</dt><dd>{fmt(x)}</dd>" for k, x in prd.items()) + "</dl>" if isinstance(prd, dict) else fmt(prd)

    flow = spec.get("user_flow") or []
    flow_html = '<ol class="flow">' + "".join(
        f'<li><span class="m">{esc(s.get("from") or "")}</span> → <b>{esc(s.get("action") or "")}</b> → {esc(s.get("to") or "")}</li>'
        if isinstance(s, dict) else f"<li>{fmt(s)}</li>" for s in flow) + "</ol>" if isinstance(flow, list) else fmt(flow)

    def cards(vs, recommended, choice_id, label):
        out = []
        for x in vs:
            xid = str(x.get("id"))
            png = f"{xid}.png"
            img = (f'<a href="{esc(IMAGE_PATH.format(aid=d["id"], name=png))}" target="_blank" rel="noopener">'
                   f'<img src="{esc(IMAGE_PATH.format(aid=d["id"], name=png))}" alt="案 {esc(xid)}"></a>') if png in files else '<p class="m">（画像なし）</p>'
            acts = (f'<div class="acts"><button class="go" data-a="choose" data-c="{choice_id}" data-o="{esc(xid)}">{label}</button>'
                    f'<button data-a="flag" data-c="{choice_id}" data-o="{esc(xid)}">違和感</button></div>') if choice_id else ""
            badge = ' <span class="pill rec">推奨</span>' if xid == recommended else ""
            out.append(f'<article class="var{" rec" if xid == recommended else ""}"><h3>{esc(xid)}. {esc(x.get("name") or "")}{badge}</h3>'
                       f'<p>{esc(x.get("tradeoff") or "")}</p><p class="m">{esc(x.get("viewport") or "mobile")}</p>{img}{acts}</article>')
        return f'<div class="vars">{"".join(out) or "<p class=m>（なし）</p>"}</div>'

    flow_cards = cards(flow_vs, rec, c.get("id") if flow_open else None, "この案で承認")
    look_cards = cards(look_vs, spec.get("recommended_look"), lc.get("id") if look_open else None, "この見た目にする") if look_vs else ""
    back_id = c.get("id") if flow_open else (lc.get("id") if look_open else None)

    why = spec.get("rationale") or {}
    why_html = fmt(why) if not isinstance(why, dict) else (
        f"<p>{fmt(why.get('why'))}</p>"
        + (f"<h3>検討してとらなかった案</h3>{fmt(why.get('rejected'))}" if why.get("rejected") else "")
        + (f"<h3>要求との対応</h3>{fmt(why.get('requirement_map'))}" if why.get("requirement_map") else "")
        + (f'<p class="m">推す理由（デザイナー役）: {esc(spec.get("recommended_reason"))}</p>' if spec.get("recommended_reason") else ""))

    screens = []
    for s in spec.get("screens") or []:
        if not isinstance(s, dict):
            continue
        rows = "".join(f"<tr><th>{esc(SECTION_JA.get(k, k))}</th><td>{fmt(s.get(k))}</td></tr>" for k in SECTION_JA if k in s)
        extra = "".join(f"<tr><th>{esc(k)}</th><td>{fmt(x)}</td></tr>" for k, x in s.items() if k not in SECTION_JA and k not in ("id", "new"))
        new = ' <span class="pill open">新しい画面</span>' if s.get("new") else ""
        screens.append(f'<section class="scr"><h3>{esc(s.get("id"))}{new}</h3>'
                       f'<div class="tw"><table>{rows}{extra}</table></div></section>')

    findings = v.get("findings") or []
    verdict_html = (f'<p>判断: <b>{esc(DECISION_JA.get(v.get("inspector_decision"), v.get("inspector_decision") or "（なし）"))}</b>'
                    f' · 推す案 {esc(v.get("chosen_variant") or "")} · 確信度 {esc((v.get("score") or {}).get("self_reported"))}</p>'
                    f'<p>{esc(v.get("interpretation") or "")}</p>'
                    + ("<h3>指摘</h3><ul>" + "".join(f'<li><span class="pill {esc(f.get("severity"))}">{esc(f.get("severity"))}</span> '
                                                   f'{esc(f.get("viewpoint"))} / 案 {esc(f.get("variant"))}: {esc(f.get("evidence"))} → {esc(f.get("fix"))}</li>'
                                                   for f in findings) + "</ul>" if findings else "")) if v else '<p class="m">まだ検査していない</p>'

    sections = [
        ("PRD の要約", prd_html),
        ("ユーザーフロー", flow_html),
        ("体験の案（プロダクトオーナーが判断）", flow_cards),
        *([("見た目の案（デザインのチーフが選ぶ）", look_cards
            + (f'<p class="m">推す理由（デザイナー役）: {esc(spec.get("recommended_look_reason"))}</p>' if spec.get("recommended_look_reason") else ""))]
          if look_cards else []),
        *([("一言・直し", f'<div class="acts back"><input id="note" maxlength="300" placeholder="一言（直しの中身・違和感の理由。直しを頼むときは必須）">'
                         f'<button data-a="return" data-c="{back_id}">どれも違う（直しを頼む）</button></div>')] if actionable else []),
        ("設計の理由", why_html),
        ("画面定義書", "".join(screens) or '<p class="m">（なし）</p>'),
        ("状態", fmt(spec.get("states"))),
        ("デザインシステム", f"<h3>従ったもの</h3>{fmt(spec.get('design_system_constraints'))}<h3>足りない部品</h3>{fmt(spec.get('design_system_gaps'))}"),
        ("未決と仮決め", fmt(spec.get("open_questions"))),
        ("デザイン検査役の判断", verdict_html),
    ]
    body = "".join(f"<section><h2>{esc(n)}</h2>{x}</section>" for n, x in sections)
    script = SCRIPT.replace("__TOKEN__", json.dumps(token))
    return (f'<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{esc(title)}</title><style>{STYLE}</style></head><body><main>{''.join(head)}{body}</main>"
            f'<div id="msg" role="status"></div><script>{script}</script></body></html>')


STYLE = """
:root{--bg:#f5f6f4;--card:#fff;--ink:#1d2420;--sub:#5d6862;--line:#d8ddd9;--accent:#1f5f8b;--ok:#2e7a4f;--warn:#b4531a}
@media (prefers-color-scheme:dark){:root{--bg:#151a18;--card:#1d2421;--ink:#e6ebe8;--sub:#9aa6a0;--line:#34403b;--accent:#78b4de;--ok:#7cc79a;--warn:#f0a06b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.7 system-ui,-apple-system,"Hiragino Sans","Noto Sans JP",sans-serif}
main{max-width:1080px;margin:0 auto;padding:24px 16px 80px;display:grid;gap:28px}h1{font-size:24px;margin:0}h2{font-size:18px;margin:0 0 10px;padding-bottom:4px;border-bottom:2px solid var(--line)}
h3{font-size:15px;margin:12px 0 6px}p{margin:0 0 6px}.m{color:var(--sub);font-size:13px}section{min-width:0}
.pill{display:inline-block;font-size:12px;font-weight:700;padding:1px 8px;border-radius:4px;border:1px solid var(--line)}.pill.open,.pill.must{color:var(--warn);border-color:var(--warn)}
.pill.decided,.pill.rec{color:var(--ok);border-color:var(--ok)}.vars{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(min(100%,420px),1fr))}
.var{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;min-width:0}.var.rec{border:2px solid var(--ok)}
.var img{display:block;width:100%;height:auto;border:1px solid var(--line);border-radius:4px;margin:8px 0}
.acts{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}.acts.back{margin-top:16px}.acts input{flex:1 1 260px;min-width:0;font:inherit;padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--ink)}
button{font:inherit;font-size:14px;padding:6px 12px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer}button.go{background:var(--ok);border-color:var(--ok);color:#fff;font-weight:700}
button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}.flow{margin:0;padding-left:1.4em}
.scr{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;margin-bottom:12px}.tw{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:14px}th,td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}th{width:11em;color:var(--sub);font-weight:700;white-space:nowrap}
dl{margin:0;display:grid;grid-template-columns:max-content 1fr;gap:2px 12px}dt{color:var(--sub)}dd{margin:0;min-width:0}ul{margin:0;padding-left:1.2em}
#msg{position:fixed;left:50%;bottom:16px;transform:translateX(-50%);background:var(--ink);color:var(--bg);padding:8px 14px;border-radius:6px;display:none}
"""

SCRIPT = r"""
const TOKEN=__TOKEN__;
function toast(t){const m=document.getElementById("msg");m.textContent=t;m.style.display="block";setTimeout(()=>m.style.display="none",4000)}
async function post(path,body){try{const r=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json","X-Console-Token":TOKEN},body:JSON.stringify(body)});
 const j=await r.json();if(!r.ok)return toast(j.error||"できませんでした");toast(j.message||"受け付けました");setTimeout(()=>location.reload(),1200)}catch(e){toast("送れませんでした")}}
document.addEventListener("click",e=>{const b=e.target.closest("button[data-a]");if(!b||!/^\d+$/.test(b.dataset.c||""))return;
 const id=b.dataset.c,note=(document.getElementById("note")||{}).value||"";
 if(b.dataset.a==="choose")return post(`/api/choices/${id}/choose`,{option:b.dataset.o,reason:note});
 if(b.dataset.a==="flag"){if(!note.trim())return toast("違和感の一言を下の欄に入れてください");return post(`/api/choices/${id}/flag`,{option:b.dataset.o,note})}
 if(!note.trim())return toast("直しの中身を一言入れてください");post(`/api/choices/${id}/return`,{reason:note})});
"""
