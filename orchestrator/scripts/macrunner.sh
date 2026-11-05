#!/bin/sh
# Mac ランナー（ADR 0028 の決定 3）をホスト（macOS）に常駐させる。工場のコンテナから host.docker.internal 越しに呼ばれる。
# 使い方:
#   scripts/macrunner.sh install     トークンを作り（なければ）、ログインのたびに launchd が常駐させる（落ちても起こし直す）
#   scripts/macrunner.sh uninstall   元に戻す（LaunchAgent を外して消す。トークンとビルドの作業場は残す）
#   scripts/macrunner.sh status      常駐しているか、/health に答えるか、足りない道具
#   scripts/macrunner.sh token       トークンを出す（.env の MAC_RUNNER_TOKEN に写す）
# install のときの PATH（Flutter・idb の場所）を launchd に渡す。Flutter を入れ直したら install し直す
# macOS 以外では何もしない
[ "$(uname)" = "Darwin" ] || { echo "macOS だけで動く"; exit 0; }

label="local.factory.macrunner"
plist="$HOME/Library/LaunchAgents/$label.plist"
token_file="${MAC_RUNNER_TOKEN_FILE:-$HOME/.config/factory/macrunner.token}"
port="${MAC_RUNNER_PORT:-8793}"
apps="${MAC_RUNNER_APPS:-guccyon/rally:apps/student-app:jp.rally.rallyStudentApp}"
here="$(cd "$(dirname "$0")/.." && pwd)"
runner="$here/macrunner/runner.py"
py="${MAC_RUNNER_PYTHON:-/usr/bin/python3}"

case "${1:-status}" in
  install)
    if [ ! -s "$token_file" ]; then
      mkdir -p "$(dirname "$token_file")"
      (umask 077; "$py" -c 'import secrets; print(secrets.token_hex(32))' >"$token_file")
      echo "トークンを作った: ${token_file}（.env の MAC_RUNNER_TOKEN に写す: scripts/macrunner.sh token）"
    fi
    chmod 600 "$token_file"
    # pip3 install --user fb-idb は idb を ~/Library/Python/<版>/bin に入れ、そこは普段の PATH の外。launchd に渡す PATH に足す
    for d in "$HOME"/Library/Python/*/bin; do
      [ -x "$d/idb" ] && case ":$PATH:" in *":$d:"*) ;; *) PATH="$PATH:$d" ;; esac
    done
    mkdir -p "$(dirname "$plist")" "$HOME/Library/Logs/factory-macrunner"
    cat >"$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array><string>$py</string><string>$runner</string><string>serve</string></array>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>$PATH</string>
    <key>MAC_RUNNER_PORT</key><string>$port</string>
    <key>MAC_RUNNER_APPS</key><string>$apps</string>
    <key>MAC_RUNNER_TOKEN_FILE</key><string>$token_file</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/factory-macrunner/runner.log</string>
</dict></plist>
EOF
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null
    launchctl bootstrap "gui/$(id -u)" "$plist"
    PATH="$PATH" "$py" "$runner" preflight || echo "（足りない道具があっても常駐はする。足りない道具を使うと失敗を返す）"
    echo "常駐させた: ${label}（127.0.0.1:${port}）"
    ;;
  uninstall)
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null
    rm -f "$plist"
    echo "外した: $label"
    ;;
  token)
    cat "$token_file"
    ;;
  status)
    if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then echo "常駐: あり"; else echo "常駐: なし（install する）"; fi
    curl -fsS "http://127.0.0.1:$port/health" && echo || echo "/health に答えない（~/Library/Logs/factory-macrunner/runner.log を見る）"
    "$py" "$runner" preflight
    ;;
  *)
    sed -n '2,8p' "$0"; exit 2
    ;;
esac
