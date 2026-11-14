"""製品のページ（操業コンソールの /products と /products/<owner>/<repo>。ADR 0045 の追記、08_console-product.md の 4.2）。

- /products は製品の一覧、/products/<製品> は製品のページ。ほかのページと同じタブとヘッダの画面の中に、GET /products/body?p=<製品> の
  断片を読み込んで出す（使用量のページと同じ作り。usage_page.py）
- 製品のページは上から: レベル（L1〜L5 と稼働 ready の線、次のレベルに足りない条件）→ 準備度メーター（項目ごとの粒度と根拠、
  仕事ごとの判定とバー・警告・お願い。仕事は切り替えられる）→ 技術スタックと外部サービス（下書き）
- 組み立ては純粋な関数。外部のライブラリ・CDN を使わない。鍵の値は中身に入っていない（onboarding.normalize が捨てる）
"""
from __future__ import annotations

import html
from datetime import datetime
from urllib.parse import quote

PATH = "/products"
BODY_PATH = "/products/body"
VERDICT_CLASS = {"blocked": "bad", "weak": "warn", "ready": "ok", "enough": "good"}

CSS = """
.prod .m{color:var(--sub);font-size:12px}.prod section{margin-top:12px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
.prod h2{font-size:14px;margin:0 0 10px}.prod h3{font-size:13px;margin:12px 0 6px;color:var(--sub);font-weight:600}
.prod .head{display:flex;flex-wrap:wrap;gap:6px 14px;align-items:baseline}.prod .head h1{font-size:18px;margin:0}
.prod .pbadge{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;border:1px solid var(--line);white-space:nowrap}
.prod .bad{color:var(--p0)}.prod .warn{color:var(--warn)}.prod .ok{color:var(--accent)}.prod .good{color:var(--ok)}
.prod .pbadge.bad{border-color:var(--p0)}.prod .pbadge.warn{border-color:var(--warn)}.prod .pbadge.ok{border-color:var(--accent)}.prod .pbadge.good{border-color:var(--ok)}
.prod .ladder{display:grid;grid-template-columns:repeat(5,1fr);gap:6px;margin:4px 0 6px}
.prod .rung{border:1px solid var(--line);border-radius:8px;padding:8px;text-align:center;position:relative;min-width:0}
.prod .rung b{display:block;font-size:13px}.prod .rung span{font-size:12px;color:var(--sub)}
.prod .rung.reached{background:var(--accent);border-color:var(--accent);color:#fff}.prod .rung.reached span{color:#fff;opacity:.85}
.prod .rung.next{border:2px solid var(--accent)}.prod .rung.later{opacity:.6}
.prod .rung.line{margin-left:6px}.prod .rung.line::before{content:"";position:absolute;left:-7px;top:-6px;bottom:-6px;border-left:2px dashed var(--warn)}
.prod .rlabel{font-size:11px;color:var(--warn);text-align:right;margin-top:2px}
.prod ul.conds{list-style:none;padding:0;margin:6px 0 0}.prod ul.conds li{padding:3px 0}.prod ul.conds .ck{display:inline-block;width:1.4em}
.prod .meter{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:14px}
@media (max-width:760px){.prod .meter{grid-template-columns:1fr}.prod .ladder{grid-template-columns:repeat(5,minmax(52px,1fr));overflow-x:auto}}
.prod details.it{border-top:1px solid var(--line);padding:5px 0}.prod details.it summary{cursor:pointer;display:flex;gap:8px;align-items:center;list-style:none}
.prod details.it summary::-webkit-details-marker{display:none}.prod details.it .nm{flex:1;min-width:0}
.prod .dots{display:inline-flex;gap:2px}.prod .dots i{display:inline-block;width:10px;height:10px;border-radius:2px;background:var(--line)}.prod .dots i.on{background:var(--accent)}
.prod .must{font-size:11px;color:var(--p0);border:1px solid var(--p0);border-radius:4px;padding:0 4px;white-space:nowrap}
.prod details.it .ev{font-size:12px;color:var(--sub);padding:4px 0 2px 18px}
.prod .tg{display:flex;flex-wrap:wrap;gap:4px;margin-bottom:8px}.prod .tg button{font:inherit;font-size:12px;padding:3px 10px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer}
.prod .tg button.on{background:var(--accent);border-color:var(--accent);color:#fff}
.prod .bar{height:10px;border-radius:5px;background:var(--line);overflow:hidden;margin:6px 0}.prod .bar i{display:block;height:100%}
.prod .bar i.bad{background:var(--p0)}.prod .bar i.warn{background:var(--warn)}.prod .bar i.ok{background:var(--accent)}.prod .bar i.good{background:var(--ok)}
.prod .verdict{font-size:16px;font-weight:700}.prod ul.ls{margin:4px 0;padding-left:18px}.prod ul.ls li{margin:2px 0}
.prod table{border-collapse:collapse;width:100%;font-size:12px}.prod td,.prod th{text-align:left;padding:5px 8px 5px 0;border-top:1px solid var(--line);vertical-align:top}
.prod th{color:var(--sub);font-weight:400;border-top:0}.prod .tbl{overflow-x:auto}
.prod .trend{display:flex;gap:2px;align-items:flex-end;height:26px;margin-top:4px}.prod .trend i{display:block;width:8px;border-radius:2px 2px 0 0}
.prod .rows a{display:flex;flex-wrap:wrap;gap:4px 14px;align-items:baseline;padding:10px 0;border-top:1px solid var(--line);color:var(--ink);text-decoration:none}
.prod .rows a:first-child{border-top:0}.prod .rows a:hover b{color:var(--accent)}
"""


