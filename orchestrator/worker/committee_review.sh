#!/usr/bin/env bash
# 知見の委員会のジョブ（ADR 0024）: 知見の変更セットの PR の head を取り、Engine の core-gates/committee_review.py で 4 役に審査させる。
# 差し戻し以外は、議決（committee/D-NNNN.yaml）と、下書きの知見を active にした変更を PR のブランチに積む。結果は最終行に JSON で出す。
# PR へのコメント・クローズ・担保の Issue は dispatcher が結果を見て行う。
# 環境変数: JOB_ID REPO PR_URL GH_TOKEN CLAUDE_CODE_OAUTH_TOKEN（PR_URL は制御プレーンが自分の記録から作ったもの）
set -euo pipefail

: "${JOB_ID:?}" "${REPO:?}" "${PR_URL:?}" "${GH_TOKEN:?}" "${CLAUDE_CODE_OAUTH_TOKEN:?}"
if ! [[ "${PR_URL}" =~ ^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[0-9]+$ ]]; then
  echo "[committee] not a PR url"; exit 2
fi
ENGINE_REPO="${ENGINE_REPO:-guccyon/factory-engine}"
ENGINE_REF="${ENGINE_REF:-main}"
echo "[committee] job=${JOB_ID} pr=${PR_URL} engine=${ENGINE_REPO}@${ENGINE_REF}"
git config --global credential.helper '!f() { echo "username=x-access-token"; echo "password=${GH_TOKEN}"; }; f'
git config --global user.name "factory-committee"
git config --global user.email "factory-committee@users.noreply.github.com"
git clone --quiet --depth 1 --branch "${ENGINE_REF}" "https://github.com/${ENGINE_REPO}.git" /work/engine
read -r HEAD_REF BASE_REF HEAD_REPO < <(gh pr view "${PR_URL}" --json headRefName,baseRefName,headRepository,headRepositoryOwner \
  -q '"\(.headRefName) \(.baseRefName) \(.headRepositoryOwner.login)/\(.headRepository.name)"')
if [ "${HEAD_REPO}" != "${REPO}" ]; then
  echo "[committee] head is not in ${REPO}"; exit 2   # 工場の PR は同じリポジトリのブランチから出る。fork には書かない
fi
git clone --quiet --branch "${HEAD_REF}" "https://github.com/${REPO}.git" /work/product
git -C /work/product fetch --quiet origin "${BASE_REF}"
set +e
python3 /work/engine/core-gates/committee_review.py --repo-dir /work/product --base "origin/${BASE_REF}" --pr-url "${PR_URL}" --out /work/committee.json 2>/work/committee.err
RC=$?
set -e
sed 's/^/[committee.err] /' /work/committee.err | tail -20 || true
if [ "${RC}" -ne 0 ] || [ ! -s /work/committee.json ]; then
  echo "FACTORY_RESULT:$(jq -nc --arg pr "${PR_URL}" --arg rc "${RC}" '{kind:"committee_review", pr_url:$pr, ok:false, exit:($rc|tonumber)}')"
  exit 1
fi
PUSHED=""
if [ -n "$(git -C /work/product status --porcelain -- .factory-config/knowledge)" ]; then
  DEC=$(jq -r '.decision // ""' /work/committee.json)
  OUT=$(jq -r '.outcome' /work/committee.json)
  git -C /work/product add .factory-config/knowledge
  git -C /work/product commit --quiet -m "知見の委員会の議決 ${DEC}（${OUT}）を記録する" -m "ADR 0024。core-gates/committee_review.py が 4 役の票を集めて書いた"
  git -C /work/product push --quiet origin "HEAD:${HEAD_REF}"
  PUSHED=$(git -C /work/product rev-parse HEAD)
fi
echo "FACTORY_RESULT:$(jq -c --arg pr "${PR_URL}" --arg pushed "${PUSHED}" '{kind:"committee_review", pr_url:$pr, ok:true, pushed_sha:$pushed, outcome:.outcome, decision:.decision, votes:.votes, enforce:(.enforce // []), added:(.added // []), reason:.reason, cost_usd:(.cost_usd // 0), usage:(.usage // {}), model:.model}' /work/committee.json)"
exit 0
