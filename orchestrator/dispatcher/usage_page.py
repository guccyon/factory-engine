"""使用量のページ（操業コンソールの /usage）。usage.view の結果を HTML にする。

- 操業コンソールの /usage は、ほかのページと同じタブとヘッダの画面の中に、body() の断片（GET /usage/body）を読み込んで出す（2026-11-08。
  前は別の 1 枚のページで、開くとタブが消えて戻れなかった）。render() は断片を 1 枚のページに包んだもの（単体で見るとき・テスト）

- 組み立ては純粋な関数（render）。外部のライブラリ・CDN・スクリプトを使わない（グラフはインラインの SVG）
- 読み手はアカウントを足すかを決める人。先に「判断の材料」、次に今週・日ごと・役割ごと・モデルごとの順に並べる
"""
from __future__ import annotations

import html
from datetime import datetime

PATH = "/usage"
BODY_PATH = "/usage/body"
LEVEL_JA = {"bad": "要対応", "warn": "注意", "ok": "問題なし"}


def esc(v) -> str:
    return html.escape(str(v), quote=True)


def usd(v) -> str:
    return "—" if v is None else f"${v:,.0f}" if abs(v) >= 100 else f"${v:,.2f}"


def mtok(v) -> str:
    return f"{(v or 0) / 1e6:,.1f}M"


def pct(v) -> str:
    return "—" if v is None else f"{v * 100:.0f}%"


def when(s: str | None) -> str:
    return "—" if not s else datetime.fromisoformat(s).strftime("%m/%d %H:%M")


def bars(daily: list[dict]) -> str:
    """日ごとの費用の棒と、その日の予算の線（SVG）"""
    w, h, pad = 640, 180, 28
    top = max([d["cost_usd"] for d in daily] + [d["budget_usd"] or 0 for d in daily] + [1])
    bw = (w - pad) / max(len(daily), 1)
    out = [f'<svg viewBox="0 0 {w} {h + 24}" role="img" aria-label="日ごとの費用と予算" class="chart">']
    for i, d in enumerate(daily):
        x = pad + i * bw
        bh = d["cost_usd"] / top * h
        cls = "bar over" if d["budget_usd"] and d["cost_usd"] >= d["budget_usd"] else "bar"
        out.append(f'<rect class="{cls}" x="{x + 2:.1f}" y="{h - bh:.1f}" width="{max(bw - 4, 1):.1f}" height="{bh:.1f}"><title>{esc(d["date"])}: {usd(d["cost_usd"])}'
                   f'（予算 {usd(d["budget_usd"])}、{d["jobs"]} ジョブ）</title></rect>')
        if d["budget_usd"]:
            y = h - d["budget_usd"] / top * h
            out.append(f'<line class="budget" x1="{x:.1f}" x2="{x + bw:.1f}" y1="{y:.1f}" y2="{y:.1f}"/>')
        if len(daily) <= 16 or i % 2 == 0:
            out.append(f'<text x="{x + bw / 2:.1f}" y="{h + 16}" text-anchor="middle">{esc(d["date"][5:])}</text>')
    out.append(f'<text x="0" y="12">{usd(top)}</text></svg>')
    return "".join(out)


def table(cols: list[tuple[str, str]], rows: list[dict], fmt: dict) -> str:
    head = "".join(f"<th>{esc(j)}</th>" for _, j in cols)
    body = "".join("<tr>" + "".join(f"<td>{fmt.get(k, esc)(r.get(k))}</td>" for k, _ in cols) + "</tr>" for r in rows)
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body or "<tr><td>記録なし</td></tr>"}</tbody></table></div>'


