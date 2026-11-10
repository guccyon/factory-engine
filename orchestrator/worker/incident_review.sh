#!/usr/bin/env bash
# 事故の調査委員会の週次の見回り（ADR 0046）: 標準入力のその週の記録を Engine の core-gates/incident_review.py に読ませ、結果を最終行に JSON で出す。
# 製品のリポジトリは読まない・書かない（台帳・送り出し口への記録と Engine の改善 Issue の起票は dispatcher と supervisor が結果を見て行う）。
# 標準入力: その週の記録（制御プレーンが組み立てた JSON）
# 環境変数: JOB_ID GH_TOKEN CLAUDE_CODE_OAUTH_TOKEN
set -euo pipefail

: "${JOB_ID:?}" "${GH_TOKEN:?}" "${CLAUDE_CODE_OAUTH_TOKEN:?}"
ENGINE_REPO="${ENGINE_REPO:-guccyon/factory-engine}"
ENGINE_REF="${ENGINE_REF:-main}"
cat > /work/week.json
echo "[incident_review] job=${JOB_ID} engine=${ENGINE_REPO}@${ENGINE_REF} input_bytes=$(wc -c < /work/week.json)"
git config --global credential.helper '!f() { echo "username=x-access-token"; echo "password=${GH_TOKEN}"; }; f'
git clone --quiet --depth 1 --branch "${ENGINE_REF}" "https://github.com/${ENGINE_REPO}.git" /work/engine
set +e
python3 /work/engine/core-gates/incident_review.py --input /work/week.json --out /work/review.json --engine-dir /work/engine 2>/work/review.err
RC=$?
set -e
sed 's/^/[incident_review.err] /' /work/review.err | tail -20 || true
if [ "${RC}" -ne 0 ] || [ ! -s /work/review.json ]; then
  echo "FACTORY_RESULT:$(jq -nc --arg rc "${RC}" '{kind:"incident_review", ok:false, exit:($rc|tonumber)}')"
  exit 1
fi
echo "FACTORY_RESULT:$(jq -c '{kind:"incident_review", ok:true, facts:.facts, report:.result, drafts:.drafts, cost_usd:(.cost_usd // 0), usage:(.usage // {}), model:.model}' /work/review.json)"
exit 0
