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

git config --global credential.helper '!f() { echo "username=x-access-token"; echo "password=${GH_TOKEN}"; }; f'
/usr/local/bin/clone_engine.sh "${ENGINE_REPO}" "${ENGINE_REF}"

# この製品の知見（ADR 0040 の決定 7 の 5）: 業務は work.yaml の from_agent_steps.inspect。製品の既定ブランチの .factory-config だけを取り、
# core-gates/knowledge_context.py（決定論）で選ぶ。触ったファイルを軸 files に渡す。取れなくても検査は続ける。KNOWLEDGE_CONTEXT=0 で止める
KNOWLEDGE_IDS='[]'
KNOWLEDGE_SEL='{}'
export KNOWLEDGE_FILE=""
REPO_OF_PR=$(sed -E 's#https://github.com/([^/]+/[^/]+)/pull/.*#\1#' <<<"${PR_URL}")
if [ "${KNOWLEDGE_CONTEXT:-1}" != "0" ] \
   && git clone --quiet --depth 1 --filter=blob:none --sparse "https://github.com/${REPO_OF_PR}.git" /work/kprod 2>/dev/null \
   && git -C /work/kprod sparse-checkout set .factory-config 2>/dev/null \
   && [ -d /work/kprod/.factory-config/knowledge ]; then
  FILES=$(gh pr view "${PR_URL}" --json files -q '[.files[].path] | .[:200] | join(",")' 2>/dev/null || echo "")
  KX=$(python3 /work/engine/core-gates/knowledge_context.py --repo /work/kprod --role inspect --set request_form=ticket ${FILES:+--files "${FILES}"} --out /work/knowledge.md 2>/dev/null || echo '{}')
  KNOWLEDGE_IDS=$(jq -c '.ids // []' <<<"${KX}" 2>/dev/null || echo '[]')
  [ -z "${KNOWLEDGE_IDS}" ] && KNOWLEDGE_IDS='[]'
  KNOWLEDGE_SEL=$(jq -c '{work: (.work // []), values: ((.values // {}) | del(.files)), fit: (.fit // {})}' <<<"${KX}" 2>/dev/null || echo '{}')
  [ -z "${KNOWLEDGE_SEL}" ] && KNOWLEDGE_SEL='{}'
  if [ "${KNOWLEDGE_IDS}" != "[]" ] && [ -s /work/knowledge.md ]; then KNOWLEDGE_FILE=/work/knowledge.md; fi
fi
echo "[inspect] knowledge context ids=${KNOWLEDGE_IDS}"
set +e
python3 /work/engine/core-gates/shadow_review.py --pr "${PR_URL}" --out /work/shadow.json 2>/work/inspect.err
RC=$?
set -e
sed 's/^/[inspect.err] /' /work/inspect.err | tail -20 || true
if [ "${RC}" -ne 0 ] || [ ! -s /work/shadow.json ]; then
  echo "FACTORY_RESULT:$(jq -nc --arg pr "${PR_URL}" --arg rc "${RC}" '{kind:"inspect", pr_url:$pr, ok:false, exit:($rc|tonumber)}')"
  exit 1
fi
echo "FACTORY_RESULT:$(jq -c --arg pr "${PR_URL}" --argjson kids "${KNOWLEDGE_IDS}" --argjson ksel "${KNOWLEDGE_SEL}" '{kind:"inspect", pr_url:$pr, ok:true, report:.[0], cost_usd:(.[0].cost_usd // 0), usage:(.[0].usage // {}), model:.[0].model, knowledge_ids:$kids, knowledge_selection:$ksel}' /work/shadow.json)"
exit 0
