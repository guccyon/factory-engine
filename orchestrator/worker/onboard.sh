#!/usr/bin/env bash
# オンボーディングの棚卸しと解析のジョブ（S1・S2。ADR 0045 の追記 2026-11-15。Gen1 のプロトタイプ）:
# 製品の構成にある全リポを読み取りで取り、Engine の core-gates/onboard.py に読ませて、結果を最終行に JSON で出す。
# 先方のリポジトリには何も書かない（トークンは読み取りだけ。dispatcher が ghapp.read_token で渡す）。
# 標準入力: dispatcher が組み立てた JSON（composition・answers・files・previous_levels・previous_questions）
# 環境変数: JOB_ID GH_TOKEN（読み取り。owner ごとに違うときは GH_TOKENS="owner=トークン owner=トークン"）CLAUDE_CODE_OAUTH_TOKEN
set -euo pipefail

: "${JOB_ID:?}" "${CLAUDE_CODE_OAUTH_TOKEN:?}"
ENGINE_REPO="${ENGINE_REPO:-guccyon/factory-engine}"
ENGINE_REF="${ENGINE_REF:-main}"
cat > /work/input.json
echo "[onboard] job=${JOB_ID} engine=${ENGINE_REPO}@${ENGINE_REF}"
# owner ごとのトークン（GH_TOKENS）を、取るリポのパスで選ぶ。無ければ GH_TOKEN
cat > /work/cred.sh <<'CRED'
#!/usr/bin/env bash
path=""
while IFS= read -r line; do
  [ -z "${line}" ] && break
  case "${line}" in path=*) path="${line#path=}" ;; esac
done
owner="${path%%/*}"
tok="${GH_TOKEN:-}"
for pair in ${GH_TOKENS:-}; do
  [ "${pair%%=*}" = "${owner}" ] && tok="${pair#*=}"
done
echo "username=x-access-token"
echo "password=${tok}"
CRED
chmod 700 /work/cred.sh
git config --global credential.helper "/work/cred.sh"
git config --global credential.useHttpPath true
git clone --quiet --depth 1 --branch "${ENGINE_REF}" "https://github.com/${ENGINE_REPO}.git" /work/engine
mkdir -p /work/repos
OK=0; NG=0
for REPO in $(jq -r '.composition.repos[].repo' /work/input.json); do
  if ! [[ "${REPO}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then echo "[onboard] skip bad repo"; continue; fi
  DIR="/work/repos/${REPO/\//__}"
  # 「新しさ」を見分けるため、履歴は中身なしで取る（blob は読むときだけ）
  if git clone --quiet --filter=blob:none "https://github.com/${REPO}.git" "${DIR}" 2>/work/clone.err; then
    OK=$((OK + 1)); echo "[onboard] cloned ${REPO} ($(git -C "${DIR}" rev-parse --short HEAD))"
  else
    NG=$((NG + 1)); echo "[onboard] clone failed ${REPO}: $(head -c 200 /work/clone.err)"
  fi
done
if [ "${OK}" -eq 0 ]; then
  echo "FACTORY_RESULT:$(jq -nc --arg ng "${NG}" '{kind:"onboard", ok:false, error:"no repo readable", failed:($ng|tonumber)}')"
  exit 1
fi
set +e
python3 /work/engine/core-gates/onboard.py --input /work/input.json --root /work/repos --out /work/onboard.json 2>/work/onboard.err
RC=$?
set -e
sed 's/^/[onboard.err] /' /work/onboard.err | tail -20 || true
if [ "${RC}" -ne 0 ] || [ ! -s /work/onboard.json ]; then
  echo "FACTORY_RESULT:$(jq -nc --arg rc "${RC}" '{kind:"onboard", ok:false, exit:($rc|tonumber)}')"
  exit 1
fi
echo "FACTORY_RESULT:$(jq -c --arg ng "${NG}" '{kind:"onboard", ok:true, analysis, clone_failed:($ng|tonumber), versions, cost_usd:(.cost_usd // 0), usage:(.usage // {}), model, num_turns}' /work/onboard.json)"
exit 0
