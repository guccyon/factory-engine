#!/usr/bin/env python3
"""デザインシステムのカタログ（2026-11-01 オーナー: デザインシステムはカタログとセットが基本。デザインシステムのデザイナー役がカタログまで作る）。

カタログは製品の specs/design/ の正本から決定論で組み立てる 1 枚の HTML。正本とは別に持たないので、正本と食い違わない:
- トークン: tokens.yaml の color（roles・status）・typography（families・scale）・spacing（scale_px）・shape（radius_px）を CSS 変数にして並べる
- 部品: components.yaml の components[] の preview（状態ごとの見本の HTML 片）を並べる。見本はデザイナー役が書く
  見本は CSS 変数（var(--color-primary) など。名前は css_vars() を参照）と、components.yaml の preview_css（任意）だけで描く

見本の HTML はエージェントの出力なので、外部の入力と同じに扱う: スクリプト・イベント属性・外の URL を含む見本は validate_previews() で落とし、
配るときは prototype.py の囲い（別のオリジン・sandbox の CSP）の中だけで出す。

使い方:
  core-gates/design_catalog.py <spec のディレクトリ> <出力の html>   # 組み立てる（デザイナー役が自分で撮って確かめるのにも使う）
依存: python3、PyYAML
"""
from __future__ import annotations

import html
import re
import sys
from pathlib import Path

import yaml

MAX_PREVIEWS = 80                 # 見本の数の上限（部品 × 状態）
MAX_PREVIEW_CHARS = 4000          # 見本 1 つの HTML の上限
MAX_PREVIEW_CSS_CHARS = 12000
FORBIDDEN = [
    (re.compile(r"<\s*/?\s*(script|iframe|object|embed|link|meta|base|form)\b", re.I), "script・iframe・link などの要素"),
    (re.compile(r"\son[a-z]+\s*=", re.I), "イベント属性（onclick など）"),
    (re.compile(r"javascript:", re.I), "javascript: の URL"),
    (re.compile(r"(https?:)?//[A-Za-z0-9.-]+\.[A-Za-z]", re.I), "外の URL"),
    (re.compile(r"@import", re.I), "@import"),
]
VAR_NAME = re.compile(r"[^A-Za-z0-9-]+")


def _k(s) -> str:
    return VAR_NAME.sub("-", str(s)).strip("-").lower()


def _px(v) -> str | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return f"{v}px"
    if isinstance(v, str) and re.fullmatch(r"-?[0-9.]+(px|rem|em|%)", v.strip()):
        return v.strip()
    return None


def _color(v) -> str | None:
    if isinstance(v, str) and re.fullmatch(r"#[0-9A-Fa-f]{3,8}|(rgba?|hsla?)\([0-9., %/]+\)", v.strip()):
        return v.strip()
    return None


def _font(v) -> str | None:
    return v.strip() if isinstance(v, str) and len(v) <= 200 and not re.search(r"[;{}<>\\()]|url", v, re.I) else None


def css_vars(tokens: dict) -> list[tuple[str, str, str]]:
    """(変数名, 値, 分類) の一覧。分類は color / space / radius / font"""
    out: list[tuple[str, str, str]] = []
    color = tokens.get("color") if isinstance(tokens.get("color"), dict) else {}
    for k, v in (color.get("roles") or {}).items() if isinstance(color.get("roles"), dict) else []:
        if _color(v):
            out.append((f"--color-{_k(k)}", _color(v), "color"))
    for st, d in (color.get("status") or {}).items() if isinstance(color.get("status"), dict) else []:
        for k, v in (d or {}).items() if isinstance(d, dict) else []:
            if _color(v):
                out.append((f"--color-{_k(st)}-{_k(k)}", _color(v), "color"))
    sp = tokens.get("spacing") if isinstance(tokens.get("spacing"), dict) else {}
    for k, v in (sp.get("scale_px") or {}).items() if isinstance(sp.get("scale_px"), dict) else []:
        if _px(v):
            out.append((f"--space-{_k(k)}", _px(v), "space"))
    sh = tokens.get("shape") if isinstance(tokens.get("shape"), dict) else {}
    for k, v in (sh.get("radius_px") or {}).items() if isinstance(sh.get("radius_px"), dict) else []:
        if _px(v):
            out.append((f"--radius-{_k(k)}", _px(v), "radius"))
    ty = tokens.get("typography") if isinstance(tokens.get("typography"), dict) else {}
    for k, v in (ty.get("families") or {}).items() if isinstance(ty.get("families"), dict) else []:
        if _font(v):
            out.append((f"--font-{_k(k)}", _font(v), "font"))
    return out


