#!/usr/bin/env bash
# 週 1 回の設計の診断のジョブ（ADR 0022）: 製品の main を浅く取り、Engine の core-gates/design_review.py に読ませて、所見を最終行に JSON で出す。
# 製品のリポジトリには何も書かない（固定 Issue への書き込みは dispatcher が結果を見て行う）。
# 標準入力: 前回の所見（制御プレーンが組み立てた JSON の配列。なければ []）
# 環境変数: JOB_ID REPO GH_TOKEN CLAUDE_CODE_OAUTH_TOKEN
set -euo pipefail

: "${JOB_ID:?}" "${REPO:?}" "${GH_TOKEN:?}" "${CLAUDE_CODE_OAUTH_TOKEN:?}"
if ! [[ "${REPO}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
  echo "[design_review] not a repo"; exit 2
fi
ENGINE_REPO="${ENGINE_REPO:-guccyon/factory-engine}"
ENGINE_REF="${ENGINE_REF:-main}"
cat > /work/previous.json
echo "[design_review] job=${JOB_ID} repo=${REPO} engine=${ENGINE_REPO}@${ENGINE_REF}"
git config --global credential.helper '!f() { echo "username=x-access-token"; echo "password=${GH_TOKEN}"; }; f'
git clone --quiet --depth 1 --branch "${ENGINE_REF}" "https://github.com/${ENGINE_REPO}.git" /work/engine
DEFAULT_BRANCH=$(gh repo view "${REPO}" --json defaultBranchRef -q .defaultBranchRef.name 2>/dev/null || echo main)
git clone --quiet --depth 1 --branch "${DEFAULT_BRANCH}" "https://github.com/${REPO}.git" /work/product
set +e
python3 /work/engine/core-gates/design_review.py --repo-dir /work/product --repo "${REPO}" --previous /work/previous.json --out /work/design.json 2>/work/design.err
RC=$?
set -e
sed 's/^/[design_review.err] /' /work/design.err | tail -20 || true
if [ "${RC}" -ne 0 ] || [ ! -s /work/design.json ]; then
  echo "FACTORY_RESULT:$(jq -nc --arg repo "${REPO}" --arg rc "${RC}" '{kind:"design_review", repo:$repo, ok:false, exit:($rc|tonumber)}')"
  exit 1
fi
echo "FACTORY_RESULT:$(jq -c '{kind:"design_review", repo:.repo, sha:.sha, ok:true, report:.result, cost_usd:(.cost_usd // 0), usage:(.usage // {}), model:.model, num_turns:.num_turns}' /work/design.json)"
exit 0
