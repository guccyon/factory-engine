#!/bin/sh
# 工場が動いている間、ホスト（macOS）をスリープさせない（D8）。Docker Desktop の動いている間だけ caffeinate を保つ。
# 使い方: scripts/keep-awake.sh &   （ログインのたびに一度。すでに動いていれば何もしない）
# macOS 以外では何もしない。pmset のようにシステムの設定は変えない（止めれば元に戻る）
[ "$(uname)" = "Darwin" ] || exit 0
if pgrep -f "caffeinate -dimsu -w" >/dev/null 2>&1; then
  exit 0
fi
pid=$(pgrep -x "com.docker.backend" | head -1)
[ -n "$pid" ] || pid=$(pgrep -f "Docker Desktop" | head -1)
[ -n "$pid" ] || { echo "Docker Desktop が動いていない" >&2; exit 1; }
exec caffeinate -dimsu -w "$pid"