def type_styles(tokens: dict) -> list[dict]:
    ty = tokens.get("typography") if isinstance(tokens.get("typography"), dict) else {}
    out = []
    for name, s in (ty.get("scale") or {}).items() if isinstance(ty.get("scale"), dict) else []:
        if isinstance(s, dict) and _px(s.get("size")):
            out.append({"name": _k(name), "size": _px(s.get("size")), "weight": int(s.get("weight") or 400) if str(s.get("weight") or "").isdigit() else 400,
                        "line_height": s.get("line_height") if isinstance(s.get("line_height"), (int, float)) else 1.5,
                        "letter_spacing": s.get("letter_spacing") if isinstance(s.get("letter_spacing"), str) and re.fullmatch(r"-?[0-9.]+em", s["letter_spacing"]) else "0",
                        "use": str(s.get("use") or "")})
    return out


def components_of(comp_yaml: dict) -> list[dict]:
    items = comp_yaml.get("components") if isinstance(comp_yaml, dict) else None
    return [c for c in items if isinstance(c, dict)] if isinstance(items, list) else []


def validate_previews(comp_yaml) -> list[str]:
    """部品ごとに見本があるか、見本に危ないものが入っていないか"""
    comps = components_of(comp_yaml)
    if not comps:
        return ["components.yaml に components（部品の一覧）がない"]
    errs, n = [], 0
    for i, c in enumerate(comps):
        cid = c.get("id") or f"components[{i}]"
        pv = c.get("preview")
        if not isinstance(pv, list) or not pv:
            errs.append(f"{cid}: preview（状態ごとの見本）がない。カタログに載せるため、部品ごとに 1 つ以上書く")
            continue
        for j, p in enumerate(pv):
            n += 1
            if not isinstance(p, dict) or not str(p.get("label") or "").strip() or not str(p.get("html") or "").strip():
                errs.append(f"{cid}.preview[{j}]: label と html が要る")
                continue
            h = str(p["html"])
            if len(h) > MAX_PREVIEW_CHARS:
                errs.append(f"{cid}.preview[{j}]: html が {len(h)} 字で長すぎる（{MAX_PREVIEW_CHARS} 字まで）")
            errs += [f"{cid}.preview[{j}]: {why}は使えない" for rx, why in FORBIDDEN if rx.search(h)]
    if n > MAX_PREVIEWS:
        errs.append(f"見本が {n} 個で多すぎる（{MAX_PREVIEWS} 個まで。状態は代表的なものに絞る）")
    css = comp_yaml.get("preview_css") if isinstance(comp_yaml, dict) else None
    if css is not None:
        if not isinstance(css, str):
            errs.append("preview_css は文字列")
        else:
            if len(css) > MAX_PREVIEW_CSS_CHARS:
                errs.append(f"preview_css が長すぎる（{MAX_PREVIEW_CSS_CHARS} 字まで）")
            errs += [f"preview_css: {why}は使えない" for rx, why in FORBIDDEN if rx.search(css)]
            if re.search(r"</\s*style", css, re.I):
                errs.append("preview_css: </style> は使えない")
    return errs


def _load(text: str | None) -> dict:
    try:
        d = yaml.safe_load(text or "") or {}
    except yaml.YAMLError:
        return {}
    return d if isinstance(d, dict) else {}


def build(files: dict[str, str]) -> str:
    """spec のファイル（名前 → 本文）からカタログの HTML を組み立てる。見本の検査は validate_previews() で済ませておくこと"""
    tokens, comp = _load(files.get("tokens.yaml")), _load(files.get("components.yaml"))
    concept = _load(files.get("concept_tone.yaml")).get("concept") or {}
    gov = _load(files.get("governance.yaml"))
    e = html.escape
    vars_ = css_vars(tokens)
    styles = type_styles(tokens)
    font = next((v for n, v, kind in vars_ if n == "--font-sans"), None) or next((v for n, v, kind in vars_ if kind == "font"), "sans-serif")
    root = "\n".join(f"  {n}: {v};" for n, v, _ in vars_)
    type_css = "\n".join(f".type-{s['name']} {{ font-size: {s['size']}; font-weight: {s['weight']}; line-height: {s['line_height']}; letter-spacing: {s['letter_spacing']}; }}" for s in styles)
    sw = "".join(f'<div class="sw"><div class="chip" style="background:var({n})"></div><code>{e(n[8:])}</code><span>{e(v)}</span></div>'
                 for n, v, kind in vars_ if kind == "color")
    ty = "".join(f'<div class="ty"><code>{e(s["name"])}</code><span class="meta">{e(s["size"])} / {s["weight"]}</span>'
                 f'<div class="type-{s["name"]}">{e(s["use"][:28]) or "あいうえお Aa 0123"}</div></div>' for s in styles)
    space = "".join(f'<div class="sp"><code>{e(n[8:])}</code><span class="meta">{e(v)}</span><i style="width:var({n})"></i></div>'
                    for n, v, kind in vars_ if kind == "space")
    radius = "".join(f'<div class="rd"><i style="border-radius:var({n})"></i><code>{e(n[9:])}</code><span class="meta">{e(v)}</span></div>'
                     for n, v, kind in vars_ if kind == "radius")
    parts = []
    for c in components_of(comp):
        pv = [p for p in c.get("preview") or [] if isinstance(p, dict) and p.get("html")]
        cells = "".join(f'<figure><figcaption>{e(str(p.get("label")))}</figcaption><div class="stage">{p["html"]}</div></figure>' for p in pv)
        lc = f'<span class="lc">{e(str(c["lifecycle"]))}</span>' if c.get("lifecycle") else ""
        parts.append(f'<section class="comp" id="c-{e(_k(c.get("id")))}"><h3>{e(str(c.get("ja") or c.get("id") or ""))} <code>{e(str(c.get("id") or ""))}</code>{lc}</h3>'
                     f'<p>{e(str(c.get("use") or ""))}</p><div class="grid">{cells}</div></section>')
    title = f"{concept.get('name') or ''}".strip()
    version = gov.get("version") or ""
    return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>デザインシステムのカタログ</title>
