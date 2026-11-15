#!/usr/bin/env bash
# Engine を /work/engine に取る（全ワーカー共通）。ENGINE_REF がブランチ名なら浅いクローン、
# コミット（短いハッシュも可）なら履歴だけ取ってその版を出す（#235: ワーカーの版を工場の版にそろえる）。
# 使い方: clone_engine.sh <ENGINE_REPO> <ENGINE_REF> [置き場（既定 /work/engine）]
set -euo pipefail
repo="$1"; ref="$2"; dest="${3:-/work/engine}"
url="https://github.com/${repo}.git"
if [[ "${ref}" =~ ^[0-9a-f]{7,40}$ ]]; then
  git clone --quiet --filter=blob:none --no-checkout "${url}" "${dest}"
  git -C "${dest}" checkout --quiet "${ref}"
else
  git clone --quiet --depth 1 --branch "${ref}" "${url}" "${dest}"
fi
