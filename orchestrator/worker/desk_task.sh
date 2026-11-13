#!/usr/bin/env bash
# 窓口の依頼の実行役のジョブ（ADR 0037 の決定 5 の quick、ADR 0039 の決定 2a）: 製品の main を浅く取り、dispatcher が組み立てた依頼と工場の記録の要約（標準入力）を
# Engine の core-gates/desk_task.py に見せて、その場で答え・文面・表・見立てを作り、最終行に JSON で出す。
# 製品のリポジトリにも Issue にも何も書かない（会話への提出は dispatcher が結果を見て行う）。
# 実行役のエージェントには GitHub のトークンも DB の接続情報も渡さない。工場の記録と製品の Issue・PR は dispatcher の中継の道具で読む。
# 標準入力: dispatcher（desk_task.task_input）が組み立てた JSON。依頼者の言葉はデータとして渡す
# 環境変数: JOB_ID REPO GH_TOKEN CLAUDE_CODE_OAUTH_TOKEN（RUNNER_RELAY_URL RUNNER_RELAY_TOKEN は中継があれば）
set -euo pipefail

: "${JOB_ID:?}" "${REPO:?}" "${GH_TOKEN:?}" "${CLAUDE_CODE_OAUTH_TOKEN:?}"
if ! [[ "${REPO}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
  echo "[desk.task] bad repo"; exit 2
fi
ENGINE_REPO="${ENGINE_REPO:-guccyon/factory-engine}"
ENGINE_REF="${ENGINE_REF:-main}"
cat > /work/input.json
echo "[desk.task] job=${JOB_ID} repo=${REPO} conversation=$(jq -r .conversation_id /work/input.json) path=$(jq -r .path /work/input.json) engine=${ENGINE_REPO}@${ENGINE_REF}"
git config --global credential.helper '!f() { echo "username=x-access-token"; echo "password=${GH_TOKEN}"; }; f'
git clone --quiet --depth 1 --branch "${ENGINE_REF}" "https://github.com/${ENGINE_REPO}.git" /work/engine
DEFAULT_BRANCH=$(gh repo view "${REPO}" --json defaultBranchRef -q .defaultBranchRef.name 2>/dev/null || echo main)
git clone --quiet --depth 1 --branch "${DEFAULT_BRANCH}" "https://github.com/${REPO}.git" /work/product
# 取り終えたら、実行役の手の届くところに GitHub のトークンを残さない
git config --global --unset credential.helper || true
unset GH_TOKEN GITHUB_TOKEN DATABASE_URL || true
set +e
python3 /work/engine/core-gates/desk_task.py --input /work/input.json --repo-dir /work/product --out /work/out.json 2>/work/task.err
RC=$?
set -e
sed 's/^/[task.err] /' /work/task.err | tail -20 || true
if [ "${RC}" -ne 0 ] || [ ! -s /work/out.json ]; then
  echo "FACTORY_RESULT:$(jq -nc --arg rc "${RC}" '{kind:"desk_task", ok:false, exit:($rc|tonumber)}')"
  exit 1
fi
echo "FACTORY_RESULT:$(jq -c '{kind:"desk_task"} + .' /work/out.json)"
exit 0