def body(v: dict) -> str:
    """使用量のページの中身（HTML の断片）。スタイルは .usage の下に閉じる"""
    wk = v["week"]
    sig = "".join(f'<li class="{esc(s["level"])}"><b>{esc(LEVEL_JA.get(s["level"], s["level"]))}</b> {esc(s["text"])}</li>' for s in v["signals"])
    limit = (f'週の上限の目安 {usd(wk["limit_usd"])} の <b>{pct(wk["share"])}</b>'
             + (f'。このペースだと {when(wk["exhausted_at"])} ごろ届く' if wk["exhausted_at"] else "")
             if wk["limit_usd"] else "週の上限の目安は未設定（PLAN_WEEKLY_LIMIT_USD）")
    tiles = "".join(f'<div class="tile"><span>{esc(k)}</span><b>{val}</b></div>' for k, val in (
        ("今週の消費", usd(wk["cost_usd"])), ("1 日あたり", usd(wk["per_day_usd"])), ("週の見込み", usd(wk["projected_usd"])),
        ("課金対象のトークン", mtok(wk["billable_tokens"])), ("ジョブ", f'{wk["jobs"]:,}'), ("上限に当たった回数", f'{wk["usage_limited"]}')))
    daily = list(reversed(v["daily"]))
    css = """
.usage .m{color:var(--ink-sub);font-size:12px}.usage .note{margin:12px 0 0}.usage section{margin-top:12px;background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:14px}
.usage h2{font-size:14px;margin:0 0 8px}.usage ul.sig{list-style:none;margin:0;padding:0;display:grid;gap:6px}
.usage ul.sig li{padding:8px 10px;border-radius:8px;border:1px solid var(--line);border-left:4px solid var(--ok)}.usage ul.sig li.warn{border-left-color:var(--warn)}.usage ul.sig li.bad{border-left-color:var(--p0)}
.usage ul.sig b{margin-right:6px}.usage .tiles{display:grid;gap:8px;grid-template-columns:repeat(auto-fill,minmax(140px,1fr))}
.usage .tile{border:1px solid var(--line);border-radius:8px;padding:8px}.usage .tile span{display:block;color:var(--ink-sub);font-size:12px}.usage .tile b{font-size:18px}
.usage .chart{width:100%;max-width:760px;height:auto;display:block}.usage .chart .bar{fill:var(--accent)}.usage .chart .bar.over{fill:var(--warn)}.usage .chart .budget{stroke:var(--p0);stroke-width:2;stroke-dasharray:4 3}
.usage .chart text{fill:var(--ink-sub);font-size:11px}.usage .scroll{overflow-x:auto}.usage table{border-collapse:collapse;width:100%;font-size:13px}
.usage th,.usage td{text-align:right;padding:4px 8px;border-bottom:1px solid var(--line);white-space:nowrap}.usage th:first-child,.usage td:first-child{text-align:left}.usage th{color:var(--ink-sub);font-weight:600}
"""
    return f"""<style>{css}</style><div class="usage">
<p class="m note">{esc(when(v["generated_at"]))} 時点。費用は API 単価に換算した消費の目安で、定額の契約の実請求ではない</p>
<section><h2>アカウントを足すかの判断の材料</h2><ul class="sig">{sig}</ul></section>
<section><h2>今週（{esc(when(wk["start"]))} 〜 {esc(when(wk["end"]))}、契約の週の区切り）</h2><div class="tiles">{tiles}</div>
<p class="m">{limit}。最後に上限に当たったのは {esc(when(wk["last_usage_limited"]))}</p></section>
<section><h2>日ごとの消費（{v["day_start_hour"]}:00 区切り）</h2>{bars(v["daily"])}
<p class="m">棒が費用、赤い破線がその日の予算の枠の合計。予算に届いた日は橙</p>
{table([("date", "日"), ("cost_usd", "費用"), ("budget_usd", "予算"), ("billable_tokens", "課金対象"), ("total_tokens", "キャッシュ読み込み含む"),
        ("jobs", "ジョブ"), ("failed_cost_usd", "失敗したジョブの費用"), ("budget_stopped_hours", "予算で止めた時間"), ("usage_limited", "上限")],
       daily, {"cost_usd": usd, "budget_usd": usd, "failed_cost_usd": usd, "billable_tokens": mtok, "total_tokens": mtok,
               "budget_stopped_hours": lambda h: f"{h:.1f} 時間" if h else "—"})}</section>
<section><h2>役割ごと（直近 7 日）</h2>
{table([("ja", "役割"), ("cost_usd", "費用"), ("share", "割合"), ("billable_tokens", "課金対象"), ("jobs", "ジョブ")], v["by_kind"],
       {"cost_usd": usd, "share": pct, "billable_tokens": mtok})}</section>
<section><h2>モデルごと（直近 7 日）</h2>
{table([("model", "モデル"), ("cost_usd", "費用"), ("input_tokens", "入力"), ("output_tokens", "出力"), ("cache_write_tokens", "キャッシュ書き込み"),
        ("cache_read_tokens", "キャッシュ読み込み"), ("sessions", "セッション")], v["by_model"],
       {"cost_usd": usd, "input_tokens": mtok, "output_tokens": mtok, "cache_write_tokens": mtok, "cache_read_tokens": mtok})}</section>
</div>"""


def render(v: dict) -> str:
    """断片を 1 枚のページに包む（単体で見るとき）。色はコンソールと同じ名前の変数"""
    return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>工場の使用量</title>
<style>
:root{{--bg:#f6f7f9;--surface:#fff;--ink:#1d2330;--ink-sub:#5b6474;--line:#e2e5ea;--accent:#2f5bd3;--p0:#c62828;--warn:#b45309;--ok:#1f7a4d}}
@media (prefers-color-scheme:dark){{:root{{--bg:#12151b;--surface:#1b2029;--ink:#e6e9ef;--ink-sub:#9aa3b2;--line:#2a313d;--accent:#7aa2ff}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.6 system-ui,-apple-system,"Hiragino Sans","Noto Sans JP",sans-serif}}
main{{padding:16px;max-width:980px;margin:0 auto}}h1{{font-size:16px;margin:0}}
</style></head><body><main><h1>工場の使用量</h1><a href="/">操業コンソールへ</a>{body(v)}</main></body></html>"""
