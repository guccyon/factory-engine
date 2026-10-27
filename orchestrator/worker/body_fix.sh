#!/usr/bin/env bash
# 本文の追記のジョブ: 標準入力の JSON（judge が採用した PR 本文だけの指摘）を Engine の core-gates/body_fix.py に渡し、
# PR の本文の末尾に節を足す。結果を最終行に JSON で出す。
# 環境変数: JOB_ID REPO PR_URL GH_TOKEN CLAUDE_CODE_OAUTH_TOKEN
set -euo pipefail

: "${JOB_ID:?}" "${REPO:?}" "${PR_URL:?}" "${GH_TOKEN:?}" "${CLAUDE_CODE_OAUTH_TOKEN:?}"
if ! [[ "${PR_URL}" =~ ^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[0-9]+$ ]]; then
  echo "[body_fix] not a PR url"; exit 2
fi
ENGINE_REPO="${ENGINE_REPO:-guccyon/factory-engine}"
ENGINE_REF="${ENGINE_REF:-main}"
cat > /work/input.json
echo "[body_fix] job=${JOB_ID} pr=${PR_URL} engine=${ENGINE_REPO}@${ENGINE_REF} input=$(wc -c < /work/input.json)B"
git config --global credential.helper '!f() { echo "username=x-access-token"; echo "password=${GH_TOKEN}"; }; f'
git clone --quiet --depth 1 --branch "${ENGINE_REF}" "https://github.com/${ENGINE_REPO}.git" /work/engine
set +e
python3 /work/engine/core-gates/body_fix.py < /work/input.json > /work/out.json 2>/work/out.err
RC=$?
set -e
sed 's/^/[out.err] /' /work/out.err | tail -20 || true
if [ "${RC}" -ne 0 ] || [ ! -s /work/out.json ]; then
  echo "FACTORY_RESULT:$(jq -nc --arg pr "${PR_URL}" --arg rc "${RC}" '{kind:"body_fix", pr_url:$pr, ok:false, exit:($rc|tonumber)}')"
  exit 1
fi
echo "FACTORY_RESULT:$(jq -c '{kind:"body_fix", pr_url:.pr_url, ok:true, chars:.chars, summary:.summary, cost_usd:(.cost_usd // 0), usage:(.usage // {}), model:.model}' /work/out.json)"
exit 0
