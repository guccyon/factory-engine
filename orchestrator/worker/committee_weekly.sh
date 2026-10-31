#!/usr/bin/env bash
# 知見の委員会の週 1 の定例のジョブ（ADR 0024）: 製品の既定ブランチを浅く取り、Engine の core-gates/committee_weekly.py に見直させて、結果を最終行に JSON で出す。
# 製品のリポジトリには何も書かない（固定 Issue への書き込みと Issue の起票は dispatcher が結果を見て行う）。
# 標準入力: その週の記録（制御プレーンが組み立てた JSON）
# 環境変数: JOB_ID REPO GH_TOKEN CLAUDE_CODE_OAUTH_TOKEN
set -euo pipefail

: "${JOB_ID:?}" "${REPO:?}" "${GH_TOKEN:?}" "${CLAUDE_CODE_OAUTH_TOKEN:?}"
if ! [[ "${REPO}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
  echo "[committee_weekly] not a repo"; exit 2
fi
ENGINE_REPO="${ENGINE_REPO:-guccyon/factory-engine}"
ENGINE_REF="${ENGINE_REF:-main}"
cat > /work/week.json
echo "[committee_weekly] job=${JOB_ID} repo=${REPO} engine=${ENGINE_REPO}@${ENGINE_REF}"
git config --global credential.helper '!f() { echo "username=x-access-token"; echo "password=${GH_TOKEN}"; }; f'
git clone --quiet --depth 1 --branch "${ENGINE_REF}" "https://github.com/${ENGINE_REPO}.git" /work/engine
DEFAULT_BRANCH=$(gh repo view "${REPO}" --json defaultBranchRef -q .defaultBranchRef.name 2>/dev/null || echo main)
git clone --quiet --depth 1 --branch "${DEFAULT_BRANCH}" "https://github.com/${REPO}.git" /work/product
set +e
python3 /work/engine/core-gates/committee_weekly.py --repo-dir /work/product --input /work/week.json --out /work/weekly.json 2>/work/weekly.err
RC=$?
set -e
sed 's/^/[committee_weekly.err] /' /work/weekly.err | tail -20 || true
if [ "${RC}" -ne 0 ] || [ ! -s /work/weekly.json ]; then
  echo "FACTORY_RESULT:$(jq -nc --arg repo "${REPO}" --arg rc "${RC}" '{kind:"committee_weekly", repo:$repo, ok:false, exit:($rc|tonumber)}')"
  exit 1
fi
echo "FACTORY_RESULT:$(jq -c --arg repo "${REPO}" '{kind:"committee_weekly", repo:$repo, ok:true, facts:.facts, report:.result, cost_usd:(.cost_usd // 0), usage:(.usage // {}), model:.model}' /work/weekly.json)"
exit 0
