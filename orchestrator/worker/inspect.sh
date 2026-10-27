#!/usr/bin/env bash
# 検査ジョブ（ADR 0004 の「検証の起動」）: 工場の PR に L4 の shadow 判定をかけ、結果を最終行に JSON で出す。
# GitHub には書かない（shadow）。判定器は Engine の core-gates/shadow_review.py を毎回 main から取って使う。
# 環境変数: JOB_ID REPO PR_URL GH_TOKEN CLAUDE_CODE_OAUTH_TOKEN（PR_URL は制御プレーンが自分の記録から作ったもの）
set -euo pipefail

: "${JOB_ID:?}" "${REPO:?}" "${PR_URL:?}" "${GH_TOKEN:?}" "${CLAUDE_CODE_OAUTH_TOKEN:?}"
if ! [[ "${PR_URL}" =~ ^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[0-9]+$ ]]; then
  echo "[inspect] not a PR url"; exit 2
fi
ENGINE_REPO="${ENGINE_REPO:-guccyon/factory-engine}"
ENGINE_REF="${ENGINE_REF:-main}"
echo "[inspect] job=${JOB_ID} pr=${PR_URL} engine=${ENGINE_REPO}@${ENGINE_REF}"

git clone --quiet --depth 1 --branch "${ENGINE_REF}" "https://github.com/${ENGINE_REPO}.git" /work/engine
set +e
python3 /work/engine/core-gates/shadow_review.py --pr "${PR_URL}" --out /work/shadow.json 2>/work/inspect.err
RC=$?
set -e
sed 's/^/[inspect.err] /' /work/inspect.err | tail -20 || true
if [ "${RC}" -ne 0 ] || [ ! -s /work/shadow.json ]; then
  echo "FACTORY_RESULT:$(jq -nc --arg pr "${PR_URL}" --arg rc "${RC}" '{kind:"inspect", pr_url:$pr, ok:false, exit:($rc|tonumber)}')"
  exit 1
fi
echo "FACTORY_RESULT:$(jq -c --arg pr "${PR_URL}" '{kind:"inspect", pr_url:$pr, ok:true, report:.[0], cost_usd:(.[0].cost_usd // 0), usage:(.[0].usage // {}), model:.[0].model}' /work/shadow.json)"
exit 0
