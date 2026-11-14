"""Docker のディスクの見張り（2026-11-14）。ビルドのキャッシュがたまって Mac のディスクが満杯になり、工場全体が止まったことが 2 回あった。
どちらも人が `docker builder prune` で直した。直し方の決まっている資源の詰まりなので、工場が自分で片付ける（人の手は承認と審美眼に絞る）。

dispatcher の周回（swap_loop）から DISK_GUARD_INTERVAL_SEC ごとに呼ぶ。
- ビルドのキャッシュ（docker system df の Build Cache）が DISK_BUILD_CACHE_MAX_GB を超えた、または空きが DISK_MIN_FREE_GB を下回ったら。
  空きは、Docker の仮想ディスクの中（コンテナの /）と、ホスト（Mac 本体）のディスク（ホストから bind mount したパス DISK_HOST_PATH、既定 /definitions）の
  少ない方で見る。11/14 の 2 回目の満杯は Mac 本体の側で、仮想ディスクの中は 356GB 空いていた。Docker Desktop は仮想ディスクで消した分を Mac に返す、ビルドのキャッシュを DISK_BUILD_CACHE_KEEP_GB まで減らす（`docker builder prune -f --keep-storage`。最近使ったものは残る）
- 製品の古い版のイメージ（<repo>-<part>:<x.y.z>）は、名前ごとに新しい DISK_KEEP_RELEASES 版（既定 3。前の版に戻せる）と、コンテナが使っているものを残して外す。
  手で付けた控えのタグ（*-before-*）は、DISK_BEFORE_TAG_DAYS 日（既定 7）より古く、名前ごとに新しい 2 つに入らないものを外す（11/14 夜: rally の 0.4.0〜0.32.0 と
  9〜13 日前の控えで回収できる 21GB があった）
- それでも空きが足りなければ、名前のないイメージ（dangling）も消す。工場の版のタグ（<name>-<sha>。更新役が管理）・コンテナ・ボリュームは消さない
- 片付けたら ops.disk_pruned に残す。片付けても空きが足りなければ、停止 disk:low（env）を開いてお知らせにする。空きが戻ったら閉じる
DISK_GUARD=0 で止める。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone

from psycopg.types.json import Jsonb

ENV = os.environ
ENABLED = ENV.get("DISK_GUARD", "1") != "0"
INTERVAL_SEC = int(ENV.get("DISK_GUARD_INTERVAL_SEC", "300"))
CACHE_MAX_GB = float(ENV.get("DISK_BUILD_CACHE_MAX_GB", "20"))
CACHE_KEEP_GB = float(ENV.get("DISK_BUILD_CACHE_KEEP_GB", "8"))
MIN_FREE_GB = float(ENV.get("DISK_MIN_FREE_GB", "15"))
HOST_PATH = ENV.get("DISK_HOST_PATH", "/definitions")   # ホストから bind mount したパス（statvfs がホストのディスクを返す）
KEEP_RELEASES = int(ENV.get("DISK_KEEP_RELEASES", "3"))
BEFORE_TAG_DAYS = int(ENV.get("DISK_BEFORE_TAG_DAYS", "7"))
STOPPAGE_KEY = "disk:low"
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
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


def free_bytes() -> float:
    """仮想ディスクの中とホストのディスクの、少ない方の空き"""
    vals = [float(shutil.disk_usage("/").free)]
    if HOST_PATH and os.path.isdir(HOST_PATH):
        try:
            vals.append(float(shutil.disk_usage(HOST_PATH).free))
        except OSError:
            pass
    return min(vals)


def stale_images(rows: list[dict], in_use: set[str], *, keep_releases: int = KEEP_RELEASES, before_days: int = BEFORE_TAG_DAYS,
                 now: datetime | None = None) -> list[str]:
    """外してよいイメージの名前（repo:tag）。rows は docker image ls の json（Repository・Tag・ID・CreatedAt）"""
    now = now or datetime.now(timezone.utc)
    out, releases, befores = [], {}, {}
    for r in rows:
        repo, tag = r.get("Repository") or "", r.get("Tag") or ""
        if not repo or repo == "<none>" or not tag or tag == "<none>":
            continue
        m = SEMVER.match(tag)
        if m:
            releases.setdefault(repo, []).append((tuple(int(x) for x in m.groups()), r))
        elif "-before-" in tag:
            befores.setdefault(repo, []).append((_created(r.get("CreatedAt") or ""), r))
    used = lambda r: f"{r['Repository']}:{r['Tag']}" in in_use or (r.get("ID") or "-") in in_use   # noqa: E731
    for items in releases.values():
        items.sort(key=lambda x: x[0], reverse=True)
        out += [f"{r['Repository']}:{r['Tag']}" for _, r in items[keep_releases:] if not used(r)]
    for items in befores.values():
        items.sort(key=lambda x: x[0] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        out += [f"{r['Repository']}:{r['Tag']}" for t, r in items[2:]
                if t and (now - t).days >= before_days and not used(r)]
    return out


def _created(s: str) -> datetime | None:
    """docker の CreatedAt（"2026-11-01 10:00:00 +0900 JST"）"""
    try:
        return datetime.strptime(" ".join(s.split()[:3]), "%Y-%m-%d %H:%M:%S %z")
    except ValueError:
        return None


def in_use_images(docker=_docker) -> set[str]:
    r = docker(["ps", "-a", "--format", "{{.Image}}"], timeout=120)
    names = set((r.stdout or "").split()) if r.returncode == 0 else set()
    r = docker(["ps", "-aq"], timeout=120)
    for cid in (r.stdout or "").split() if r.returncode == 0 else []:
        i = docker(["inspect", "-f", "{{.Image}}", cid], timeout=60)
        if i.returncode == 0:
            names.add(i.stdout.strip())
    return names


def remove_stale_images(docker=_docker) -> dict:
    r = docker(["image", "ls", "--format", "{{json .}}"], timeout=120)
    if r.returncode != 0:
        return {"cmd": "rmi stale", "ok": False, "out": (r.stderr or "")[-200:]}
    rows = []
    for line in (r.stdout or "").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    names = stale_images(rows, in_use_images(docker))
    removed = [n for n in names if docker(["rmi", n], timeout=300).returncode == 0]   # -f なし。使われていれば docker が断る
    return {"cmd": "rmi stale", "ok": True, "out": f"{len(removed)}/{len(names)}", "removed": removed[:50]}


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
    actions.append(remove_stale_images(docker))
    if free() < MIN_FREE_GB * GB:
        r = docker(["image", "prune", "-f"])
        actions.append({"cmd": "image prune (dangling)", "ok": r.returncode == 0, "out": (r.stdout or r.stderr or "").strip()[-200:]})
    after = free()
    rec = {"cache_gb": round(cache / GB, 1), "free_gb_before": round(avail / GB, 1), "free_gb_after": round(after / GB, 1),
           "cache_max_gb": CACHE_MAX_GB, "min_free_gb": MIN_FREE_GB, "actions": actions}
    cur.execute("insert into events(name, payload) values ('ops.disk_pruned', %s)", (Jsonb(rec),))
    log("disk pruned", rec["cache_gb"], "GB cache,", rec["free_gb_before"], "->", rec["free_gb_after"], "GB free")
    if after < MIN_FREE_GB * GB:
        stoppage.open_stoppage(cur, STOPPAGE_KEY, "env", f"ディスク（Docker の仮想ディスクか Mac 本体）の空きが {rec['free_gb_after']}GB（下限 {MIN_FREE_GB:g}GB）。"
                               "ビルドのキャッシュと名前のないイメージを片付けても足りない",
                               waiting_for="Mac 本体の空き（ディスクユーティリティ）と docker system df を見て、要らないファイル・イメージ・ボリュームを消す",
                               detected_by="dispatcher")
    else:
        stoppage.close_stoppage(cur, STOPPAGE_KEY, remedy="ビルドのキャッシュを片付けて空きが戻った", closed_by="dispatcher")
    return rec
