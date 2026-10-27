#!/usr/bin/env bash
# judge のジョブ（ADR 0006 後半）: 標準入力の JSON（制御プレーンが組み立てた仮決めと指摘）を Engine の core-gates/judge.py に渡し、
# 裁きの結果を最終行に JSON で出す。GitHub には書かない（起票やラベルは dispatcher が結果を見て行う）。
# 環境変数: JOB_ID REPO PR_URL GH_TOKEN CLAUDE_CODE_OAUTH_TOKEN
set -euo pipefail

: "${JOB_ID:?}" "${REPO:?}" "${PR_URL:?}" "${GH_TOKEN:?}" "${CLAUDE_CODE_OAUTH_TOKEN:?}"
if ! [[ "${PR_URL}" =~ ^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[0-9]+$ ]]; then
  echo "[judge] not a PR url"; exit 2
fi
ENGINE_REPO="${ENGINE_REPO:-guccyon/factory-engine}"
ENGINE_REF="${ENGINE_REF:-main}"
cat > /work/input.json
echo "[judge] job=${JOB_ID} pr=${PR_URL} engine=${ENGINE_REPO}@${ENGINE_REF} input=$(wc -c < /work/input.json)B"
git config --global credential.helper '!f() { echo "username=x-access-token"; echo "password=${GH_TOKEN}"; }; f'
git clone --quiet --depth 1 --branch "${ENGINE_REF}" "https://github.com/${ENGINE_REPO}.git" /work/engine
set +e
python3 /work/engine/core-gates/judge.py < /work/input.json > /work/judge.json 2>/work/judge.err
RC=$?
set -e
sed 's/^/[judge.err] /' /work/judge.err | tail -20 || true
if [ "${RC}" -ne 0 ] || [ ! -s /work/judge.json ]; then
  echo "FACTORY_RESULT:$(jq -nc --arg pr "${PR_URL}" --arg rc "${RC}" '{kind:"judge", pr_url:$pr, ok:false, exit:($rc|tonumber)}')"
  exit 1
fi
echo "FACTORY_RESULT:$(jq -c '{kind:"judge", pr_url:.pr_url, ok:true, verdicts:.result, cost_usd:(.cost_usd // 0), usage:(.usage // {}), model:.model}' /work/judge.json)"
exit 0
