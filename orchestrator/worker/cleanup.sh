#!/usr/bin/env bash
# 後片付けのジョブ（ADR 0018）: チケットのボリューム（読み取りのみ）と標準入力の JSON（制御プレーンの記録）を Engine の core-gates/cleanup.py に渡し、
# 申し送りと再起票の案を出す。結果を最終行に JSON で出す。記録の永続化とボリュームの破棄は dispatcher が行う。
# 環境変数: JOB_ID REPO ISSUE_NUMBER HANDOFF_DIR GH_TOKEN CLAUDE_CODE_OAUTH_TOKEN
set -euo pipefail

: "${JOB_ID:?}" "${REPO:?}" "${ISSUE_NUMBER:?}" "${CLAUDE_CODE_OAUTH_TOKEN:?}"
export HANDOFF_DIR="${HANDOFF_DIR:-/handoff}"
ENGINE_REPO="${ENGINE_REPO:-guccyon/factory-engine}"
ENGINE_REF="${ENGINE_REF:-main}"
cat > /work/input.json
echo "[cleanup] job=${JOB_ID} repo=${REPO} issue=#${ISSUE_NUMBER} engine=${ENGINE_REPO}@${ENGINE_REF} input=$(wc -c < /work/input.json)B"
git clone --quiet --depth 1 --branch "${ENGINE_REF}" "https://github.com/${ENGINE_REPO}.git" /work/engine
set +e
python3 /work/engine/core-gates/cleanup.py < /work/input.json > /work/out.json 2>/work/out.err
RC=$?
set -e
sed 's/^/[out.err] /' /work/out.err | tail -20 || true
if [ "${RC}" -ne 0 ] || [ ! -s /work/out.json ]; then
  echo "FACTORY_RESULT:$(jq -nc --arg rc "${RC}" '{kind:"cleanup", ok:false, exit:($rc|tonumber)}')"
  exit 1
fi
echo "FACTORY_RESULT:$(jq -c '{kind:"cleanup", pr_url:.pr_url, ok:true, report:.report, cost_usd:(.cost_usd // 0), usage:(.usage // {}), model:.model}' /work/out.json)"
exit 0