def esc(v) -> str:
    return html.escape(str(v if v is not None else ""), quote=True)


def when(s) -> str:
    if not s:
        return "—"
    return (datetime.fromisoformat(s) if isinstance(s, str) else s).strftime("%m/%d %H:%M")


def pct(r) -> str:
    return f"{round((r or 0) * 100)}%"


def href(product: str) -> str:
    return f"{PATH}/{quote(product, safe='/')}"


def _wrap(inner: str) -> str:
    return f"<style>{CSS}</style><div class=\"prod\">{inner}</div>"


# ------------------------------------------------------------------ 一覧
def body_list(rows: list[dict]) -> str:
    if not rows:
        return _wrap('<section><h2>製品</h2><div class="m">オンボーディングの判定の記録がまだない。'
                     '記録は factoryctl readiness record（オンボーディングの道具が呼ぶ）で残す</div></section>')
    out = []
    for r in rows:
        w = r["weakest"]
        state = ("稼働 ready を承認済み" if r["approved"] else "稼働 ready を越えた" if r["past_ready"]
                 else "稼働 ready の承認待ち" if r["ready_ok"] else f"次は L{r['next']} {r['next_ja']}" if r["next"] else "")
        out.append(f'<a href="{href(r["product"])}" data-nav><b>{esc(r["product"])}</b>'
                   f'<span>L{r["reached"]} {esc(r["reached_ja"])}</span><span class="m">{esc(state)}</span>'
                   f'<span>いちばん弱い仕事: {esc(w["target_ja"])} <b class="{VERDICT_CLASS[w["verdict"]]}">{esc(w["verdict_ja"])}</b></span>'
                   f'<span class="m">お願い {r["asks"]} 件 · 判定 {when(r["at"])}</span></a>')
    return _wrap(f'<section><h2>製品（{len(rows)}）</h2><div class="rows">{"".join(out)}</div>'
                 '<div class="m" style="margin-top:8px">オンボーディングの判定の記録に書かれた製品の名前で束ねている（ジョブとチケットを製品に付けるのは Gen2）</div></section>')


