#!/usr/bin/env bash
# デザイン検査役のジョブ（ADR 0025。P4-10）: 標準入力の案（UXSpec とモック・キャプチャ）を、製品の specs/ と一緒に
# Engine の core-gates/ui_design_review.py に見せ、承認／直し／人へ を最終行に JSON で出す。
# 製品のリポジトリにも Issue にも何も書かない（作用は dispatcher が結果を見て行う）。
# 標準入力: dispatcher が組み立てた JSON（Issue、round、UXSpec、案のファイルの base64）
# 環境変数: JOB_ID REPO GH_TOKEN CLAUDE_CODE_OAUTH_TOKEN
set -euo pipefail

: "${JOB_ID:?}" "${REPO:?}" "${GH_TOKEN:?}" "${CLAUDE_CODE_OAUTH_TOKEN:?}"
if ! [[ "${REPO}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
  echo "[ui_design_review] not a repo"; exit 2
fi
ENGINE_REPO="${ENGINE_REPO:-guccyon/factory-engine}"
ENGINE_REF="${ENGINE_REF:-main}"
cat > /work/review_input.json
echo "[ui_design_review] job=${JOB_ID} repo=${REPO} engine=${ENGINE_REPO}@${ENGINE_REF} input_bytes=$(wc -c < /work/review_input.json)"
git config --global credential.helper '!f() { echo "username=x-access-token"; echo "password=${GH_TOKEN}"; }; f'
git clone --quiet --depth 1 --branch "${ENGINE_REF}" "https://github.com/${ENGINE_REPO}.git" /work/engine
DEFAULT_BRANCH=$(gh repo view "${REPO}" --json defaultBranchRef -q .defaultBranchRef.name 2>/dev/null || echo main)
git clone --quiet --depth 1 --branch "${DEFAULT_BRANCH}" "https://github.com/${REPO}.git" /work/product
set +e
python3 /work/engine/core-gates/ui_design_review.py --input /work/review_input.json --repo-dir /work/product --out /work/review.json 2>/work/review.err
RC=$?
set -e
sed 's/^/[ui_design_review.err] /' /work/review.err | tail -20 || true
if [ "${RC}" -ne 0 ] || [ ! -s /work/review.json ]; then
  echo "FACTORY_RESULT:$(jq -nc --arg rc "${RC}" '{kind:"ui_design_review", ok:false, exit:($rc|tonumber)}')"
  exit 1
fi
echo "FACTORY_RESULT:$(jq -c '{kind:"ui_design_review", ok:true, decision:.decision, human_reasons:.human_reasons, verdict:.verdict, cost_usd:(.cost_usd // 0), usage:(.usage // {}), model:.model, num_turns:.num_turns}' /work/review.json)"
exit 0
