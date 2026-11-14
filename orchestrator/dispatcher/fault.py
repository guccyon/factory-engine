"""工場の中で握りつぶした例外を数えて、束ねて記録する（factory-engine#308）。

supervisor と dispatcher は、1 つの工程の失敗でラインを止めないよう、例外を握りつぶしてログに出すだけにしている
（`log("notify report failed", repr(e))` の形）。11-08 の更新で `notify report failed IndexError(2)` が 12 時間に 425 回出たが、
停止・受信トレイ・Slack のどこにも出ず、オーナーが「Slack に届いていない」と気づくまで誰も知らなかった。

- 各プロセスの log() から note_log() を呼ぶ。最初の引数が「… failed」「… error」で、例外の repr（`IndexError(2)` の形）が
  引数にあれば、(プロセス, 文言, 例外の型) を 1 つの印として数える。呼び出し元の 35 か所は書き換えない
- 数はメモリに貯め、flush() で FLUSH_SEC に 1 回だけ events の factory.fault に 1 印 1 行で書く（1 時間に 55 回出ても 12 行）
- 書けなかったら（DB が落ちているなど）数を残して次に回す。ここでの失敗でもラインは止めない
- 繰り返しの判定と停止は repeat_watch.py（番人）
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
import time

from psycopg.types.json import Jsonb

ENV = os.environ
FLUSH_SEC = int(ENV.get("FAULT_FLUSH_SEC", "300"))
_EXC = re.compile(r"^<?((?:[a-z_]\w*\.)*[A-Z]\w*)[( ]")   # repr(e) の頭（IndexError(2)、UndefinedColumn('…')、<HTTPError 404: 'Not Found'>）
_LABEL = re.compile(r"\b(failed|error)$")

_lock = threading.Lock()
_counts: dict[str, dict] = {}
_last_flush = time.time()


def signature(component: str, label: str, exc: str) -> str:
    """停止の key に使う印。空白は _ にそろえる"""
    return f"{component}:{re.sub(r'\s+', '_', label.strip())}:{exc}"


def parse(args: tuple) -> tuple[str, str, str] | None:
    """log() の引数から (文言, 例外の型, 見本) を取る。握りつぶした例外のログでなければ None"""
    if not args or not isinstance(args[0], str) or not _LABEL.search(args[0].strip()):
        return None
    for a in reversed(args[1:]):
        s = str(a)
        m = _EXC.match(s)
        if m:
            return args[0].strip(), m.group(1).rsplit(".", 1)[-1], s[:300]
    return None


# 名前解決・DB や外への接続が落ちた例外は、工程ごとに分けず「工場の環境の不調」1 つの印にまとめる。
# 11-14 01:16〜 Docker の VM が詰まって名前解決が途切れ途切れに落ち、工程ごとに 7 件の repeat: が開いた（原因は 1 つ）
ENV_COMPONENT = "env"
ENV_SIGNATURE = "env:network"
_ENV_MARKERS = ("name resolution", "name or service not known", "could not translate host name", "nodename nor servname",
                "connection refused", "closed the connection unexpectedly", "connection timed out", "network is unreachable")


def is_env(sample: str) -> bool:
    s = (sample or "").lower()
    return any(m in s for m in _ENV_MARKERS)


def _normalize(sample: str) -> str:
    """見本から、回ごとに変わる部分（引用した値・数・16 進）を落とす。同じ根っこのエラーが同じ文になるように"""
    s = (sample or "").lower()
    s = re.sub(r"'[^']*'|\"[^\"]*\"", "''", s)
    s = re.sub(r"0x[0-9a-f]+|\b[0-9a-f]{12,}\b", "x", s)
    s = re.sub(r"\d+", "0", s)
    return re.sub(r"\s+", " ", s).strip()[:160]


def root_signature(exc: str, sample: str) -> str:
    """停止の key に使う印。どの工程で出たかでなく、どんなエラーか（例外の型と、回ごとに変わる部分を落とした中身）で作る。
    11-14 に同じ名前解決の失敗が工程ごとに 7 件の判断待ちになった（オーナー「同じ内容は最新の 1 件だけでよい」）。
    名前解決・接続の失敗はまとめて env:network"""
    if is_env(sample):
        return ENV_SIGNATURE
    return f"{exc}:{hashlib.sha1(_normalize(sample).encode()).hexdigest()[:10]}"


def note(component: str, label: str, exc: str, sample: str = "") -> None:
    sig = root_signature(exc, sample)
    where = f"{component}: {label}"   # 最後に出た工程（見本の手がかり）
    if sig == ENV_SIGNATURE:
        component, exc = ENV_COMPONENT, "network"
    now = time.time()
    with _lock:
        c = _counts.get(sig)
        if c is None:
            c = _counts[sig] = {"component": component, "label": where, "exc": exc, "count": 0, "first": now}
        c["label"] = where
        c["count"] += 1
        c["last"] = now
        c["sample"] = sample


def note_log(component: str, args: tuple) -> None:
    """各プロセスの log() から呼ぶ。ここでの失敗は飲み込む（ログを止めない）"""
    try:
        p = parse(args)
        if p:
            note(component, *p)
    except Exception:  # noqa: BLE001
        pass


def pending() -> dict[str, dict]:
    with _lock:
        return {k: dict(v) for k, v in _counts.items()}


def flush(cur, *, force: bool = False) -> int:
    """貯めた数を events の factory.fault に書く。書いた印の数を返す"""
    global _last_flush
    now = time.time()
    if not force and now - _last_flush < FLUSH_SEC:
        return 0
    with _lock:
        batch = dict(_counts)
        _counts.clear()
        _last_flush = now
    written = 0
    try:
        for sig, c in batch.items():
            cur.execute("insert into events(name, payload) values ('factory.fault', %s)",
                        (Jsonb({"signature": sig, "component": c["component"], "label": c["label"], "exc": c["exc"],
                                "count": c["count"], "sample": c.get("sample") or "",
                                "first_at": c["first"], "last_at": c["last"]}),))
            written += 1
    except Exception:  # noqa: BLE001  書けなかった分は次に回す（数は足し戻す）
        with _lock:
            for sig, c in list(batch.items())[written:]:
                cur_c = _counts.get(sig)
                if cur_c is None:
                    _counts[sig] = c
                else:
                    cur_c["count"] += c["count"]
                    cur_c["first"] = min(cur_c["first"], c["first"])
        raise
    return written
