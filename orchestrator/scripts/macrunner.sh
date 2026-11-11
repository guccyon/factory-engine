#!/bin/sh
# Mac ランナー（ADR 0028 の決定 3、ADR 0033）をホスト（macOS）に常駐させる。ランナーは工場のランナーの窓口へ仕事を取りに行く（待ち受けない）。
# 使い方:
#   scripts/macrunner.sh install     工場とペアリングし（まだなら）、ログインのたびに launchd が常駐させる（落ちても起こし直す）
#   scripts/macrunner.sh pair        ペアリングだけやり直す（常駐はそのまま。鍵を作り直す）
#   scripts/macrunner.sh uninstall   元に戻す（LaunchAgent を外し、工場に知らせて鍵を捨てる。ビルドの作業場とログは残す）
#   scripts/macrunner.sh status      常駐しているか、ペアリング済みか、足りない道具
#   scripts/macrunner.sh android-key 審査前の配布（ADR 0047）に使う Android の署名の鍵を作る。パスワードはキーチェーンに入れる（1 回だけ）
# 審査前の配布を開くときは、install の前に MAC_RUNNER_FIREBASE_APP_ID（と MAC_RUNNER_FIREBASE_GROUPS）を入れる。
#   例: MAC_RUNNER_FIREBASE_APP_ID=1:123:android:abc scripts/macrunner.sh install
# 窓口の場所は MAC_RUNNER_GATE（既定 https://127.0.0.1:8794。別の Mac の工場なら https://<その Mac の名前か IP>:8794）
# install のときの PATH（Flutter・idb の場所）を launchd に渡す。Flutter を入れ直したら install し直す
# macOS 以外では何もしない
[ "$(uname)" = "Darwin" ] || { echo "macOS だけで動く"; exit 0; }

