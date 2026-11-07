#!/usr/bin/env bash
# 工場のイメージを依存の順に作る（ADR 0031 の決定 4）。手で更新するときも、更新役も、これを使う。
#
#   scripts/build.sh            # 作業ツリーの HEAD の版で作る
#   scripts/build.sh --dry-run  # 作る順とコマンドだけ出す
#
# - 土台（factory-dispatcher:p0・factory-worker:p0）を先に、それに重ねる派生（factory-shipper:p2、worker の design・rust・flutter）を後に作る。
#   並べて作ると派生が古い土台に乗る（2026-11-05、design が古い p0 に乗った）。`docker compose up -d --build` はワーカーのイメージを作らないので使わない
# - どのイメージにも版のラベル factory.engine_version=<コミット> を付け、<名前>:<コミット> の名前も付ける（前の版に戻すときに作り直さない）
# - dispatcher と shipper には ENGINE_VERSION を焼き込む（来歴。.env には書かない）
# - rust・flutter は、このマシンにすでにあるときだけ作り直す（使っていない製品の重いイメージを作らない）。FORCE_ALL=1 で全部
#
# 作ったら、すべてのサービスを上げる: docker compose up -d   その後 docker compose exec dispatcher factoryctl version --expect <コミット>
set -euo pipefail
cd "$(dirname "$0")/.."

DRY=0
[[ "${1:-}" == "--dry-run" ]] && DRY=1
VERSION=$(git rev-parse --short=7 HEAD)
if [[ -n "$(git status --porcelain -- . ../definitions ../core-gates ../health)" ]]; then
  echo "作業ツリーに未コミットの変更がある。版のラベルと中身がずれるので止める（git status を見る）" >&2
  exit 2
fi

run() { echo "+ $*"; [[ $DRY == 1 ]] || "$@"; }
exists() { docker image inspect "$1" >/dev/null 2>&1; }

# name|context|dockerfile|always(1)/when-exists(0)
STEPS=(
  "factory-dispatcher:p0|dispatcher|dispatcher/Dockerfile|1"
  "factory-shipper:p2|dispatcher|dispatcher/Dockerfile.shipper|1"
  "factory-worker:p0|worker|worker/Dockerfile|1"
  "factory-worker:design|worker|worker/Dockerfile.design|1"
  "factory-worker:rust|worker|worker/Dockerfile.rust|0"
  "factory-worker:flutter|worker|worker/Dockerfile.flutter|0"
)

echo "版 $VERSION のイメージを作る"
for step in "${STEPS[@]}"; do
  IFS='|' read -r name ctx file always <<<"$step"
  if [[ $always == 0 && ${FORCE_ALL:-0} != 1 ]] && ! exists "$name"; then
    echo "- $name はこのマシンにないので作らない（FORCE_ALL=1 で作る）"
    continue
  fi
  repo=${name%%:*}
  run docker build -t "$name" -t "$repo:${name#*:}-$VERSION" -f "$file" \
    --build-arg "ENGINE_VERSION=$VERSION" --label "factory.engine_version=$VERSION" "$ctx"
done
echo "できた: 版 ${VERSION}。次は docker compose up -d（すべてのサービス）"
