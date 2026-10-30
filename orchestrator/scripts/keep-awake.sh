#!/bin/sh
# 工場（Compose の project "factory"）のコンテナが動いている間だけ、ホスト（macOS）のスリープを止める（ADR 0011 の 7）。
# 工場が止まっていれば何もしないので、普段どおり眠れる。pmset などシステムの設定は変えない。
# 操業コンソールのトグル（稼働中は Mac をスリープさせない）で切ったときも眠れる。コンソールに問い合わせられなければ止める側に倒す
# 使い方:
#   scripts/keep-awake.sh install     ログインのたびに launchd が常駐させる（落ちても起こし直す）
#   scripts/keep-awake.sh uninstall   元に戻す（LaunchAgent を外して消す）
#   scripts/keep-awake.sh status      常駐しているか、いまスリープを止めているか
#   scripts/keep-awake.sh             見張りを前面で動かす（launchd が呼ぶもの）
# macOS 以外では何もしない
[ "$(uname)" = "Darwin" ] || exit 0

label="local.factory.keepawake"
plist="$HOME/Library/LaunchAgents/$label.plist"
project="${FACTORY_COMPOSE_PROJECT:-factory}"
interval="${KEEP_AWAKE_INTERVAL:-120}"
console="${CONSOLE_URL:-http://127.0.0.1:${CONSOLE_HOST_PORT:-8790}}"
PATH="/usr/local/bin:/opt/homebrew/bin:/Applications/Docker.app/Contents/Resources/bin:$PATH"

factory_running() {
  docker ps -q --filter "label=com.docker.compose.project=$project" --filter status=running 2>/dev/null | grep -q .
}

case "${1:-run}" in
  install)
    script="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
    mkdir -p "$(dirname "$plist")"
    cat >"$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array><string>/bin/sh</string><string>$script</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>/tmp/$label.log</string>
</dict></plist>
EOF
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null
    launchctl bootstrap "gui/$(id -u)" "$plist"
    # 以前の手で起動する版（Docker の pid に紐づけた caffeinate）が残っていれば止める
    pkill -f "caffeinate -dimsu -w" 2>/dev/null
    echo "installed: $plist"
    ;;
  uninstall)
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null
    rm -f "$plist"
    echo "uninstalled"
    ;;
  status)
    if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then echo "agent: loaded"; else echo "agent: not loaded"; fi
    if factory_running; then echo "factory: running"; else echo "factory: stopped"; fi
    if pgrep -f "caffeinate -ims -t" >/dev/null 2>&1; then echo "sleep: prevented"; else echo "sleep: allowed"; fi
    ;;
  run)
    # 工場が動いていて、コンソールのトグルが切られていなければ interval 秒だけスリープを止め、また確かめる。それ以外は待つだけ
    # 問い合わせは居ることの知らせを兼ねる（コンソールは連絡があるときだけトグルを出す）
    while :; do
      if factory_running; then state=running; else state=stopped; fi
      setting=$(curl -fsS -m 5 "$console/api/host/keep-awake?factory=$state" 2>/dev/null)
      if [ "$state" = running ] && ! printf '%s' "$setting" | grep -q '"prevent_sleep": *false'; then
        caffeinate -ims -t "$interval"
      else
        sleep "$interval"
      fi
    done
    ;;
  *)
    echo "usage: $0 [install|uninstall|status]" >&2
    exit 2
    ;;
esac
