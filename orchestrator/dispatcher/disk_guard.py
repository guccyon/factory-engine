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
- 空きが DISK_MIN_FREE_GB を割っている間は、dispatcher が新しいジョブを取らない（too_low。_claim_loop が見る）
- 動いているジョブのコンテナの書き込み層が DISK_JOB_MAX_GB（既定 30）を超えたら、そのコンテナを止めて人へ回す（ジョブは exit=137 で落ち、自動の立て直しはしない）。
  11/14 夜、rust のジョブ 1 本の書き込み層が 54GB になり、Mac の空きが 23GB まで減った
- 空きが DISK_CRITICAL_FREE_GB（既定 6）を割ったら、上限の内でも書き込み層のいちばん大きいジョブを 1 本止めて人へ回す（11/15 夜: rust のジョブが
  2 本並ぶと、上限 30GB の内のまま 16 分で 10GB 減り、満杯の手前まで行った。新しいジョブを取らないだけでは、動いているジョブが書き続ける）
- 大きさは docker の表記と同じ 10 進の GB で比べる。ビルドのキャッシュはエンジンの API から、イメージと共有していない（prune で消せる）分を測る
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
CACHE_MIN_AGE_H = int(ENV.get("DISK_BUILD_CACHE_MIN_AGE_H", "24"))   # これより新しいキャッシュは prune しない
MIN_FREE_GB = float(ENV.get("DISK_MIN_FREE_GB", "15"))
HOST_PATH = ENV.get("DISK_HOST_PATH", "/definitions")   # ホストから bind mount したパス（statvfs がホストのディスクを返す）
KEEP_RELEASES = int(ENV.get("DISK_KEEP_RELEASES", "3"))
BEFORE_TAG_DAYS = int(ENV.get("DISK_BEFORE_TAG_DAYS", "7"))
JOB_MAX_GB = float(ENV.get("DISK_JOB_MAX_GB", "30"))
CRITICAL_FREE_GB = float(ENV.get("DISK_CRITICAL_FREE_GB", "6"))   # これを割ったら、上限の内でもいちばん大きいジョブを止める（満杯で工場ごと止まるより軽い）
JOB_LABEL = "factory.job"   # reattach.JOB_LABEL と同じ
STOPPAGE_KEY = "disk:low"
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
GB = 1000 ** 3   # docker の表記（10 進）と同じ単位で比べる（11/14: 1024³ と 10 進を混ぜ、21.36GB のキャッシュを上限 20GB より下と見ていた）
DOCKER_SOCK = ENV.get("DOCKER_SOCK", "/var/run/docker.sock")
UNITS = {"b": 1, "kb": 1000, "mb": 1000 ** 2, "gb": 1000 ** 3, "tb": 1000 ** 4}


