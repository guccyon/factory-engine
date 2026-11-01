#!/usr/bin/env bash
# デザインシステムのデザイナー役のジョブ（ADR 0025・0026）: 製品の main を浅く取り、Engine の core-gates/design_system.py に
# コンセプトとトーン＆マナーの案（見本の画面つき）か、デザインシステムの正本（specs/design/ のファイル）を作らせる。
# 製品のリポジトリにも Issue にも何も書かない（人の選択への回付と、正本を Issue に足して実装役へ渡すのは dispatcher が行う）。
# 出力: 見本のファイルを 1 つずつ FACTORY_FILE:<名前>:<base64> の行で、最終行に FACTORY_RESULT の JSON（spec のファイルは JSON の中）
# 標準入力: dispatcher が組み立てた JSON（phase・選ばれたコンセプト・作り直しの一言。最初は {}）
# 環境変数: JOB_ID REPO ISSUE_NUMBER GH_TOKEN CLAUDE_CODE_OAUTH_TOKEN
set -euo pipefail

: "${JOB_ID:?}" "${REPO:?}" "${ISSUE_NUMBER:?}" "${GH_TOKEN:?}" "${CLAUDE_CODE_OAUTH_TOKEN:?}"
if ! [[ "${REPO}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || ! [[ "${ISSUE_NUMBER}" =~ ^[0-9]+$ ]]; then
  echo "[design_system] bad repo or issue"; exit 2
fi
ENGINE_REPO="${ENGINE_REPO:-guccyon/factory-engine}"
ENGINE_REF="${ENGINE_REF:-main}"
cat > /work/input.json
echo "[design_system] job=${JOB_ID} repo=${REPO}#${ISSUE_NUMBER} engine=${ENGINE_REPO}@${ENGINE_REF}"
git config --global credential.helper '!f() { echo "username=x-access-token"; echo "password=${GH_TOKEN}"; }; f'
git clone --quiet --depth 1 --branch "${ENGINE_REF}" "https://github.com/${ENGINE_REPO}.git" /work/engine
DEFAULT_BRANCH=$(gh repo view "${REPO}" --json defaultBranchRef -q .defaultBranchRef.name 2>/dev/null || echo main)
git clone --quiet --depth 1 --branch "${DEFAULT_BRANCH}" "https://github.com/${REPO}.git" /work/product
gh issue view "${ISSUE_NUMBER}" -R "${REPO}" --json number,title,body | jq --arg repo "${REPO}" '. + {repo:$repo}' > /work/issue.json
set +e
python3 /work/engine/core-gates/design_system.py --repo-dir /work/product --issue /work/issue.json --input /work/input.json \
  --out-dir /work/design --out /work/design.json 2>/work/design.err
RC=$?
set -e
sed 's/^/[design_system.err] /' /work/design.err | tail -20 || true
if [ "${RC}" -ne 0 ] || [ ! -s /work/design.json ]; then
  ERRS='[]'; [ -s /work/design.json ] && ERRS=$(jq -c '.errors // []' /work/design.json)
  echo "FACTORY_RESULT:$(jq -nc --arg rc "${RC}" --argjson errs "${ERRS}" '{kind:"design_system", ok:false, exit:($rc|tonumber), errors:$errs}')"
  exit 1
fi
for f in $(jq -r '.files // [] | .[]' /work/design.json); do
  echo "FACTORY_FILE:${f}:$(base64 -w0 "/work/design/${f}")"
done
echo "FACTORY_RESULT:$(jq -c '{kind:"design_system", ok:true, phase, delta, round, concepts, spec_files, files, cost_usd:(.cost_usd // 0), usage:(.usage // {}), model, num_turns}' /work/design.json)"
exit 0
