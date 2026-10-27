#!/usr/bin/env python3
"""L1 の契約互換性チェッカー（gates.yaml の L1「契約互換性チェッカー」、P1）。

PR の base と head の契約（OpenAPI 3.1 と AsyncAPI）を比べ、支えている版のクライアント・購読先を壊す変更を 0 / 1 で判定する。
決定的（LLM を使わない）。製品の manifest の compat.api（deprecation_period_days）に従う。

壊す変更（violation）:
  - 操作（path × method）・チャンネル・メッセージの削除
  - リクエスト: 必須の項目・パラメータの追加、任意だったものを必須に、項目の型の変更、enum の値の削除
  - レスポンス・イベントの本文: 項目の削除、必須だったものを任意に、型の変更
  ただし base で `deprecated: true` と `x-deprecated-at: YYYY-MM-DD` が付いていて、その日から deprecation_period_days 以上たったものの削除は通す
注意（warning、判定は落とさない）:
  - レスポンス・イベントの enum に値を足した（古いクライアントが知らない値を受け取る）
  - `deprecated: true` を付けたのに `x-deprecated-at` がない（90 日を確かめられない）

使い方:
  core-gates/compat_check.py --base origin/main [--repo .] [--today YYYY-MM-DD] [--json out.json] [--summary summary.md]
  終了コード: 0 = 違反なし、1 = 違反あり、2 = 読めなかった

依存: python3、git。YAML は PyYAML があればそれ、なければ ruby で読む
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

CONTRACT_GLOBS = ["specs/contracts/api/*/openapi.yaml", "specs/contracts/events/asyncapi.yaml"]
METHODS = ("get", "put", "post", "delete", "patch", "head", "options")


def parse_yaml(text: str) -> dict:
    try:
        import yaml  # type: ignore
        return yaml.safe_load(text) or {}
    except ImportError:
        r = subprocess.run(["ruby", "-ryaml", "-rjson", "-e", "puts JSON.generate(YAML.safe_load(STDIN.read, aliases: true))"],
                           input=text, capture_output=True, text=True, check=True)
        return json.loads(r.stdout) or {}


def git_show(repo: str, ref: str, path: str) -> str | None:
    r = subprocess.run(["git", "-C", repo, "show", f"{ref}:{path}"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


class Doc:
    """$ref（同じ文書の中の #/...）をたどれる契約の文書"""

    def __init__(self, d: dict):
        self.d = d

    def resolve(self, node, seen=()):
        while isinstance(node, dict) and "$ref" in node:
            ref = node["$ref"]
            if not ref.startswith("#/") or ref in seen:
                return node
            seen = seen + (ref,)
            cur = self.d
            for part in ref[2:].split("/"):
                cur = (cur or {}).get(part.replace("~1", "/").replace("~0", "~"))
            node = cur
        return node


def types_of(s: dict) -> set:
    t = s.get("type")
    if t is None:
        return set()
    return set(t) if isinstance(t, list) else {t}


