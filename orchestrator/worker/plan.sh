#!/usr/bin/env bash
# プランナーのジョブ（ADR 0025 のプランナーの節、ADR 0026 の着手判定）: 製品の main を浅く取り、Issue の本文とコメントを
# Engine の core-gates/plan.py に見せて、着手判定（ready / ask / escalate）と経路を最終行に JSON で出す。
# 製品のリポジトリにも Issue にも何も書かない（ラベルと問いのコメントは dispatcher が結果を見て付ける）。
# 標準入力: dispatcher が組み立てた JSON（判定し直しのときは round。最初は {}）
# 環境変数: JOB_ID REPO ISSUE_NUMBER GH_TOKEN CLAUDE_CODE_OAUTH_TOKEN
set -euo pipefail

: "${JOB_ID:?}" "${REPO:?}" "${ISSUE_NUMBER:?}" "${GH_TOKEN:?}" "${CLAUDE_CODE_OAUTH_TOKEN:?}"
if ! [[ "${REPO}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || ! [[ "${ISSUE_NUMBER}" =~ ^[0-9]+$ ]]; then
  echo "[plan] bad repo or issue"; exit 2
fi
ENGINE_REPO="${ENGINE_REPO:-guccyon/factory-engine}"
ENGINE_REF="${ENGINE_REF:-main}"
cat > /work/input.json
echo "[plan] job=${JOB_ID} repo=${REPO}#${ISSUE_NUMBER} engine=${ENGINE_REPO}@${ENGINE_REF}"
git config --global credential.helper '!f() { echo "username=x-access-token"; echo "password=${GH_TOKEN}"; }; f'
git clone --quiet --depth 1 --branch "${ENGINE_REF}" "https://github.com/${ENGINE_REPO}.git" /work/engine
DEFAULT_BRANCH=$(gh repo view "${REPO}" --json defaultBranchRef -q .defaultBranchRef.name 2>/dev/null || echo main)
git clone --quiet --depth 1 --branch "${DEFAULT_BRANCH}" "https://github.com/${REPO}.git" /work/product
gh issue view "${ISSUE_NUMBER}" -R "${REPO}" --json number,title,body,comments \
  | jq '{number, title, body, comments: [.comments[] | {createdAt, body}]}' > /work/issue.json
set +e
python3 /work/engine/core-gates/plan.py --issue /work/issue.json --input /work/input.json --repo-dir /work/product --out /work/plan.json 2>/work/plan.err
RC=$?
set -e
sed 's/^/[plan.err] /' /work/plan.err | tail -20 || true
if [ "${RC}" -ne 0 ] || [ ! -s /work/plan.json ]; then
  echo "FACTORY_RESULT:$(jq -nc --arg rc "${RC}" '{kind:"plan", ok:false, exit:($rc|tonumber)}')"
  exit 1
fi
echo "FACTORY_RESULT:$(jq -c '{kind:"plan", ok:true, decision, route, notes, round, facts, plan, cost_usd:(.cost_usd // 0), usage:(.usage // {}), model, num_turns}' /work/plan.json)"
exit 0