# ------------------------------------------------------------------ 製品のページ
def _ladder(p: dict) -> str:
    line = p["ready_line_after"]
    cells = "".join(f'<div class="rung {r["state"]}{" line" if r["level"] == line + 1 else ""}"><b>L{r["level"]}</b><span>{esc(r["ja"])}</span></div>'
                    for r in p["rungs"])
    s = p["summary"]
    if p["approval"]:
        status = f'<span class="pbadge good">稼働 ready を承認済み（{when(p["approval"].get("at"))} {esc(p["approval"].get("by"))}）</span>'
    elif s["past_ready"]:
        status = '<span class="pbadge good">稼働 ready を越えている</span>'
    elif s["ready_ok"]:
        status = (f'<a class="pbadge ok" href="/inbox/{quote("readiness:ready:" + p["product"], safe="")}" data-nav>'
                  '稼働 ready の承認待ち（受信トレイ）→</a>')
    else:
        status = '<span class="pbadge">稼働 ready の前</span>'
    conds = ""
    if p["next_conditions"]:
        lis = []
        for c in p["next_conditions"]:
            link = (f' <a href="/inbox" data-nav class="m">受信トレイで確かめる →</a>' if c["human"] and not c["met"] else "")
            lis.append(f'<li><span class="ck {"good" if c["met"] else ""}">{"✓" if c["met"] else "○"}</span>{esc(c["ja"])}'
                       f'{"<span class=m>（事業主の確認）</span>" if c["human"] else ""}{link}</li>')
        nxt = next(r for r in p["rungs"] if r["state"] == "next")
        conds = f'<h3>L{nxt["level"]} {esc(nxt["ja"])} に足りないもの</h3><ul class="conds">{"".join(lis)}</ul>'
    else:
        conds = '<div class="m" style="margin-top:6px">L5 まで届いている</div>'
    return (f'<section><h2>オンボーディングのレベル {status}</h2><div class="ladder">{cells}</div>'
            f'<div class="rlabel">点線が稼働 ready の線（L{line} と L{line + 1} の間。越えるには事業主の承認が要る）</div>{conds}</section>')


def _item(it: dict, targets: dict) -> str:
    dots = "".join(f'<i class="{"on" if i <= it["level"] and it["measured"] else ""}"></i>' for i in range(1, len(it["levels"])))
    must = "".join(f'<span class="must" title="{esc(targets[t]["ja"])}の必須">必須: {esc(targets[t]["ja"])}</span>' for t in it["must"])
    ev = it.get("evidence") or {}
    evs = "・".join(x for x in (f'測った道具 {esc(ev["tool"])}' if ev.get("tool") else "", f'コミット {esc(ev["commit"])}' if ev.get("commit") else "",
                                f'確かめた人 {esc(ev["by"])}' if ev.get("by") else "", esc(ev.get("note") or "")) if x)
    scale = " → ".join(f'<b>{esc(x)}</b>' if i == it["level"] else esc(x) for i, x in enumerate(it["levels"]))
    return (f'<details class="it"><summary><span class="dots" aria-label="粒度 {it["level"]} / 3">{dots}</span>'
            f'<span class="nm">{esc(it["ja"])} <span class="m">{esc(it["level_ja"] if it["measured"] else "まだ測っていない")}</span></span>{must}</summary>'
            f'<div class="ev">粒度: {scale}<br>根拠: {evs or "記録なし"}</div></details>')


def _target(t: str, j: dict, first: bool) -> str:
    c = VERDICT_CLASS[j["verdict"]]
    bars = "".join(f'<i class="{VERDICT_CLASS[x["verdict"]]}" title="{when(x["at"])} {pct(x["ratio"])}" '
                   f'style="height:{max(3, round(x["ratio"] * 26))}px;background:var(--{ {"bad": "p0", "warn": "warn", "ok": "accent", "good": "ok"}[VERDICT_CLASS[x["verdict"]]] })"></i>'
                   for x in j["trend"][-12:])
    block = (f'<h3>始められない（必須の欠け）</h3><ul class="ls">{"".join(f"<li>{esc(x)}</li>" for x in j["blocked_ja"])}</ul>' if j["blocked_ja"] else "")
    warns = (f'<h3>警告</h3><ul class="ls">{"".join(f"<li><b>{esc(w["ja"])}</b>: {esc(w["warn"])}</li>" for w in j["warnings"])}</ul>' if j["warnings"] else "")
    asks = (f'<h3>事業主へのお願い</h3><ul class="ls">{"".join(f"<li>{esc(a["ask"])} <span class=m>（{esc(a["ja"])}）</span></li>" for a in j["asks"])}</ul>'
            if j["asks"] else '<div class="m" style="margin-top:8px">お願いはない</div>')
    return (f'<div data-tgt="{esc(t)}" {"" if first else "hidden"}><div class="verdict {c}">{esc(j["verdict_ja"])}</div>'
            f'<div class="bar"><i class="{c}" style="width:{pct(j["ratio"])}"></i></div>'
            f'<div class="m">目標にどれだけ近いか {pct(j["ratio"])}（0.7 未満は品質が出にくい、0.92 以上は十分。必須が最低に届かなければ始められない）</div>'
            f'{f"<div class=trend title=判定の推移>{bars}</div>" if len(j["trend"]) > 1 else ""}{block}{warns}{asks}</div>')


