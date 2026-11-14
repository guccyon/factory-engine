#!/usr/bin/env bash
# QA 役のジョブ（ADR 0028 の決定 2）: 製品の main を浅く取り、Engine の core-gates/qa.py に、触れる環境（staging を兼ねる）へ
# ユーザーストーリー（specs/stories/）を流させ、ストーリーごとの Verdict を返す。
# 製品のリポジトリにも Issue にも何も書かない（Defect の起票と固定 Issue のまとめは dispatcher の qa_line.py）。
# 出力: 根拠のスクリーンショットを 1 枚ずつ FACTORY_FILE:<名前>:<base64> の行で、最終行に FACTORY_RESULT の JSON
# 標準入力: dispatcher が組み立てた JSON（stories・env・env_host・release_id・touchable。夜間の探索は mode: explore・charter・night、合成の利用者は mode: synthetic・friction。重点 focus と回帰の scenarios は品質の Learn から）
# 環境変数: JOB_ID REPO ISSUE_NUMBER GH_TOKEN CLAUDE_CODE_OAUTH_TOKEN
set -euo pipefail

: "${JOB_ID:?}" "${REPO:?}" "${GH_TOKEN:?}" "${CLAUDE_CODE_OAUTH_TOKEN:?}"
if ! [[ "${REPO}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
  echo "[qa] not a repo"; exit 2
fi
ENGINE_REPO="${ENGINE_REPO:-guccyon/factory-engine}"
ENGINE_REF="${ENGINE_REF:-main}"
cat > /work/input.json
echo "[qa] job=${JOB_ID} repo=${REPO} engine=${ENGINE_REPO}@${ENGINE_REF}"
git config --global credential.helper '!f() { echo "username=x-access-token"; echo "password=${GH_TOKEN}"; }; f'
/usr/local/bin/clone_engine.sh "${ENGINE_REPO}" "${ENGINE_REF}"
DEFAULT_BRANCH=$(gh repo view "${REPO}" --json defaultBranchRef -q .defaultBranchRef.name 2>/dev/null || echo main)
git clone --quiet --depth 1 --branch "${DEFAULT_BRANCH}" "https://github.com/${REPO}.git" /work/product
set +e
python3 /work/engine/core-gates/qa.py --repo-dir /work/product --input /work/input.json --out-dir /work/qa --out /work/qa.json 2>/work/qa.err
RC=$?
set -e
sed 's/^/[qa.err] /' /work/qa.err | tail -20 || true
SHA=$(git -C /work/product rev-parse HEAD)
if [ "${RC}" -ne 0 ] || [ ! -s /work/qa.json ]; then
  ERRS='[]'; [ -s /work/qa.json ] && ERRS=$(jq -c '.errors // []' /work/qa.json)
  echo "FACTORY_RESULT:$(jq -nc --arg rc "${RC}" --argjson errs "${ERRS}" '{kind:"qa", ok:false, exit:($rc|tonumber), errors:$errs}')"
  exit 1
fi
for f in $(jq -r '.files[]' /work/qa.json); do
  echo "FACTORY_FILE:${f}:$(base64 -w0 "/work/qa/${f}")"
done
echo "FACTORY_RESULT:$(jq -c --arg sha "${SHA}" '{kind:"qa", ok:true, sha:$sha, mode:(.mode // "verdict"), env:.env, release_id:.release_id, stories:(.stories // []), verdicts:(.verdicts // []), charter:.charter, night:.night, explore:.explore, synthetic:.synthetic, mobile:.mobile, files:.files, knowledge_ids:(.knowledge_ids // []), knowledge_selection:(.knowledge_selection // {}), cost_usd:(.cost_usd // 0), usage:(.usage // {}), model:.model, num_turns:.num_turns}' /work/qa.json)"
exit 0
