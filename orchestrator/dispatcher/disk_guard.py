"""Docker のディスクの見張り（2026-11-14）。ビルドのキャッシュがたまって Mac のディスクが満杯になり、工場全体が止まったことが 2 回あった。
どちらも人が `docker builder prune` で直した。直し方の決まっている資源の詰まりなので、工場が自分で片付ける（人の手は承認と審美眼に絞る）。

dispatcher の周回（swap_loop）から DISK_GUARD_INTERVAL_SEC ごとに呼ぶ。
- ビルドのキャッシュ（docker system df の Build Cache）が DISK_BUILD_CACHE_MAX_GB を超えた、またはコンテナから見た空きが DISK_MIN_FREE_GB を
  下回ったら、ビルドのキャッシュを DISK_BUILD_CACHE_KEEP_GB まで減らす（`docker builder prune -f --keep-storage`。最近使ったものは残る）
- それでも空きが足りなければ、名前のないイメージ（dangling）も消す。名前のあるイメージ（前の版に戻すためのものを含む）・コンテナ・ボリュームは消さない
- 片付けたら ops.disk_pruned に残す。片付けても空きが足りなければ、停止 disk:low（env）を開いてお知らせにする。空きが戻ったら閉じる
DISK_GUARD=0 で止める。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

from psycopg.types.json import Jsonb

ENV = os.environ
ENABLED = ENV.get("DISK_GUARD", "1") != "0"
INTERVAL_SEC = int(ENV.get("DISK_GUARD_INTERVAL_SEC", "300"))
CACHE_MAX_GB = float(ENV.get("DISK_BUILD_CACHE_MAX_GB", "20"))
CACHE_KEEP_GB = float(ENV.get("DISK_BUILD_CACHE_KEEP_GB", "8"))
MIN_FREE_GB = float(ENV.get("DISK_MIN_FREE_GB", "15"))
STOPPAGE_KEY = "disk:low"
GB = 1024 ** 3
UNITS = {"b": 1, "kb": 1000, "mb": 1000 ** 2, "gb": 1000 ** 3, "tb": 1000 ** 4}


def _docker(args: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def size_bytes(s: str) -> float:
    """docker の大きさの表記（"12.3GB"・"512MB"・"0B"）をバイトに。読めなければ 0"""
    m = re.match(r"^\s*([\d.]+)\s*([kKmMgGtT]?[bB])\s*$", s or "")
    return float(m.group(1)) * UNITS[m.group(2).lower()] if m else 0.0


def build_cache_bytes(docker=_docker) -> float:
    r = docker(["system", "df", "--format", "{{json .}}"], timeout=120)
    if r.returncode != 0:
        return 0.0
    for line in (r.stdout or "").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("Type") == "Build Cache":
            return size_bytes(row.get("Size") or "")
    return 0.0


def free_bytes(path: str = "/") -> float:
    return float(shutil.disk_usage(path).free)


def step(cur, log=print, *, docker=_docker, free=free_bytes) -> dict | None:
    """見て、要れば片付ける。片付けたら記録の中身を返す"""
    if not ENABLED:
        return None
    import stoppage
    cache, avail = build_cache_bytes(docker), free()
    low = avail < MIN_FREE_GB * GB
    if cache <= CACHE_MAX_GB * GB and not low:
        stoppage.close_stoppage(cur, STOPPAGE_KEY, remedy="ディスクの空きが戻った", closed_by="dispatcher")
        return None
    actions = []
    r = docker(["builder", "prune", "-f", "--keep-storage", f"{int(CACHE_KEEP_GB * 1000)}MB"])
    actions.append({"cmd": "builder prune", "ok": r.returncode == 0, "out": (r.stdout or r.stderr or "").strip()[-200:]})
    if free() < MIN_FREE_GB * GB:
        r = docker(["image", "prune", "-f"])
        actions.append({"cmd": "image prune (dangling)", "ok": r.returncode == 0, "out": (r.stdout or r.stderr or "").strip()[-200:]})
    after = free()
    rec = {"cache_gb": round(cache / GB, 1), "free_gb_before": round(avail / GB, 1), "free_gb_after": round(after / GB, 1),
           "cache_max_gb": CACHE_MAX_GB, "min_free_gb": MIN_FREE_GB, "actions": actions}
    cur.execute("insert into events(name, payload) values ('ops.disk_pruned', %s)", (Jsonb(rec),))
    log("disk pruned", rec["cache_gb"], "GB cache,", rec["free_gb_before"], "->", rec["free_gb_after"], "GB free")
    if after < MIN_FREE_GB * GB:
        stoppage.open_stoppage(cur, STOPPAGE_KEY, "env", f"Docker のディスクの空きが {rec['free_gb_after']}GB（下限 {MIN_FREE_GB:g}GB）。"
                               "ビルドのキャッシュと名前のないイメージを片付けても足りない",
                               waiting_for="Mac で docker system df を見て、要らないイメージ・ボリュームを消すか、Docker Desktop のディスクを広げる",
                               detected_by="dispatcher")
    else:
        stoppage.close_stoppage(cur, STOPPAGE_KEY, remedy="ビルドのキャッシュを片付けて空きが戻った", closed_by="dispatcher")
    return rec
