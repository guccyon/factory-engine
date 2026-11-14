#!/usr/bin/env bash
# 本文の直しのジョブ: 標準入力の JSON（judge が採用した PR 本文だけの指摘）を Engine の core-gates/body_fix.py に渡し、
# PR の本文の説明の部分を作り直す（守る節はそのまま）。結果を最終行に JSON で出す。
# 環境変数: JOB_ID REPO PR_URL GH_TOKEN CLAUDE_CODE_OAUTH_TOKEN（任意: KNOWLEDGE_CONTEXT=0 で知見を渡さない）
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
/usr/local/bin/clone_engine.sh "${ENGINE_REPO}" "${ENGINE_REF}"

# この製品の知見（ADR 0040 の決定 7 の 2）: 本文の直しは業務 dev.describe（work.yaml の from_agent_steps.body_fix）。
# 製品の .factory-config だけを取り、core-gates/knowledge_context.py（決定論）で選ぶ。取れなくても直しは続ける。KNOWLEDGE_CONTEXT=0 で止める
KNOWLEDGE_IDS='[]'
KNOWLEDGE_SEL='{}'
export KNOWLEDGE_FILE=""
if [ "${KNOWLEDGE_CONTEXT:-1}" != "0" ] \
   && git clone --quiet --depth 1 --filter=blob:none --sparse "https://github.com/${REPO}.git" /work/product 2>/dev/null \
   && git -C /work/product sparse-checkout set .factory-config 2>/dev/null \
   && [ -d /work/product/.factory-config/knowledge ]; then
  KX=$(python3 /work/engine/core-gates/knowledge_context.py --repo /work/product --role body_fix --set request_form=ticket --out /work/knowledge.md 2>/dev/null || echo '{}')
  KNOWLEDGE_IDS=$(jq -c '.ids // []' <<<"${KX}" 2>/dev/null || echo '[]')
  [ -z "${KNOWLEDGE_IDS}" ] && KNOWLEDGE_IDS='[]'
  KNOWLEDGE_SEL=$(jq -c '{work: (.work // []), values: (.values // {}), fit: (.fit // {})}' <<<"${KX}" 2>/dev/null || echo '{}')
  [ -z "${KNOWLEDGE_SEL}" ] && KNOWLEDGE_SEL='{}'
  if [ "${KNOWLEDGE_IDS}" != "[]" ] && [ -s /work/knowledge.md ]; then KNOWLEDGE_FILE=/work/knowledge.md; fi
fi
echo "[body_fix] knowledge context ids=${KNOWLEDGE_IDS}"
set +e
python3 /work/engine/core-gates/body_fix.py < /work/input.json > /work/out.json 2>/work/out.err
RC=$?
set -e
sed 's/^/[out.err] /' /work/out.err | tail -20 || true
if [ "${RC}" -ne 0 ] || [ ! -s /work/out.json ]; then
  # モデルを呼んだあとの失敗は body_fix.py が費用を out.json に残す。失敗でも費用を帳簿に載せる（2026-11-10）
  FAIL_EXTRA='{}'
  if [ -s /work/out.json ]; then FAIL_EXTRA=$(jq -c '{cost_usd:(.cost_usd // null), usage:(.usage // {}), model:(.model // null), error:(.error // null)}' /work/out.json 2>/dev/null || echo '{}'); fi
  [ -z "${FAIL_EXTRA}" ] && FAIL_EXTRA='{}'
  echo "FACTORY_RESULT:$(jq -nc --arg pr "${PR_URL}" --arg rc "${RC}" --argjson x "${FAIL_EXTRA}" '{kind:"body_fix", pr_url:$pr, ok:false, exit:($rc|tonumber)} + $x')"
  exit 1
fi
echo "FACTORY_RESULT:$(jq -c --argjson kids "${KNOWLEDGE_IDS}" --argjson ksel "${KNOWLEDGE_SEL}" '{kind:"body_fix", pr_url:.pr_url, ok:true, chars:.chars, chars_before:.chars_before, chars_after:.chars_after, skipped:(.skipped // null), summary:.summary, cost_usd:(.cost_usd // 0), usage:(.usage // {}), model:.model, knowledge_ids:$kids, knowledge_selection:$ksel}' /work/out.json)"
exit 0
