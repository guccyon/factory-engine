"""定期レポートの報告書（HTML）とひとことの要約（ADR 0032 の決定 9）。

Slack には全文を流さず、この報告書を添付し、本文は数行の要約だけにする（2026-11-06 オーナー「HTML で可視化した報告書を添付し、
三行サマリくらいのひとことを投稿してほしい」）。文面（report.render）と同じく、種類を知らず Report の型だけを見る。
ひとことは何をやったかの要約でなく所感（順調か、計画が崩れる兆候はないか、早めに打てる手はないか）。見立て（report_health.assess など）が
Report.summary・level・findings に詰め、報告書は異常の印（Item.level）をハイライトし、推移（Item.series）を図で見せる（2026-11-06 オーナー）。

報告書は 1 枚で完結する HTML（外部の CSS・JS・フォントを読まない）。Slack から落として開いても、オフラインでも同じに見える。
"""
from __future__ import annotations

import html
from datetime import timedelta

import report
from report import Report

SUMMARY_MAX = 3
LEVEL = {"ok": ("順調", "●", ":large_green_circle:"), "warn": ("注意", "▲", ":large_yellow_circle:"), "bad": ("危険", "■", ":red_circle:")}


def period(rep: Report) -> str:
    s, e = rep.start.astimezone(report.TZ), (rep.end - timedelta(seconds=1)).astimezone(report.TZ)
    return f"{s:%Y-%m-%d}" if rep.end - rep.start <= timedelta(days=1) else f"{s:%Y-%m-%d}〜{e:%Y-%m-%d}"


def summary(rep: Report, party: str = "provider") -> list[str]:
    """ひとことの所感（SUMMARY_MAX 行まで）。見立てが詰めた行を優先し、なければ見せてよい節の先頭の数から"""
    if rep.summary:
        return rep.summary[:SUMMARY_MAX]
    out = []
    for s in report.visible(rep, party):
        ms = [i for i in s.items if i.kind == "metric"][:2]
        if ms:
            out.append(f"{s.title}: " + "、".join(f"{i.label} {report._num(i.value, i.unit)}" for i in ms))
        if len(out) >= SUMMARY_MAX:
            break
    return out


def comment(rep: Report, party: str = "provider", factory: str = "") -> str:
    """Slack の投稿の本文（mrkdwn）。見立てと見出し 1 行、所感。詳しくは添付の報告書"""
    head = f"{f'[{factory}] ' if factory and party == 'provider' else ''}{rep.title}（{period(rep)}）"
    lv = LEVEL.get(rep.level)
    lines = [(f"{lv[2]} *{lv[0]}* " if lv else "") + f"*{head}*"] + [f"• {t}" for t in summary(rep, party)]
    if rep.provisional:
        lines.append("_項目と閾値は仮置きです。詳しくは添付の報告書を開いてください_")
    return "\n".join(lines)


def filename(rep: Report) -> str:
    names = {"report.factory_health": "factory-operations", "report.product": "development-status"}
    return f"{names.get(rep.kind, rep.kind.replace('report.', '').replace('_', '-'))}-{rep.start.astimezone(report.TZ):%Y-%m-%d}.html"


