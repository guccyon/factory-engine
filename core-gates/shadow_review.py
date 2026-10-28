#!/usr/bin/env python3
"""rubric.yaml に基づく shadow 判定（P0 Week 4）。

PR の差分を判定器（tier-judge のモデル）に渡し、rubric の各項目に照らした Defect の配列を返させる。
shadow なので GitHub には何も書かない。結果は JSON ファイルに出し、--db を付ければ制御プレーンの
events に gate.evaluated（mode=shadow）として記録する。差し戻しの根拠には使わない（P0 は記録のみ）。

使い方:
  core-gates/shadow_review.py --pr https://github.com/guccyon/rally/pull/4 --out shadow-4.json [--db docker:factory-postgres-1]

依存: python3（標準ライブラリ。--db のときだけ psql コマンド）、gh、agent_runner（工程 inspect。runner とモデルは definitions/agents.yaml、--tier で上書き）
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import agent_runner   # 同じ core-gates/ にある。工程からエージェントを呼ぶ唯一の入口（ADR 0010）

HERE = Path(__file__).resolve().parent

SCHEMA = {
    "type": "object",
    "properties": {
        "defects": {"type": "array", "items": {"type": "object", "properties": {
            "rubric_id": {"type": "string"}, "gate": {"type": "string"}, "severity": {"type": "string", "enum": ["must", "should", "nits"]},
            "location": {"type": "string"}, "evidence": {"type": "string"}, "remediation_hint": {"type": "string"}},
            "required": ["rubric_id", "gate", "severity", "location", "evidence", "remediation_hint"]}},
        "checked_rubric_ids": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": ["defects", "checked_rubric_ids", "summary"],
}


def sh(*a: str, input: str | None = None, retries: int = 3) -> str:
    """gh は一時的に rc=1 で落ちることがある（レート制限とみられる）ので 20・40・80 秒待って繰り返す"""
    import time
    last = None
    for i in range(retries + 1):
        r = subprocess.run(a, capture_output=True, text=True, input=input)
        if r.returncode == 0:
            return r.stdout
        last = r
        if i < retries:
            time.sleep(20 * (2 ** i))
    raise RuntimeError(f"{a[0]} failed (rc={last.returncode}): stderr={last.stderr[-300:]} stdout={last.stdout[-300:]}")


def load_rubric() -> list[dict]:
    """rubric.yaml を読む。PyYAML が無い環境でも動くよう ruby か python の yaml を試す。"""
    path = HERE / "rubric.yaml"
    try:
        import yaml  # type: ignore
        return yaml.safe_load(path.read_text(encoding="utf-8"))["rules"]
    except ImportError:
        out = sh("ruby", "-ryaml", "-rjson", "-e", 'puts JSON.generate(YAML.load_file(ARGV[0])["rules"])', str(path))
        return json.loads(out)


def pr_parts(url: str) -> tuple[str, int]:
    m = re.match(r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)", url)
    if not m:
        raise SystemExit(f"PR の URL ではない: {url}")
    return m.group(1), int(m.group(2))


def review(pr_url: str, tier: str = "tier-judge", max_diff_chars: int = 60000) -> dict:
    repo, number = pr_parts(pr_url)
    meta = json.loads(sh("gh", "pr", "view", str(number), "-R", repo, "--json", "title,body,files"))
    diff = sh("gh", "pr", "diff", str(number), "-R", repo)
    if len(diff) > max_diff_chars:
        diff = diff[:max_diff_chars] + f"\n\n[... 差分が長いので {max_diff_chars} 文字で切った]"
    body = meta.get("body") or ""
    # 本文は丸ごと渡す（3000 字で切っていたため、検査官が「本文が途中で途切れている」と誤検出した。T08 / T10）。
    # 極端に長いときだけ切り、切ったことを明示する
    body_text = body if len(body) <= 20000 else body[:20000] + "\n\n[... 本文が長いので検査器が 20000 字で切った。途切れは PR の欠陥ではない]"
    rules = load_rubric()
    rubric_text = "\n".join(f"- {r['id']} [{r['gate']}/{r['severity']}] {r['title']}: {r.get('check', '')}" for r in rules)
    prompt = f"""あなたは工場の検査官（inspector）です。build-cell とは別の文脈で、PR の差分をルーブリックに照らして検査します。
ルーブリックの各項目について、この差分に該当する欠陥があれば Defect として挙げてください。該当しない項目は挙げません。
根拠（evidence）は差分の中の具体的な行や箇所を引いてください。推測で欠陥を作らないこと。差分の外のファイルを見ることはできないので、
「見えないので判定できない」項目は checked_rubric_ids に含めないでください。

# ルーブリック
{rubric_text}

# PR: {meta['title']}
{body_text}

# 差分
{diff}
"""
    r = agent_runner.run("inspect", prompt, schema=SCHEMA, tier=tier)
    return {
        "pr": pr_url, "repo": repo, "number": number, "mode": "shadow", "tier": tier, "model": r.model,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "usage": r.usage, "cost_usd": r.cost_usd,
        "result": r.output,
    }


def record_db(dsn: str, r: dict):
    payload = json.dumps({"pr_url": r["pr"], "mode": "shadow", "gate": "rubric", "model": r["model"],
                          "defects": r["result"]["defects"], "checked": r["result"]["checked_rubric_ids"],
                          "cost_usd": r["cost_usd"]}, ensure_ascii=False).replace("'", "''")
    corr = f"github:{r['repo']}#pr{r['number']}"
    sql = f"insert into events(name, correlation_id, payload, dedupe_key) values ('gate.evaluated', '{corr}', '{payload}'::jsonb, 'shadow:{r['pr']}:{r['evaluated_at'][:16]}') on conflict do nothing;"
    import shutil
    if shutil.which("psql"):
        sh("psql", dsn, "-c", sql)
    else:   # builder の Mac に psql がないときは Compose の postgres コンテナで実行する（dsn は "docker:<container>" でもよい）
        container = dsn.split(":", 1)[1] if dsn.startswith("docker:") else "factory-postgres-1"
        sh("docker", "exec", "-i", container, "psql", "-U", "factory", "-d", "factory", "-c", sql)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pr", required=True, action="append", help="PR の URL（複数可）")
    ap.add_argument("--out", required=True)
    ap.add_argument("--tier", default="tier-judge")
    ap.add_argument("--db", default=None)
    a = ap.parse_args()
    results = []
    for url in a.pr:
        r = review(url, a.tier)
        results.append(r)
        print(f"{url}: defects={len(r['result']['defects'])} checked={len(r['result']['checked_rubric_ids'])} cost=${r['cost_usd']}", file=sys.stderr)
        Path(a.out).write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        if a.db:
            record_db(a.db, r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
