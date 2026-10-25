#!/usr/bin/env bash
# 対象リポジトリに工場のラベルを作る。使い方: scripts/labels.sh guccyon/rally
set -euo pipefail
repo="${1:?repo (owner/name)}"
create() { gh label create "$1" -R "$repo" --color "$2" --description "$3" --force >/dev/null && echo "label $1"; }
create "factory:implement" "1f5f8b" "工場に実装を依頼する（付けると 30 秒以内にジョブになる）"
create "factory:queued"    "9aa5b4" "工場が受け付け、順番待ち"
create "factory:running"   "e39255" "ワーカーが実行中"
create "factory:pr-open"   "2e7d4f" "PR を作成済み。マージは人間の判断"
create "factory:failed"    "a63a3a" "失敗。factoryctl rerun で再実行"
