#!/usr/bin/env bash
# 窓口の相談役のジョブ（ADR 0030 の 3）: 製品の main を浅く取り、開いている Issue の一覧と、dispatcher が組み立てた会話（標準入力）を
# Engine の core-gates/desk_consult.py に見せて、依頼者への問いか、Request とタスクのまとまりの下書きを最終行に JSON で出す。
# 製品のリポジトリにも Issue にも何も書かない（会話への書き込み・起票は dispatcher が結果を見て行う）。
# 標準入力: dispatcher（desk_line.consult_input）が組み立てた JSON。依頼者の言葉はデータとして渡す
# 環境変数: JOB_ID REPO GH_TOKEN CLAUDE_CODE_OAUTH_TOKEN
set -euo pipefail

: "${JOB_ID:?}" "${REPO:?}" "${GH_TOKEN:?}" "${CLAUDE_CODE_OAUTH_TOKEN:?}"
if ! [[ "${REPO}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
  echo "[desk] bad repo"; exit 2
fi
ENGINE_REPO="${ENGINE_REPO:-guccyon/factory-engine}"
ENGINE_REF="${ENGINE_REF:-main}"
cat > /work/input.json
echo "[desk] job=${JOB_ID} repo=${REPO} conversation=$(jq -r .conversation_id /work/input.json) engine=${ENGINE_REPO}@${ENGINE_REF}"
git config --global credential.helper '!f() { echo "username=x-access-token"; echo "password=${GH_TOKEN}"; }; f'
git clone --quiet --depth 1 --branch "${ENGINE_REF}" "https://github.com/${ENGINE_REPO}.git" /work/engine
DEFAULT_BRANCH=$(gh repo view "${REPO}" --json defaultBranchRef -q .defaultBranchRef.name 2>/dev/null || echo main)
git clone --quiet --depth 1 --branch "${DEFAULT_BRANCH}" "https://github.com/${REPO}.git" /work/product
# 開いている Issue（PR を除く）。重なりと依存を見つける材料
gh api --paginate "repos/${REPO}/issues?state=open&per_page=100" \
  | jq -s '[.[][] | select(.pull_request | not) | {number, title, labels: [.labels[].name]}]' > /work/issues.json || echo '[]' > /work/issues.json
set +e
python3 /work/engine/core-gates/desk_consult.py --input /work/input.json --repo-dir /work/product --issues /work/issues.json --out /work/out.json 2>/work/desk.err
RC=$?
set -e
sed 's/^/[desk.err] /' /work/desk.err | tail -20 || true
if [ "${RC}" -ne 0 ] || [ ! -s /work/out.json ]; then
  echo "FACTORY_RESULT:$(jq -nc --arg rc "${RC}" '{kind:"desk_consult", ok:false, exit:($rc|tonumber)}')"
  exit 1
fi
echo "FACTORY_RESULT:$(jq -c '{kind:"desk_consult", ok:true} + .' /work/out.json)"
exit 0