label="local.factory.macrunner"
plist="$HOME/Library/LaunchAgents/$label.plist"
apps="${MAC_RUNNER_APPS:-guccyon/rally:apps/student-app:jp.rally.rallyStudentApp}"
gate="${MAC_RUNNER_GATE:-https://127.0.0.1:8794}"
here="$(cd "$(dirname "$0")/.." && pwd)"
runner="$here/macrunner/runner.py"
py="${MAC_RUNNER_PYTHON:-/usr/bin/python3}"
# pip3 install --user fb-idb は idb を ~/Library/Python/<版>/bin に入れ、そこは普段の PATH の外。launchd に渡す PATH と調べる PATH に足す
for d in "$HOME"/Library/Python/*/bin; do
  [ -x "$d/idb" ] && case ":$PATH:" in *":$d:"*) ;; *) PATH="$PATH:$d" ;; esac
done
export MAC_RUNNER_GATE="$gate" MAC_RUNNER_APPS="$apps"
keystore="${MAC_RUNNER_ANDROID_KEYSTORE:-$HOME/.config/factory/android-upload.jks}"
kc_service="${MAC_RUNNER_KEYCHAIN_SERVICE:-factory-macrunner-android-keystore}"
# 審査前の配布の設定（入れたものだけ plist に書く。値は Mac の中だけに置き、リポジトリには書かない）
dist_env=""
for k in MAC_RUNNER_FIREBASE_APP_ID MAC_RUNNER_FIREBASE_GROUPS MAC_RUNNER_FIREBASE MAC_RUNNER_ANDROID_KEYSTORE MAC_RUNNER_ANDROID_KEY_ALIAS MAC_RUNNER_APKSIGNER ANDROID_HOME; do
  eval "v=\${$k:-}"
  [ -n "$v" ] && dist_env="$dist_env
    <key>$k</key><string>$v</string>"
done

case "${1:-status}" in
  install|pair)
    if [ "$1" = "pair" ] || ! "$py" "$runner" paired; then
      "$py" "$runner" pair || { echo "ペアリングできなかった（工場の操業コンソールを開いて、もう一度 install する）"; exit 1; }
    fi
    [ "$1" = "pair" ] && { launchctl kickstart -k "gui/$(id -u)/$label" 2>/dev/null; exit 0; }
    mkdir -p "$(dirname "$plist")" "$HOME/Library/Logs/factory-macrunner"
    cat >"$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array><string>$py</string><string>$runner</string><string>serve</string></array>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>$PATH</string>
    <key>MAC_RUNNER_APPS</key><string>$apps</string>$dist_env
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/factory-macrunner/runner.log</string>
</dict></plist>
EOF
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null
    launchctl bootstrap "gui/$(id -u)" "$plist"
    "$py" "$runner" preflight || echo "（足りない道具があっても常駐はする。足りない道具を使うと失敗を返す）"
    echo "常駐させた: ${label}（工場の窓口 ${gate} へ取りに行く）"
    ;;
  uninstall)
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null
    rm -f "$plist"
    "$py" "$runner" bye   # 工場に知らせて、両側の鍵を捨てる（工場が止まっていれば、こちらの鍵だけ捨てる）
    echo "外した: $label"
    ;;
  android-key)
    # 鍵は Mac にだけ置く。パスワードは乱数で作ってキーチェーンに入れ、ランナーは配布のたびにキーチェーンから読む（工場には渡さない）
    [ -e "$keystore" ] && { echo "もうある: ${keystore}（作り直すと、配ったアプリの更新ができなくなる。消すのは人の判断で）"; exit 1; }
    command -v keytool >/dev/null || { echo "keytool が見つからない（Android Studio の JDK。例: PATH=\"/Applications/Android Studio.app/Contents/jbr/Contents/Home/bin:\$PATH\"）"; exit 1; }
    mkdir -p "$(dirname "$keystore")" && chmod 700 "$(dirname "$keystore")"
    FACTORY_KS_PASS="$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32)"; export FACTORY_KS_PASS
    keytool -genkeypair -keystore "$keystore" -storetype PKCS12 -alias "${MAC_RUNNER_ANDROID_KEY_ALIAS:-upload}" -keyalg RSA -keysize 4096 \
      -validity 10000 -dname "CN=factory preview" -storepass:env FACTORY_KS_PASS -keypass:env FACTORY_KS_PASS || exit 1
    chmod 600 "$keystore"
    # -T /usr/bin/security: ランナーが security で読むときに毎回の許可を出さない（ほかのアプリには許可を求める）
    security add-generic-password -U -a "$USER" -s "$kc_service" -T /usr/bin/security -w "$FACTORY_KS_PASS" || exit 1
    unset FACTORY_KS_PASS
    echo "作った: ${keystore}（パスワードはキーチェーンの ${kc_service}）。鍵はバックアップしておく（なくすと同じアプリの更新を配れない）"
    ;;
  status)
    if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then echo "常駐: あり"; else echo "常駐: なし（install する）"; fi
    if "$py" "$runner" paired; then echo "ペアリング: 済み"; else echo "ペアリング: まだ（install する）"; fi
    tail -n 3 "$HOME/Library/Logs/factory-macrunner/runner.log" 2>/dev/null
    # 常駐しているランナーと同じ設定で確かめる（打ったシェルの環境変数ではなく、plist の EnvironmentVariables を使う）
    if [ -f "$plist" ]; then
      for k in PATH MAC_RUNNER_APPS MAC_RUNNER_FIREBASE_APP_ID MAC_RUNNER_FIREBASE_GROUPS MAC_RUNNER_FIREBASE MAC_RUNNER_ANDROID_KEYSTORE MAC_RUNNER_ANDROID_KEY_ALIAS MAC_RUNNER_APKSIGNER ANDROID_HOME; do
        v="$(/usr/libexec/PlistBuddy -c "Print :EnvironmentVariables:$k" "$plist" 2>/dev/null)" && export "$k=$v"
      done
    fi
    "$py" "$runner" preflight
    ;;
  *)
    sed -n '2,13p' "$0"; exit 2
    ;;
esac
