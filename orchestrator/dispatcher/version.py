"""工場の版の一覧（ADR 0031 の決定 4・9）。サービスごとに、今動いているコンテナがどの版（Engine のコミット）のイメージかを出す。

版はイメージのラベル `factory.engine_version` に入っている（scripts/build.sh が作るときに付ける）。コンテナの Config.Labels には
イメージのラベルが引き継がれるので、docker inspect だけで読める。DB は使わない（入れ替え役が上げ直しの確かめにも使う）。
"""
from __future__ import annotations

import json
import subprocess

LABEL = "factory.engine_version"
PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"
JOB_LABEL = "factory.job"
UNKNOWN = "unknown"
OWN_IMAGE_PREFIX = "factory-"   # 工場が作るイメージ（factory-dispatcher・factory-shipper・factory-worker）


def _docker(args: list[str]) -> str:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=30, check=True).stdout


def rows_from_inspect(items: list[dict]) -> list[dict]:
    """docker inspect（コンテナ）の結果から、1 コンテナ 1 行の一覧を作る。並びは compose のサービス、ジョブ、その他の順"""
    rows = []
    for it in items:
        cfg = it.get("Config") or {}
        labels = cfg.get("Labels") or {}
        state = (it.get("State") or {}).get("Status", "")
        if labels.get(SERVICE_LABEL):
            kind, name = "service", labels[SERVICE_LABEL]
        elif labels.get(JOB_LABEL):
            kind, name = "job", (it.get("Name") or "").lstrip("/")
        else:
            continue
        image = cfg.get("Image", "")
        external = kind == "service" and not image.startswith(OWN_IMAGE_PREFIX)   # postgres・オブジェクトストアなど、工場が作らないイメージ
        health = ((it.get("State") or {}).get("Health") or {}).get("Status")   # ヘルスチェックのないコンテナは None
        rows.append({"kind": kind, "name": name, "state": state, "image": image, "image_id": it.get("Image", ""), "health": health, "external": external,
                     "version": "-" if external else (labels.get(LABEL) or UNKNOWN)})
    order = {"service": 0, "job": 1}
    return sorted(rows, key=lambda r: (order[r["kind"]], r["name"]))


def list_running(project: str = "factory") -> list[dict]:
    ids = _docker(["ps", "-a", "-q", "--filter", f"label={PROJECT_LABEL}={project}"]).split()
    ids += _docker(["ps", "-a", "-q", "--filter", f"label={JOB_LABEL}"]).split()
    if not ids:
        return []
    return rows_from_inspect(json.loads(_docker(["inspect", *dict.fromkeys(ids)])))


def mismatched(rows: list[dict], expect: str | None = None) -> list[dict]:
    """版のずれたサービス。expect（コミット）を渡せばそれと、渡さなければいちばん多い版と比べる。
    ジョブ（ワーカー）は前の版のイメージのまま終わってよい（ADR 0020）ので数えない。工場が作らないイメージ（postgres など）は版を見ず、
    止まっていれば数える。止まっているサービスも数える（上げ忘れ）"""
    services = [r for r in rows if r["kind"] == "service"]
    if not services:
        return []
    stopped = [r for r in services if r["state"] != "running"]
    services = [r for r in services if not r["external"]]
    if not services:
        return stopped
    if expect is None:
        counts: dict[str, int] = {}
        for r in services:
            counts[r["version"]] = counts.get(r["version"], 0) + 1
        expect = max(counts, key=lambda v: (counts[v], v != UNKNOWN))
    bad = [r for r in services if not _same(r["version"], expect)]
    return bad + [r for r in stopped if r not in bad]


def _same(a: str, b: str) -> bool:
    """短いコミットと長いコミットを同じとみなす（7 文字以上の前方一致）"""
    if a == UNKNOWN or b == UNKNOWN:
        return False
    n = min(len(a), len(b))
    return n >= 7 and a[:n] == b[:n]


def report(rows: list[dict], expect: str | None = None) -> tuple[str, int]:
    """表示する文字と終了コード（ずれがあれば 1）"""
    bad = {id(r) for r in mismatched(rows, expect)}
    lines = [f"{'':2}{'種類':<8}{'名前':<28}{'状態':<10}{'版':<12}イメージ"]
    for r in rows:
        mark = "! " if id(r) in bad else "  "
        lines.append(f"{mark}{r['kind']:<8}{r['name']:<28}{r['state']:<10}{r['version']:<12}{r['image']}")
    if bad:
        target = f"（期待 {expect}）" if expect else ""
        lines.append(f"版のずれ・止まっているサービスが {len(bad)} 件{target}: " + ", ".join(r["name"] for r in rows if id(r) in bad))
    return "\n".join(lines), (1 if bad else 0)