<style>
:root {{
{root}
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; font-family: {font}; color: #10131a; background: #fff; }}
.wrap {{ max-width: 1120px; margin: 0 auto; padding: 32px 24px 64px; }}
header h1 {{ margin: 0 0 4px; font-size: 28px; }} header p {{ margin: 0; color: #4a5163; }}
h2 {{ margin: 40px 0 12px; font-size: 20px; border-bottom: 1px solid #dde1ea; padding-bottom: 6px; }}
h3 {{ margin: 0 0 4px; font-size: 18px; display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; }}
code {{ font-family: ui-monospace, monospace; font-size: 12px; color: #4a5163; }}
.meta {{ font-size: 12px; color: #4a5163; }}
.lc {{ font-size: 11px; border: 1px solid #7d8599; border-radius: 4px; padding: 0 4px; color: #4a5163; }}
.sws {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 12px; }}
.sw {{ display: flex; flex-direction: column; gap: 2px; font-size: 12px; }} .chip {{ height: 56px; border-radius: 8px; border: 1px solid #dde1ea; }}
.ty {{ display: grid; grid-template-columns: 110px 90px 1fr; gap: 12px; align-items: baseline; padding: 8px 0; border-bottom: 1px solid #f1f3f8; }}
.sps {{ display: flex; flex-direction: column; gap: 8px; }} .sp {{ display: grid; grid-template-columns: 110px 60px 1fr; gap: 12px; align-items: center; }} .sp i {{ display: block; height: 12px; background: #7d8599; }}
.rds {{ display: flex; gap: 24px; flex-wrap: wrap; }} .rd {{ display: flex; flex-direction: column; gap: 4px; align-items: flex-start; }} .rd i {{ display: block; width: 96px; height: 56px; background: #f1f3f8; border: 2px solid #7d8599; }}
.comp {{ padding: 20px 0; border-bottom: 1px solid #dde1ea; }} .comp > p {{ margin: 0 0 12px; color: #4a5163; font-size: 14px; }}
.grid {{ display: flex; flex-wrap: wrap; gap: 16px; align-items: flex-start; }}
figure {{ margin: 0; display: flex; flex-direction: column; gap: 6px; max-width: 100%; }}
figcaption {{ font-size: 12px; font-weight: 700; color: #4a5163; }}
.stage {{ padding: 16px; border: 1px dashed #dde1ea; border-radius: 8px; font-family: {font}; }}
{type_css}
</style>
<style>
{comp.get("preview_css") or ""}
</style></head>
<body><div class="wrap">
<header><h1>デザインシステムのカタログ{f"（{e(title)}）" if title else ""}</h1><p>specs/design/ の正本から組み立てた見本{f"・版 {e(str(version))}" if version else ""}。値が食い違えば正本が勝つ。</p></header>
<h2>色</h2><div class="sws">{sw}</div>
<h2>書体</h2><div>{ty}</div>
<h2>余白</h2><div class="sps">{space}</div>
<h2>角丸</h2><div class="rds">{radius}</div>
<h2>部品</h2>
{"".join(parts)}
</div></body></html>
"""


def catalog_height(files: dict[str, str]) -> int:
    """撮るときの高さの見込み（Chromium の --screenshot は窓の大きさで撮るため）"""
    comps = components_of(_load(files.get("components.yaml")))
    tokens = _load(files.get("tokens.yaml"))
    colors = sum(1 for _, _, k in css_vars(tokens) if k == "color")
    h = 900 + (colors // 6 + 1) * 90 + len(type_styles(tokens)) * 60 + sum(220 + 120 * ((len(c.get("preview") or []) - 1) // 4) for c in comps)
    return max(1200, min(h, 16000))


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.split("使い方:")[1].split("依存")[0].strip())
        return 2
    d = Path(argv[0])
    files = {p.name: p.read_text(encoding="utf-8") for p in sorted(d.glob("*.yaml"))}
    errs = validate_previews(_load(files.get("components.yaml")))
    if errs:
        print("\n".join(errs))
        return 1
    Path(argv[1]).write_text(build(files), encoding="utf-8")
    print(f"ok（撮るときの高さの見込み {catalog_height(files)}px）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