class Checker:
    def __init__(self, today: date, period_days: int):
        self.today, self.period = today, period_days
        self.violations: list[dict] = []
        self.warnings: list[dict] = []

    def v(self, where, rule, msg):
        self.violations.append({"where": where, "rule": rule, "message": msg})

    def w(self, where, rule, msg):
        self.warnings.append({"where": where, "rule": rule, "message": msg})

    def removal_allowed(self, node: dict, where: str) -> bool:
        """base で deprecated かつ x-deprecated-at から期間が過ぎていれば削除してよい"""
        if not isinstance(node, dict) or not node.get("deprecated"):
            return False
        at = node.get("x-deprecated-at")
        try:
            d = date.fromisoformat(str(at))
        except ValueError:
            return False
        return (self.today - d).days >= self.period

    # --- スキーマの比較。direction は "request"（クライアントが送る）か "response"（クライアント・購読先が読む） ---
    def schema(self, bdoc: Doc, hdoc: Doc, b, h, where: str, direction: str, depth: int = 0):
        b, h = bdoc.resolve(b), hdoc.resolve(h)
        if not isinstance(b, dict) or not isinstance(h, dict) or depth > 12:
            return
        bt, ht = types_of(b), types_of(h)
        if bt and ht and bt != ht:
            # null を足す・外すだけの変更は、読む側（response）で null を足すときだけ壊す
            if direction == "request" and bt - {"null"} == ht - {"null"} and bt <= ht:
                pass
            elif direction == "response" and bt - {"null"} == ht - {"null"} and ht <= bt:
                pass
            else:
                self.v(where, "type_changed", f"型が {sorted(bt)} から {sorted(ht)} に変わった")
        be, he = b.get("enum"), h.get("enum")
        if isinstance(be, list) and isinstance(he, list):
            removed, added = [x for x in be if x not in he], [x for x in he if x not in be]
            if direction == "request" and removed:
                self.v(where, "enum_value_removed", f"enum の値 {removed} を消した（古いクライアントが送る）")
            if direction == "response" and added:
                self.w(where, "enum_value_added", f"enum に {added} を足した（古いクライアントが知らない値を受け取る）")
            if direction == "response" and removed:
                pass   # 返す値が減るのは読む側を壊さない
        bp, hp = b.get("properties") or {}, h.get("properties") or {}
        breq, hreq = set(b.get("required") or []), set(h.get("required") or [])
        for name, bs in bp.items():
            sub = f"{where}.{name}"
            if name not in hp:
                if direction == "response" and not self.removal_allowed(bdoc.resolve(bs) or {}, sub):
                    self.v(sub, "property_removed", "項目を消した（読む側が壊れる）。deprecated と x-deprecated-at を付けて期間を置く")
                continue
            self.schema(bdoc, hdoc, bs, hp[name], sub, direction, depth + 1)
            self.deprecation_marked(bdoc.resolve(bs), hdoc.resolve(hp[name]), sub)
        if direction == "request":
            for name in sorted(hreq - breq):
                self.v(f"{where}.{name}", "required_added", "リクエストに必須の項目を足した（古いクライアントは送らない）")
        else:
            for name in sorted(breq - hreq):
                if name in hp:
                    self.v(f"{where}.{name}", "required_removed", "レスポンスの必須の項目を任意にした（読む側は必ずあると思っている）")
        for key in ("items", "additionalProperties"):
            if isinstance(b.get(key), dict) and isinstance(h.get(key), dict):
                self.schema(bdoc, hdoc, b[key], h[key], f"{where}[]" if key == "items" else f"{where}{{}}", direction, depth + 1)
        for key in ("allOf", "oneOf", "anyOf"):
            bl, hl = b.get(key) or [], h.get(key) or []
            for i, (x, y) in enumerate(zip(bl, hl)):
                self.schema(bdoc, hdoc, x, y, f"{where}.{key}[{i}]", direction, depth + 1)

    def deprecation_marked(self, b, h, where):
        if isinstance(h, dict) and h.get("deprecated") and not (isinstance(b, dict) and b.get("deprecated")) and not h.get("x-deprecated-at"):
            self.w(where, "deprecated_without_date", "deprecated を付けたが x-deprecated-at がない（廃止までの期間を確かめられない）")

    # --- OpenAPI ---
    def openapi(self, path: str, bd: dict, hd: dict):
        bdoc, hdoc = Doc(bd), Doc(hd)
        bpaths, hpaths = bd.get("paths") or {}, hd.get("paths") or {}
        for p, bitem in bpaths.items():
            hitem = hpaths.get(p) or {}
            for m in METHODS:
                if m not in (bitem or {}):
                    continue
                bop, where = bitem[m], f"{path} {m.upper()} {p}"
                if m not in hitem:
                    if not self.removal_allowed(bop, where):
                        self.v(where, "operation_removed", "操作を消した。deprecated と x-deprecated-at を付けて期間を置く")
                    continue
                self.operation(bdoc, hdoc, bop, hitem[m], where, (bitem or {}).get("parameters") or [], (hitem or {}).get("parameters") or [])

    def operation(self, bdoc, hdoc, bop, hop, where, bpar_common, hpar_common):
        self.deprecation_marked(bop, hop, where)

        def params(doc, op, common):
            out = {}
            for prm in list(common) + list(op.get("parameters") or []):
                prm = doc.resolve(prm)
                if isinstance(prm, dict) and "name" in prm:
                    out[(prm.get("in"), prm["name"])] = prm
            return out
        bp, hp = params(bdoc, bop, bpar_common), params(hdoc, hop, hpar_common)
        for key, h in hp.items():
            b = bp.get(key)
            loc = f"{where} param {key[0]}:{key[1]}"
            if b is None:
                if h.get("required"):
                    self.v(loc, "required_param_added", "必須のパラメータを足した（古いクライアントは送らない）")
                continue
            if h.get("required") and not b.get("required"):
                self.v(loc, "param_became_required", "任意のパラメータを必須にした")
            self.schema(bdoc, hdoc, b.get("schema") or {}, h.get("schema") or {}, loc, "request")
        for key in bp:
            if key not in hp and key[0] == "path":
                self.v(f"{where} param path:{key[1]}", "path_param_removed", "パスのパラメータを消した")
        brb, hrb = bdoc.resolve(bop.get("requestBody")) or {}, hdoc.resolve(hop.get("requestBody")) or {}
        if hrb.get("required") and not brb and hop.get("requestBody"):
            self.v(f"{where} requestBody", "request_body_required_added", "必須のリクエスト本文を足した")
        for ct, bmt in (brb.get("content") or {}).items():
            hmt = (hrb.get("content") or {}).get(ct)
            if hmt is None:
                self.v(f"{where} requestBody {ct}", "request_media_removed", "受け付けていたメディア型をやめた")
                continue
            self.schema(bdoc, hdoc, bmt.get("schema") or {}, hmt.get("schema") or {}, f"{where} request", "request")
        bres, hres = bop.get("responses") or {}, hop.get("responses") or {}
        for code, br in bres.items():
            if not str(code).startswith("2"):
                continue
            hr = hres.get(code)
            loc = f"{where} response {code}"
            if hr is None:
                self.v(loc, "success_response_removed", "成功のレスポンスの区分を消した")
                continue
            br, hr = bdoc.resolve(br) or {}, hdoc.resolve(hr) or {}
            for ct, bmt in (br.get("content") or {}).items():
                hmt = (hr.get("content") or {}).get(ct)
                if hmt is None:
                    self.v(f"{loc} {ct}", "response_media_removed", "返していたメディア型をやめた")
                    continue
                self.schema(bdoc, hdoc, bmt.get("schema") or {}, hmt.get("schema") or {}, loc, "response")

    # --- AsyncAPI（購読先は読む側なので response と同じ向き） ---
    def asyncapi(self, path: str, bd: dict, hd: dict):
        bdoc, hdoc = Doc(bd), Doc(hd)
        bch, hch = bd.get("channels") or {}, hd.get("channels") or {}
        for name, bc in bch.items():
            where = f"{path} channel {name}"
            if name not in hch:
                if not self.removal_allowed(bc, where):
                    self.v(where, "channel_removed", "チャンネルを消した（購読先が壊れる）")
                continue
            for mname in ((bc or {}).get("messages") or {}):
                if mname not in ((hch[name] or {}).get("messages") or {}):
                    self.v(f"{where} message {mname}", "message_removed", "チャンネルのメッセージを消した")
        bm, hm = (bd.get("components") or {}).get("messages") or {}, (hd.get("components") or {}).get("messages") or {}
        for name, bmsg in bm.items():
            where = f"{path} message {name}"
            if name not in hm:
                if not self.removal_allowed(bmsg, where):
                    self.v(where, "message_removed", "メッセージを消した（購読先が壊れる）")
                continue
            b, h = bdoc.resolve(bmsg) or {}, hdoc.resolve(hm[name]) or {}
            self.schema(bdoc, hdoc, b.get("payload") or {}, h.get("payload") or {}, f"{where} payload", "response")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="比べる元の ref（PR の base。例: origin/main）")
    ap.add_argument("--head", help="比べる先の ref。省くと作業ツリー（CI ではチェックアウトした PR の head）")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--today", default=date.today().isoformat())
    ap.add_argument("--json")
    ap.add_argument("--summary", help="Markdown の要約（CI の Job Summary に足す）")
    a = ap.parse_args()
    period = 90
    manifest = Path(a.repo) / ".factory-config/manifest.yaml"
    if manifest.exists():
        try:
            period = int(((parse_yaml(manifest.read_text(encoding="utf-8")).get("compat") or {}).get("api") or {}).get("deprecation_period_days", 90))
        except Exception:  # noqa: BLE001  manifest が読めなくても既定の 90 日で続ける
            pass
    ck = Checker(date.fromisoformat(a.today), period)
    pats = [re.compile("^" + re.escape(g).replace(r"\*", "[^/]+") + "$") for g in CONTRACT_GLOBS]

    def files_at(ref):
        r = subprocess.run(["git", "-C", a.repo, "ls-tree", "-r", "--name-only", ref, "specs/contracts"], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"{ref} を読めない: {r.stderr.strip()}")
        return {f for f in r.stdout.split() if any(p.match(f) for p in pats)}
    try:
        base_files = files_at(a.base)
        head_files = files_at(a.head) if a.head else {str(Path(p).relative_to(a.repo)) for g in CONTRACT_GLOBS for p in glob.glob(str(Path(a.repo) / g))}
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 2
    checked = []
    for f in sorted(base_files | head_files):
        bt = git_show(a.repo, a.base, f) if f in base_files else None
        if bt is None:
            continue   # 新しい契約は壊すものがない
        if f not in head_files:
            ck.v(f, "contract_removed", "契約の文書ごと消した")
            continue
        ht = git_show(a.repo, a.head, f) if a.head else (Path(a.repo) / f).read_text(encoding="utf-8")
        try:
            bd, hd = parse_yaml(bt), parse_yaml(ht)
        except Exception as e:  # noqa: BLE001
            print(f"{f}: YAML を読めない: {e}", file=sys.stderr)
            return 2
        checked.append(f)
        if "openapi" in hd or "openapi" in bd:
            ck.openapi(f, bd, hd)
        elif "asyncapi" in hd or "asyncapi" in bd:
            ck.asyncapi(f, bd, hd)
    report = {"gate": "L1", "check": "contract_compat", "base": a.base, "deprecation_period_days": period, "checked": checked,
              "violations": ck.violations, "warnings": ck.warnings, "pass": not ck.violations}
    if a.json:
        Path(a.json).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    lines = ["## L1 契約の互換性", "", f"- 比べた契約: {len(checked)} 件（base: `{a.base}`、廃止までの期間 {period} 日）",
             f"- 違反 {len(ck.violations)} 件、注意 {len(ck.warnings)} 件"]
    for title, items in (("違反", ck.violations), ("注意", ck.warnings)):
        if items:
            lines += ["", f"### {title}", "", "| 箇所 | 規則 | 内容 |", "|---|---|---|"]
            lines += [f"| `{i['where']}` | {i['rule']} | {i['message']} |" for i in items]
    text = "\n".join(lines) + "\n"
    if a.summary:
        with open(a.summary, "a", encoding="utf-8") as fh:
            fh.write(text)
    print(text)
    return 0 if not ck.violations else 1


if __name__ == "__main__":
    sys.exit(main())
