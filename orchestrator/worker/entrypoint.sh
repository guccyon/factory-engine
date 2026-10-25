#!/usr/bin/env bash
# ワーカーの手順: 浅いクローン → Claude Code → commit / push → PR → 結果を最終行に JSON で出す。
# 環境変数: JOB_ID REPO ISSUE_NUMBER MODEL_TIER MAX_TURNS GH_TOKEN CLAUDE_CODE_OAUTH_TOKEN
# 権限は PR 作成まで（P0 autonomy）。マージしない。コンテナは --rm なので終了時に何も残らない。
set -euo pipefail

: "${JOB_ID:?}" "${REPO:?}" "${ISSUE_NUMBER:?}"

# 試験運転: GitHub にも Claude にも触らず、停止・再実行・二重実行抑止の確認に使う
if [ -n "${WORKER_DRY_RUN:-}" ]; then
  echo "[worker] dry run job=${JOB_ID} repo=${REPO} issue=#${ISSUE_NUMBER} (sleep ${WORKER_DRY_RUN}s)"
  for i in $(seq 1 "${WORKER_DRY_RUN}"); do echo "[worker] tick ${i}"; sleep 1; done
  echo "FACTORY_RESULT:{\"pr_url\":\"https://example.invalid/${REPO}/pull/0\",\"pr_number\":0,\"usage\":{\"input_tokens\":10,\"output_tokens\":5},\"cost_usd\":0,\"model\":\"dry-run\",\"num_turns\":0,\"change_types\":[\"docs_only\"]}"
  exit 0
fi

: "${GH_TOKEN:?}" "${CLAUDE_CODE_OAUTH_TOKEN:?}"
MAX_TURNS="${MAX_TURNS:-40}"
case "${MODEL_TIER:-tier-large}" in
  tier-small) MODEL="claude-sonnet-5-5" ;;
  tier-judge) MODEL="claude-opus-5-5" ;;
  *)          MODEL="claude-opus-5-5" ;;
esac
BRANCH="factory/issue-${ISSUE_NUMBER}-job-${JOB_ID}"
export GIT_TERMINAL_PROMPT=0

echo "[worker] job=${JOB_ID} repo=${REPO} issue=#${ISSUE_NUMBER} model=${MODEL} max_turns=${MAX_TURNS}"
git config --global user.name "factory-worker"
git config --global user.email "factory-worker@users.noreply.github.com"
git config --global credential.helper '!f() { echo "username=x-access-token"; echo "password=${GH_TOKEN}"; }; f'

DEFAULT_BRANCH=$(gh api "repos/${REPO}" --jq .default_branch)
git clone --quiet --depth 1 --branch "${DEFAULT_BRANCH}" "https://github.com/${REPO}.git" repo
cd repo
git checkout -q -b "${BRANCH}"

ISSUE_JSON=$(gh issue view "${ISSUE_NUMBER}" -R "${REPO}" --json title,body,labels)
TITLE=$(jq -r .title <<<"${ISSUE_JSON}")
BODY=$(jq -r '.body // ""' <<<"${ISSUE_JSON}")

PROMPT=$(cat <<EOF
あなたは工場のヘッドレスワーカーです。このリポジトリで GitHub Issue #${ISSUE_NUMBER} を実装してください。

# Issue: ${TITLE}

${BODY}

# 約束
- リポジトリの CLAUDE.md や README の規約に従う。コード・テスト・文書は同じ変更に含める（三位一体）
- Issue の範囲だけを変える。範囲外の改善はしない
- 秘密情報・権限設定・破壊的スキーマ・決済ロジックには触れない（必要なら作業を止めて理由を書く）
- 作業が終わったら、変更の要約を 5 行以内で最後に出力する。git commit / push / PR 作成はこちらで行うので実行しない
EOF
)

set +e
CLAUDE_OUT=$(claude -p "${PROMPT}" --model "${MODEL}" --max-turns "${MAX_TURNS}" --output-format json --dangerously-skip-permissions 2>claude.err)
CLAUDE_RC=$?
set -e
echo "[worker] claude exit=${CLAUDE_RC}"
sed 's/^/[claude.err] /' claude.err | tail -50 || true
SUMMARY=$(jq -r '.result // ""' <<<"${CLAUDE_OUT}" 2>/dev/null | tail -20)
USAGE=$(jq -c '.usage // {}' <<<"${CLAUDE_OUT}" 2>/dev/null || echo '{}')
COST=$(jq -r '.total_cost_usd // 0' <<<"${CLAUDE_OUT}" 2>/dev/null || echo 0)
TURNS=$(jq -r '.num_turns // 0' <<<"${CLAUDE_OUT}" 2>/dev/null || echo 0)

if [ -z "$(git status --porcelain)" ]; then
  echo "[worker] no changes produced"
  echo "FACTORY_RESULT:$(jq -nc --argjson usage "${USAGE}" --arg cost "${COST}" --arg model "${MODEL}" --arg turns "${TURNS}" '{pr_url:null, reason:"no_changes", usage:$usage, cost_usd:($cost|tonumber), model:$model, num_turns:($turns|tonumber)}')"
  exit 3
fi

git add -A
git commit -q -m "$(printf 'feat: %s (#%s)\n\n工場のヘッドレスワーカー（job %s）による実装。\n\nCloses #%s' "${TITLE}" "${ISSUE_NUMBER}" "${JOB_ID}" "${ISSUE_NUMBER}")"
git push -q -u origin "${BRANCH}:${BRANCH}"

PR_BODY=$(cat <<EOF
Closes #${ISSUE_NUMBER}

## 概要
${SUMMARY}

## 工場の記録
- job: ${JOB_ID}（attempt は dispatcher の記録を参照）
- model: ${MODEL}、turns: ${TURNS}、cost(API 換算): \$${COST}
- 権限: P0 は PR 作成まで。マージは人間の判断（manifest.yaml autonomy）
EOF
)
PR_URL=$(gh pr create -R "${REPO}" --base "${DEFAULT_BRANCH}" --head "${BRANCH}" --title "${TITLE} (#${ISSUE_NUMBER})" --body "${PR_BODY}")
PR_NUMBER=$(basename "${PR_URL}")
echo "[worker] pr=${PR_URL}"

CHANGED=$(git diff --name-only "origin/${DEFAULT_BRANCH}...HEAD" | tr '\n' ' ')
CHANGE_TYPE="code_change"
if ! git diff --name-only "origin/${DEFAULT_BRANCH}...HEAD" | grep -vqE '(\.md$|^docs/|^CLAUDE\.md$)'; then CHANGE_TYPE="docs_only"; fi

echo "FACTORY_RESULT:$(jq -nc --arg pr "${PR_URL}" --arg n "${PR_NUMBER}" --argjson usage "${USAGE}" --arg cost "${COST}" --arg model "${MODEL}" --arg turns "${TURNS}" --arg ct "${CHANGE_TYPE}" --arg files "${CHANGED}" \
  '{pr_url:$pr, pr_number:($n|tonumber), usage:$usage, cost_usd:($cost|tonumber), model:$model, num_turns:($turns|tonumber), change_types:[$ct], files:$files}')"