# ------------------------------------------------------------------ 報告書
CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#1b1f24;--sub:#5b6570;--line:#e3e6ea;--accent:#d9480f;--chip:#eef1f4;--na:#8a94a0;
--bar:#2a78d6;--bar-dim:#a9c8ee;--good:#0ca30c;--warn:#fab219;--bad:#d03b3b;--warn-bg:#fff6e0;--bad-bg:#fdecec;--warn-ink:#8a5a00;--bad-ink:#a32020}
@media (prefers-color-scheme:dark){:root{--bg:#121417;--card:#1b1e22;--ink:#e8eaed;--sub:#9aa4ae;--line:#2c3137;--accent:#ff7a3d;--chip:#262a30;--na:#6f7983;
--bar:#3987e5;--bar-dim:#2b4a70;--warn-bg:#3a2f12;--bad-bg:#3d1c1c;--warn-ink:#fac35a;--bad-ink:#ff8a80}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 -apple-system,BlinkMacSystemFont,"Hiragino Sans","Noto Sans JP","Segoe UI",sans-serif}
main{max-width:920px;margin:0 auto;padding:24px 16px 48px}
header{margin-bottom:16px}
.eyebrow{color:var(--sub);font-size:13px;letter-spacing:.04em}
h1{font-size:24px;margin:4px 0 8px;line-height:1.3}
.badge{display:inline-block;font-size:12px;padding:2px 8px;border-radius:999px;background:var(--chip);color:var(--sub);margin-right:6px}
.badge.prov{color:var(--accent)}
.verdict{display:inline-flex;gap:6px;align-items:center;font-weight:700;font-size:13px;padding:3px 10px;border-radius:999px;margin-right:6px}
.verdict.ok{background:#e7f6e7;color:#0b6e0b}.verdict.warn{background:var(--warn-bg);color:var(--warn-ink)}.verdict.bad{background:var(--bad-bg);color:var(--bad-ink)}
@media (prefers-color-scheme:dark){.verdict.ok{background:#16301a;color:#6fd36f}}
.summary{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--accent);border-radius:10px;padding:14px 18px;margin:16px 0}
.summary.ok{border-left-color:var(--good)}.summary.warn{border-left-color:var(--warn)}.summary.bad{border-left-color:var(--bad)}
.summary p{margin:0 0 6px}.summary p:first-child{font-weight:700}.summary p:last-child{margin:0}
h2{font-size:16px;margin:0 0 12px}
section,.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin-bottom:16px}
ul.findings{list-style:none;margin:0;padding:0}
ul.findings li{padding:10px 0;border-top:1px solid var(--line)}ul.findings li:first-child{border-top:0;padding-top:0}
.tag{display:inline-block;font-size:12px;font-weight:700;padding:1px 8px;border-radius:999px;margin-right:6px}
.tag.warn{background:var(--warn-bg);color:var(--warn-ink)}.tag.bad{background:var(--bad-bg);color:var(--bad-ink)}
.action{color:var(--sub);font-size:14px;margin-top:2px}.action b{color:var(--ink);font-weight:600}
.charts{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px}
.chart{background:var(--bg);border-radius:10px;padding:10px 12px}
.chart .ct{font-size:13px;color:var(--sub);margin-bottom:4px}.chart .cv{font-size:20px;font-weight:700;font-variant-numeric:tabular-nums}
.chart svg{display:block;width:100%;height:auto}
.chart rect.b{fill:var(--bar-dim)}.chart rect.b.last{fill:var(--bar)}.chart rect.b:hover{fill:var(--bar)}
.chart text{font-size:10px;fill:var(--sub)}.chart line{stroke:var(--line)}
.metrics{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:10px;margin-bottom:8px}
.metric{background:var(--bg);border-radius:10px;padding:10px 12px;border:2px solid transparent}
.metric.warn{border-color:var(--warn);background:var(--warn-bg)}.metric.bad{border-color:var(--bad);background:var(--bad-bg)}
.metric .label{color:var(--sub);font-size:12.5px;line-height:1.4}
.metric .value{font-size:24px;font-weight:700;font-variant-numeric:tabular-nums;line-height:1.3;margin-top:4px}
.metric .value .unit{font-size:13px;font-weight:500;color:var(--sub);margin-left:3px}
.metric .value.na{color:var(--na);font-size:18px}
.metric .foot{display:flex;justify-content:space-between;align-items:flex-end;gap:6px;margin-top:2px}
.delta{font-size:12px;color:var(--sub)}
.spark{width:72px;height:22px;flex:none}.spark polyline{fill:none;stroke:var(--bar);stroke-width:1.6}.spark circle{fill:var(--bar)}
ul.entries{list-style:none;margin:8px 0 0;padding:0}
ul.entries li{display:flex;gap:10px;align-items:baseline;justify-content:space-between;padding:8px 0;border-top:1px solid var(--line)}
ul.entries li:first-child{border-top:0}
ul.entries li.warn,ul.entries li.bad{padding-left:8px;border-left:3px solid var(--warn)}ul.entries li.bad{border-left-color:var(--bad)}
ul.entries a{color:var(--ink);text-decoration:underline;text-decoration-color:var(--line);text-underline-offset:3px}
.state{flex:none;font-size:12px;padding:2px 8px;border-radius:999px;background:var(--chip);color:var(--sub);white-space:nowrap}
p.note{color:var(--sub);margin:8px 0 0}
footer{color:var(--sub);font-size:12px;margin-top:24px}
"""


def _e(s) -> str:
    return html.escape(str(s), quote=True)


def _nums(series) -> list:
    return [x if isinstance(x, (int, float)) else None for x in series or []]


def _spark(series) -> str:
    vals = _nums(series)
    pts = [(i, v) for i, v in enumerate(vals) if v is not None]
    if len(pts) < 2:
        return ""
    lo, hi = min(v for _, v in pts), max(v for _, v in pts)
    w, h, n = 72, 22, max(len(vals) - 1, 1)
    y = lambda v: 2 + (h - 4) * (1 - ((v - lo) / (hi - lo) if hi > lo else 0.5))   # noqa: E731
    poly = " ".join(f"{2 + (w - 4) * i / n:.1f},{y(v):.1f}" for i, v in pts)
    li, lv = pts[-1]
    return (f'<svg class="spark" viewBox="0 0 {w} {h}" aria-hidden="true"><polyline points="{poly}"/>'
            f'<circle cx="{2 + (w - 4) * li / n:.1f}" cy="{y(lv):.1f}" r="2.2"/></svg>')


def _tag(level: str) -> str:
    lv = LEVEL.get(level)
    return f'<span class="tag {level}">{lv[1]} {lv[0]}</span>' if lv and level != "ok" else ""


def _metric(i) -> str:
    if i.value is None:
        val = '<div class="value na">不明</div>'
    else:
        v = report._num(i.value, "")
        val = f'<div class="value">{_e(v)}<span class="unit">{_e(i.unit)}</span></div>' if i.unit else f'<div class="value">{_e(v)}</div>'
    d = ""   # 増えて良いか悪いかは数ごとに違うので、色を付けずに差だけを出す。良し悪しは見立て（level）が示す
    if isinstance(i.delta, (int, float)) and i.delta != 0:
        dv = round(i.delta, 1) if isinstance(i.delta, float) and abs(i.delta) < 100 else round(i.delta)
        d = f'前日比 {"+" if i.delta > 0 else ""}{_e(dv)}{_e(i.unit) if i.unit in ("%", "件", "分", "時間", "回", "日") else ""}'
    cls = f" {i.level}" if i.level in ("warn", "bad") else ""
    return (f'<div class="metric{cls}"><div class="label">{_tag(i.level)}{_e(i.label)}</div>{val}'
            f'<div class="foot"><span class="delta">{d}</span>{_spark(i.series)}</div></div>')


def _entry(i) -> str:
    label = f'<a href="{_e(i.url)}">{_e(i.label)}</a>' if i.url.startswith("https://") else _e(i.label)
    state = f'<span class="state">{_e(i.state)}</span>' if i.state else ""
    cls = f' class="{i.level}"' if i.level in ("warn", "bad") else ""
    return f"<li{cls}><span>{_tag(i.level)}{label}</span>{state}</li>"


def _section(s) -> str:
    out, ms, es = [f"<section><h2>{_e(s.title)}</h2>"], [], []

    def flush():
        if ms:
            out.append('<div class="metrics">' + "".join(ms) + "</div>")
            ms.clear()
        if es:
            out.append('<ul class="entries">' + "".join(es) + "</ul>")
            es.clear()

    for i in s.items:
        if i.kind == "metric":
            if es:
                flush()
            ms.append(_metric(i))
        elif i.kind == "entry":
            if ms:
                flush()
            es.append(_entry(i))
        else:
            flush()
            out.append(f'<p class="note">{_e(i.text)}</p>')
    flush()
    out.append("</section>")
    return "".join(out)


def _bars(title: str, item, days: list[str]) -> str:
    """推移の棒グラフ（期間ごと 1 本、最後が対象の期間）。棒にかざすと日付と値が出る"""
    vals = _nums(item.series)
    if sum(v is not None for v in vals) < 2:
        return ""
    w, h, top, bottom = 260, 100, 16, 16
    hi = max([v for v in vals if v is not None] + [0])
    lo = min([v for v in vals if v is not None] + [0])
    rng = (hi - lo) or 1
    n = len(vals)
    bw = (w - 4) / n
    base = top + (h - top - bottom) * (hi / rng) if lo < 0 else h - bottom
    bars = []
    for k, v in enumerate(vals):
        day = days[k] if k < len(days) else ""
        tip = f"{day}: {report._num(v, item.unit)}"
        if v is None:
            bars.append(f'<text x="{2 + k * bw + bw / 2:.1f}" y="{h - bottom - 2}" text-anchor="middle"><title>{_e(tip)}</title>·</text>')
            continue
        bh = max((h - top - bottom) * abs(v) / rng, 1.5 if v else 0)
        yv = base - bh if v >= 0 else base
        cls = "b last" if k == n - 1 else "b"
        bars.append(f'<rect class="{cls}" x="{2 + k * bw + 1:.1f}" y="{yv:.1f}" width="{max(bw - 2, 1):.1f}" height="{bh:.1f}" rx="2">'
                    f"<title>{_e(tip)}</title></rect>")
    first, last = (days[0][5:] if days else ""), (days[-1][5:] if days else "")
    axis = (f'<line x1="2" x2="{w - 2}" y1="{base:.1f}" y2="{base:.1f}"/>'
            f'<text x="2" y="{h - 3}">{_e(first)}</text><text x="{w - 2}" y="{h - 3}" text-anchor="end">{_e(last)}</text>'
            f'<text x="{w - 2}" y="10" text-anchor="end">最大 {_e(report._num(hi, ""))}</text>')
    cur = report._num(item.value, item.unit)
    return (f'<div class="chart"><div class="ct">{_e(title)}</div><div class="cv">{_e(cur)}</div>'
            f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="{_e(title)}の推移">{axis}{"".join(bars)}</svg></div>')


def render_html(rep: Report, party: str = "provider", factory: str = "") -> str:
    """1 枚で完結する報告書。見立て → 所感 → 兆候と打ち手 → 推移 → 節の数。見せてよい節だけを載せる（決定 3）"""
    eyebrow = " · ".join(x for x in (factory if party == "provider" else "", "定期レポート") if x)
    lv = LEVEL.get(rep.level)
    badges = (f'<span class="verdict {rep.level}">{lv[1]} {lv[0]}</span>' if lv else "") + f'<span class="badge">対象 {_e(period(rep))}</span>'
    badges += '<span class="badge prov">項目と閾値は仮置き</span>' if rep.provisional else ""
    built = f"{rep.built_at.astimezone(report.TZ):%Y-%m-%d %H:%M}" if rep.built_at else "不明"
    secs = report.visible(rep, party)
    shown = {s.id for s in secs}
    lines = "".join(f"<p>{_e(t)}</p>" for t in summary(rep, party))
    found = "".join(f'<li>{_tag(f["level"])}{_e(f["text"])}<div class="action"><b>打ち手</b> {_e(f["action"])}</div></li>' for f in rep.findings)
    items = {(s.id, i.label): i for s in secs for i in s.items if i.kind == "metric"}
    charts = "".join(_bars(c["title"], items[(c["section"], c["label"])], rep.days) for c in rep.charts
                     if c["section"] in shown and (c["section"], c["label"]) in items)
    span = f"直近 {len(rep.days)} 日" if rep.days else ""
    return (
        '<!doctype html><html lang="ja"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{_e(rep.title)}（{_e(period(rep))}）</title><style>{CSS}</style></head><body><main>"
        f'<header><div class="eyebrow">{_e(eyebrow)}</div><h1>{_e(rep.title)}</h1>{badges}</header>'
        + (f'<div class="summary {_e(rep.level)}">{lines}</div>' if lines else "")
        + (f'<div class="card"><h2>兆候と打ち手</h2><ul class="findings">{found}</ul></div>' if found else "")
        + (f'<div class="card"><h2>推移（{_e(span)}）</h2><div class="charts">{charts}</div></div>' if charts else "")
        + "".join(_section(s) for s in secs)
        + f"<footer>作成 {_e(built)}（{_e(report.TZ.key)}）。値が取れなかった数は「不明」と出しています。"
          "注意・危険の印は仮置きの閾値で付けています。</footer>"
        "</main></body></html>"
    )
