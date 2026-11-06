"""定期レポートの報告書（HTML）とひとことの要約（ADR 0032 の決定 9）。

Slack には全文を流さず、この報告書を添付し、本文は数行の要約だけにする（2026-11-06 オーナー「HTML で可視化した報告書を添付し、
三行サマリくらいのひとことを投稿してほしい」）。文面（report.render）と同じく、種類を知らず Report の型だけを見る。
要約の行は組み立て（report_health.py など）が Report.summary に詰める。なければ節の先頭の数から作る。

報告書は 1 枚で完結する HTML（外部の CSS・JS・フォントを読まない）。Slack から落として開いても、オフラインでも同じに見える。
"""
from __future__ import annotations

import html
from datetime import timedelta

import report
from report import Report

SUMMARY_MAX = 3


def period(rep: Report) -> str:
    s, e = rep.start.astimezone(report.TZ), (rep.end - timedelta(seconds=1)).astimezone(report.TZ)
    return f"{s:%Y-%m-%d}" if rep.end - rep.start <= timedelta(days=1) else f"{s:%Y-%m-%d}〜{e:%Y-%m-%d}"


def summary(rep: Report, party: str = "provider") -> list[str]:
    """ひとことの要約（SUMMARY_MAX 行まで）。組み立てが詰めた行を優先し、なければ見せてよい節の先頭の数から"""
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
    """Slack の投稿の本文（mrkdwn）。見出し 1 行と要約。詳しくは添付の報告書"""
    head = f"{f'[{factory}] ' if factory and party == 'provider' else ''}{rep.title}（{period(rep)}）"
    lines = [f"*{head}*"] + [f"• {t}" for t in summary(rep, party)]
    if rep.provisional:
        lines.append("_中身は仮置きです。詳しくは添付の報告書を開いてください_")
    return "\n".join(lines)


def filename(rep: Report) -> str:
    return f"{rep.kind.replace('report.', '').replace('_', '-')}-{rep.start.astimezone(report.TZ):%Y-%m-%d}.html"


# ------------------------------------------------------------------ 報告書
CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#1b1f24;--sub:#5b6570;--line:#e3e6ea;--accent:#d9480f;--chip:#eef1f4;--na:#8a94a0}
@media (prefers-color-scheme:dark){:root{--bg:#121417;--card:#1b1e22;--ink:#e8eaed;--sub:#9aa4ae;--line:#2c3137;--accent:#ff7a3d;--chip:#262a30;--na:#6f7983}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 -apple-system,BlinkMacSystemFont,"Hiragino Sans","Noto Sans JP","Segoe UI",sans-serif}
main{max-width:880px;margin:0 auto;padding:24px 16px 48px}
header{margin-bottom:20px}
.eyebrow{color:var(--sub);font-size:13px;letter-spacing:.04em}
h1{font-size:24px;margin:4px 0 8px;line-height:1.3}
.badge{display:inline-block;font-size:12px;padding:2px 8px;border-radius:999px;background:var(--chip);color:var(--sub);margin-right:6px}
.badge.warn{color:var(--accent)}
.summary{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--accent);border-radius:10px;padding:14px 18px;margin:16px 0 24px}
.summary ul{margin:0;padding-left:1.1em}
.summary li{margin:2px 0}
section{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin-bottom:16px}
h2{font-size:16px;margin:0 0 12px}
.metrics{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;margin-bottom:8px}
.metric{background:var(--bg);border-radius:10px;padding:10px 12px}
.metric .label{color:var(--sub);font-size:12.5px;line-height:1.4}
.metric .value{font-size:26px;font-weight:700;font-variant-numeric:tabular-nums;line-height:1.3;margin-top:4px}
.metric .value .unit{font-size:14px;font-weight:500;color:var(--sub);margin-left:3px}
.metric .value.na{color:var(--na);font-size:18px}
.delta{display:inline-block;font-size:12px;margin-top:2px;color:var(--sub)}
ul.entries{list-style:none;margin:8px 0 0;padding:0}
ul.entries li{display:flex;gap:10px;align-items:baseline;justify-content:space-between;padding:8px 0;border-top:1px solid var(--line)}
ul.entries li:first-child{border-top:0}
ul.entries a{color:var(--ink);text-decoration:underline;text-decoration-color:var(--line);text-underline-offset:3px}
.state{flex:none;font-size:12px;padding:2px 8px;border-radius:999px;background:var(--chip);color:var(--sub);white-space:nowrap}
p.note{color:var(--sub);margin:8px 0 0}
footer{color:var(--sub);font-size:12px;margin-top:24px}
"""


def _e(s) -> str:
    return html.escape(str(s), quote=True)


def _metric(i) -> str:
    if i.value is None:
        val = '<div class="value na">不明</div>'
    else:
        v = report._num(i.value, "")
        val = f'<div class="value">{_e(v)}<span class="unit">{_e(i.unit)}</span></div>' if i.unit else f'<div class="value">{_e(v)}</div>'
    d = ""   # 増えて良いか悪いかは数ごとに違うので、色を付けずに差だけを出す
    if isinstance(i.delta, (int, float)) and i.delta != 0:
        dv = round(i.delta, 1) if isinstance(i.delta, float) and abs(i.delta) < 100 else round(i.delta)
        d = f'<div class="delta">前日比 {"+" if i.delta > 0 else ""}{_e(dv)}{_e(i.unit) if i.unit in ("%", "件", "分", "時間", "回") else ""}</div>'
    return f'<div class="metric"><div class="label">{_e(i.label)}</div>{val}{d}</div>'


def _entry(i) -> str:
    label = f'<a href="{_e(i.url)}">{_e(i.label)}</a>' if i.url.startswith("https://") else _e(i.label)
    state = f'<span class="state">{_e(i.state)}</span>' if i.state else ""
    return f"<li><span>{label}</span>{state}</li>"


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


def render_html(rep: Report, party: str = "provider", factory: str = "") -> str:
    """1 枚で完結する報告書。見せてよい節だけを載せる（決定 3）"""
    eyebrow = " · ".join(x for x in (factory if party == "provider" else "", "定期レポート") if x)
    badges = '<span class="badge warn">中身は仮置き</span>' if rep.provisional else ""
    built = f"{rep.built_at.astimezone(report.TZ):%Y-%m-%d %H:%M}" if rep.built_at else "不明"
    items = "".join(f"<li>{_e(t)}</li>" for t in summary(rep, party))
    body = "".join(_section(s) for s in report.visible(rep, party))
    return (
        '<!doctype html><html lang="ja"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{_e(rep.title)}（{_e(period(rep))}）</title><style>{CSS}</style></head><body><main>"
        f'<header><div class="eyebrow">{_e(eyebrow)}</div><h1>{_e(rep.title)}</h1>'
        f'<span class="badge">対象 {_e(period(rep))}</span>{badges}</header>'
        + (f'<div class="summary"><ul>{items}</ul></div>' if items else "")
        + body
        + f"<footer>作成 {_e(built)}（{_e(report.TZ.key)}）。値が取れなかった数は「不明」と出しています。</footer>"
        "</main></body></html>"
    )
