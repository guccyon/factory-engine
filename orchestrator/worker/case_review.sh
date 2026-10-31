#!/usr/bin/env bash
# 事例型の分析役のジョブ（ADR 0023）: 標準入力の出来事の記録を Engine の core-gates/case_review.py に読ませ、結果を最終行に JSON で出す。
# 製品のリポジトリには何も書かない（記録は dispatcher が結果を見て残す）。
# 標準入力: dispatcher が組み立てた JSON（制御プレーンの記録と、後片付けのアーカイブからクレンジングしたセッションの抜粋）
# 環境変数: JOB_ID GH_TOKEN CLAUDE_CODE_OAUTH_TOKEN
set -euo pipefail

: "${JOB_ID:?}" "${GH_TOKEN:?}" "${CLAUDE_CODE_OAUTH_TOKEN:?}"
ENGINE_REPO="${ENGINE_REPO:-guccyon/factory-engine}"
ENGINE_REF="${ENGINE_REF:-main}"
cat > /work/case_input.json
echo "[case_review] job=${JOB_ID} engine=${ENGINE_REPO}@${ENGINE_REF} input_bytes=$(wc -c < /work/case_input.json)"
git config --global credential.helper '!f() { echo "username=x-access-token"; echo "password=${GH_TOKEN}"; }; f'
git clone --quiet --depth 1 --branch "${ENGINE_REF}" "https://github.com/${ENGINE_REPO}.git" /work/engine
set +e
python3 /work/engine/core-gates/case_review.py --input /work/case_input.json --out /work/case.json 2>/work/case.err
RC=$?
set -e
sed 's/^/[case_review.err] /' /work/case.err | tail -20 || true
if [ "${RC}" -ne 0 ] || [ ! -s /work/case.json ]; then
  echo "FACTORY_RESULT:$(jq -nc --arg rc "${RC}" '{kind:"case_review", ok:false, exit:($rc|tonumber)}')"
  exit 1
fi
echo "FACTORY_RESULT:$(jq -c '{kind:"case_review", ok:true, report:.result, cost_usd:(.cost_usd // 0), usage:(.usage // {}), model:.model, num_turns:.num_turns}' /work/case.json)"
exit 0
