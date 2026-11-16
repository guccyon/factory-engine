#!/usr/bin/env bash
# 知見の委員会の週 1 の定例のジョブ（ADR 0024）: 製品の既定ブランチを浅く取り、Engine の core-gates/retro.py に見直させて、結果を最終行に JSON で出す。
# 製品のリポジトリには何も書かない（議事録の記録・固定 Issue への書き込み・Issue の起票は dispatcher が結果を見て行う）。
# 標準入力: 振り返りの議題（制御プレーンが組み立てた JSON。retro.build_input）
# 環境変数: JOB_ID REPO GH_TOKEN CLAUDE_CODE_OAUTH_TOKEN
set -euo pipefail

: "${JOB_ID:?}" "${REPO:?}" "${GH_TOKEN:?}" "${CLAUDE_CODE_OAUTH_TOKEN:?}"
if ! [[ "${REPO}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
  echo "[retro] not a repo"; exit 2
fi
ENGINE_REPO="${ENGINE_REPO:-guccyon/factory-engine}"
ENGINE_REF="${ENGINE_REF:-main}"
cat > /work/retro_in.json
echo "[retro] job=${JOB_ID} repo=${REPO} engine=${ENGINE_REPO}@${ENGINE_REF}"
git config --global credential.helper '!f() { echo "username=x-access-token"; echo "password=${GH_TOKEN}"; }; f'
/usr/local/bin/clone_engine.sh "${ENGINE_REPO}" "${ENGINE_REF}"
DEFAULT_BRANCH=$(gh repo view "${REPO}" --json defaultBranchRef -q .defaultBranchRef.name 2>/dev/null || echo main)
git clone --quiet --depth 1 --branch "${DEFAULT_BRANCH}" "https://github.com/${REPO}.git" /work/product
set +e
python3 /work/engine/core-gates/retro.py --repo-dir /work/product --input /work/retro_in.json --out /work/retro_out.json 2>/work/retro.err
RC=$?
set -e
sed 's/^/[retro.err] /' /work/retro.err | tail -20 || true
if [ "${RC}" -ne 0 ] || [ ! -s /work/retro_out.json ]; then
  echo "FACTORY_RESULT:$(jq -nc --arg repo "${REPO}" --arg rc "${RC}" '{kind:"retro", repo:$repo, ok:false, exit:($rc|tonumber)}')"
  exit 1
fi
echo "FACTORY_RESULT:$(jq -c --arg repo "${REPO}" '{kind:"retro", repo:$repo, ok:true, facts:.facts, opinions:.opinions, failed_roles:.failed_roles, lead:.lead, cost_usd:(.cost_usd // 0), usage:(.usage // {}), model:.model}' /work/retro_out.json)"
exit 0