def _docker(args: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def size_bytes(s: str) -> float:
    """docker の大きさの表記（"12.3GB"・"512MB"・"0B"）をバイトに。読めなければ 0"""
    m = re.match(r"^\s*([\d.]+)\s*([kKmMgGtT]?[bB])\s*$", s or "")
    return float(m.group(1)) * UNITS[m.group(2).lower()] if m else 0.0


def engine_build_cache_bytes(sock: str | None = None) -> float | None:
    """ビルドのキャッシュのうち、イメージと共有していない分（`builder prune --keep-storage` で消せる分）を Docker Engine の API（/system/df）から。
    読めなければ None。CLI の版で数え方が変わる（11/14: CLI 26.1.5 は共有分抜き 21.36GB、ホストの新しい CLI は全体 45.43GB）ので、エンジンに直接聞く。
    共有分（11/15: 30.17GB のうち 22.32GB）は prune では消えず、古いイメージを外したときに減る。全体で比べると上限を下回れず、5 分ごとに空回りした"""
    import http.client
    import socket
    path = sock or DOCKER_SOCK
    if not os.path.exists(path):
        return None

    class _Conn(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(self.timeout)
            self.sock.connect(path)
    try:
        c = _Conn("localhost", timeout=120)
        c.request("GET", "/system/df?type=build-cache")
        r = c.getresponse()
        if r.status != 200:
            return None
        rows = json.loads(r.read().decode() or "{}").get("BuildCache") or []
        return float(sum(int(x.get("Size") or 0) for x in rows if not x.get("Shared")))
    except (OSError, ValueError, http.client.HTTPException):
        return None


def build_cache_bytes(docker=_docker, engine=engine_build_cache_bytes) -> float:
    """ビルドのキャッシュのうち、prune で消せる分。エンジンの API が読めればそれを、読めなければ CLI の system df を使う"""
    e = engine() if engine else None
    if e is not None:
        return e
    r = docker(["system", "df", "--format", "{{json .}}"], timeout=120)
    for line in (r.stdout or "").splitlines() if r.returncode == 0 else []:
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


def too_low() -> bool:
    """新しいジョブを取らないほど空きが少ないか（見られなければ False）"""
    if not ENABLED:
        return False
    try:
        return free_bytes() < MIN_FREE_GB * GB
    except OSError:
        return False


def oversized_jobs(docker=_docker, max_gb: float | None = None) -> list[dict]:
    """書き込み層が max_gb（既定 JOB_MAX_GB）を超えた、動いているジョブのコンテナ。大きい順"""
    limit = (JOB_MAX_GB if max_gb is None else max_gb) * 1e9
    r = docker(["ps", "--size", "--filter", f"label={JOB_LABEL}", "--format", "{{json .}}"], timeout=300)
    out = []
    for line in (r.stdout or "").splitlines() if r.returncode == 0 else []:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        rw = size_bytes(((row.get("Size") or "").split("(")[0]).strip())   # "54.6GB (virtual 60GB)"
        if rw > limit:
            # docker ps -s は書き込み層を多めに数える（11/14: 中の du で 13GB のとき 39GB と出た。cargo のハードリンクを重ねて数える見込み）。
            # 中の du（-x で 1 つのファイルシステムだけ）でも上限を超えたときだけ止める。du が取れなければ ps の値で判断する
            d = docker(["exec", row.get("Names") or "", "du", "-sxm", "/"], timeout=240)
            m = re.match(r"^\s*(\d+)", d.stdout or "") if d.returncode == 0 else None
            if m:
                rw = min(rw, int(m.group(1)) * 1e6)
        if rw > limit:
            out.append({"name": row.get("Names"), "size_gb": round(rw / 1e9, 1), "labels": row.get("Labels") or ""})
    return sorted(out, key=lambda c: -c["size_gb"])


def _kill_job(cur, c: dict, why: str, log, docker, extra: dict) -> bool:
    import stoppage
    m = re.search(re.escape(JOB_LABEL) + r"=(\d+)", c["labels"])
    job_id = int(m.group(1)) if m else None
    if docker(["kill", c["name"]], timeout=60).returncode != 0:
        return False
    cur.execute("insert into events(name, job_id, payload) values ('ops.job_disk_killed', %s, %s)",
                (job_id, Jsonb({"container": c["name"], "size_gb": c["size_gb"], **extra})))
    stoppage.open_stoppage(cur, f"diskjob:{job_id or c['name']}", "factory-bug", f"ジョブ {job_id} のコンテナの書き込み層が {c['size_gb']}GB。{why}",
                           waiting_for="何がそこまで大きくなったか（Rust の target・生成物）を見て、チケットを直すか積み直す", detected_by="dispatcher", job_id=job_id)
    log("job container killed for disk", c["name"], c["size_gb"], why)
    return True


def stop_oversized_jobs(cur, log=print, *, docker=_docker, free=None) -> list[dict]:
    killed = [c for c in oversized_jobs(docker)
              if _kill_job(cur, c, f"上限 {JOB_MAX_GB:g}GB を超えたので止めた", log, docker, {"max_gb": JOB_MAX_GB})]
    if free is not None and not killed and free() < CRITICAL_FREE_GB * GB:   # 満杯の手前。上限の内でもいちばん大きいジョブを 1 本止める
        biggest = oversized_jobs(docker, max_gb=0)[:1]
        killed += [c for c in biggest if _kill_job(cur, c, f"ディスクの空きが {CRITICAL_FREE_GB:g}GB を割ったので、いちばん大きいジョブを止めた",
                                                  log, docker, {"critical_free_gb": CRITICAL_FREE_GB})]
    return killed


def step(cur, log=print, *, docker=_docker, free=free_bytes) -> dict | None:
    """見て、要れば片付ける。片付けたら記録の中身を返す"""
    if not ENABLED:
        return None
    import stoppage
    stop_oversized_jobs(cur, log, docker=docker, free=free)
    cache, avail = build_cache_bytes(docker, engine_build_cache_bytes if docker is _docker else None), free()   # 試験の偽の docker では本物のエンジンに聞かない
    low = avail < MIN_FREE_GB * GB
    if cache <= CACHE_MAX_GB * GB and not low:
        stoppage.close_stoppage(cur, STOPPAGE_KEY, remedy="ディスクの空きが戻った", closed_by="dispatcher")
        return None
    actions = []
    # 作ったばかりの層は消さない（11/15: 更新で作り直した worker の Flutter の層 1.79GB を 7 秒後に消し、次の更新でまた作っていた）。
    # 空きが下限を割っているときは新しい層も消す（止まるよりは作り直す方がよい）
    r = docker(["builder", "prune", "-f", "--keep-storage", f"{int(CACHE_KEEP_GB * 1000)}MB"]
               + ([] if low else ["--filter", f"until={CACHE_MIN_AGE_H}h"]))
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
