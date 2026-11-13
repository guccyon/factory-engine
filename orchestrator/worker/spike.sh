#!/usr/bin/env bash
# スパイクのレーンのジョブ（ADR 0055。Gen1 のプロトタイプ）: 製品の main を取り、Issue の本文とコメントと、dispatcher が渡した材料
# （スパイクで決めること・開いている QA の不具合）を Engine の core-gates/spike.py（System Architect）に見せて、設計書を最終行に JSON で出す。
# 製品のリポジトリにも Issue にも何も書かない（コメント・ラベル・束ねは dispatcher が結果を見て行う）。
# 標準入力: dispatcher が組み立てた JSON（questions・open_defects・defect_bodies）
# 環境変数: JOB_ID REPO ISSUE_NUMBER GH_TOKEN CLAUDE_CODE_OAUTH_TOKEN
set -euo pipefail

: "${JOB_ID:?}" "${REPO:?}" "${ISSUE_NUMBER:?}" "${GH_TOKEN:?}" "${CLAUDE_CODE_OAUTH_TOKEN:?}"
if ! [[ "${REPO}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || ! [[ "${ISSUE_NUMBER}" =~ ^[0-9]+$ ]]; then
  echo "[spike] bad repo or issue"; exit 2
fi
ENGINE_REPO="${ENGINE_REPO:-guccyon/factory-engine}"
ENGINE_REF="${ENGINE_REF:-main}"
cat > /work/input.json
echo "[spike] job=${JOB_ID} repo=${REPO}#${ISSUE_NUMBER} engine=${ENGINE_REPO}@${ENGINE_REF}"
git config --global credential.helper '!f() { echo "username=x-access-token"; echo "password=${GH_TOKEN}"; }; f'
git clone --quiet --depth 1 --branch "${ENGINE_REF}" "https://github.com/${ENGINE_REPO}.git" /work/engine
DEFAULT_BRANCH=$(gh repo view "${REPO}" --json defaultBranchRef -q .defaultBranchRef.name 2>/dev/null || echo main)
git clone --quiet --depth 1 --branch "${DEFAULT_BRANCH}" "https://github.com/${REPO}.git" /work/product
gh issue view "${ISSUE_NUMBER}" -R "${REPO}" --json number,title,body,comments \
  | jq '{number, title, body, comments: [.comments[] | {createdAt, body}]}' > /work/issue.json
set +e
python3 /work/engine/core-gates/spike.py --issue /work/issue.json --input /work/input.json --repo-dir /work/product --out /work/spike.json 2>/work/spike.err
RC=$?
set -e
sed 's/^/[spike.err] /' /work/spike.err | tail -20 || true
if [ "${RC}" -ne 0 ] || [ ! -s /work/spike.json ]; then
  echo "FACTORY_RESULT:$(jq -nc --arg rc "${RC}" '{kind:"spike", ok:false, exit:($rc|tonumber)}')"
  exit 1
fi
echo "FACTORY_RESULT:$(jq -c '{kind:"spike", ok:true, designdoc, versions, cost_usd:(.cost_usd // 0), usage:(.usage // {}), model, num_turns}' /work/spike.json)"
exit 0