def _meter(p: dict) -> str:
    ts = p["targets"]
    order = list(ts)
    weakest = p["summary"]["weakest"]["target"]
    first = weakest if weakest in ts else order[0]
    tabs = "".join(f'<button type="button" data-tgt-b="{esc(t)}" class="{"on" if t == first else ""}">{esc(ts[t]["ja"])} '
                   f'<span class="{VERDICT_CLASS[ts[t]["verdict"]] if t != first else ""}">●</span></button>' for t in order)
    groups = "".join(f'<h3>{esc(gja)}</h3>' + "".join(_item(it, ts) for it in p["items"] if it["group"] == g) for g, gja in p["groups"].items())
    return (f'<section><h2>準備度メーター <span class="m">判定 {when(p["at"])}・コミット {esc(p.get("commit") or "—")}・記録 {p["assessments"]} 回</span></h2>'
            f'<div class="meter"><div><div class="m">項目ごとの今の粒度（押すと根拠）</div>{groups}</div>'
            f'<div><div class="tg" role="tablist">{tabs}</div>{"".join(_target(t, ts[t], t == first) for t in order)}</div></div></section>')


def _stack(p: dict) -> str:
    st = p["stack"]
    repos = "".join(f'<tr><td><code>{esc(r.get("repo"))}</code></td><td>{esc(r.get("role"))}</td><td>{esc("・".join(r.get("languages") or []))}</td>'
                    f'<td>{esc("・".join(r.get("frameworks") or []))}</td><td>{esc("・".join(r.get("tests") or []))}</td></tr>' for r in st["repos"])
    svcs = "".join(f'<tr><td><b>{esc(s.get("name"))}</b></td><td>{esc(s.get("role"))}</td><td>{esc("・".join(s.get("repos") or []))}</td>'
                   f'<td class="{"warn" if s.get("sandbox") in ("no", "unknown") else ""}">{esc(s["sandbox_ja"])}</td><td>{esc(s["handling_ja"])}</td>'
                   f'<td class="m">{esc(s.get("note"))}</td></tr>' for s in st["services"])
    draft = (f'<span class="pbadge warn">下書き（正本は製品のリポジトリ）</span> <span class="m">コミット {esc(st.get("commit") or "—")} から作った</span>'
             if st["draft"] else f'<span class="m">コミット {esc(st.get("commit") or "—")}</span>')
    return (f'<section><h2>技術スタックと外部サービス {draft}</h2>'
            f'<h3>リポジトリ</h3>{f"<div class=tbl><table><tr><th>リポジトリ</th><th>役割</th><th>言語</th><th>枠組み</th><th>テストの道具</th></tr>{repos}</table></div>" if repos else "<div class=m>まだない</div>"}'
            f'<h3>製品に組み込まれた外部サービス</h3>'
            f'{f"<div class=tbl><table><tr><th>サービス</th><th>役割</th><th>使うリポジトリ</th><th>検証用の環境</th><th>工場の環境での扱い</th><th></th></tr>{svcs}</table></div>" if svcs else "<div class=m>まだない</div>"}'
            '<div class="m" style="margin-top:6px">検証用の鍵は値を出さず、あるかどうかだけを出す。「無い」「不明」は準備度の「技術スタックと外部サービスの地図」に効く。'
            '工場の生産に使うサービス（バックログ管理・デザインの資料など）は設定の<a href="/settings/connectors" data-nav>サービスとの連携</a></div></section>')


def body_product(p: dict | None, product: str = "") -> str:
    if not p:
        return _wrap(f'<section><h2>{esc(product)}</h2><div class="m">この製品の判定の記録がない。<a href="{PATH}" data-nav>製品の一覧へ</a></div></section>')
    head = (f'<div class="head"><h1>{esc(p["product"])}</h1><a class="m" href="{PATH}" data-nav>製品の一覧</a>'
            f'<span class="m">記録した道具・人 {esc(p.get("by") or "—")}</span></div>'
            + (f'<div class="m">{esc(p["note"])}</div>' if p.get("note") else ""))
    return _wrap(head + _ladder(p) + _meter(p) + _stack(p))
