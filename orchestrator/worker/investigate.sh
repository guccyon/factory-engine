#!/usr/bin/env bash
# 工場の中での一次調査のジョブ（ADR 0048 の順番 3）: dispatcher が組み立てた調査の依頼（標準入力）を Engine の core-gates/investigate.py に渡し、
# 調査役に工場の記録を読ませて、結果（報告・直す先・引き継ぎ資料）を最終行に JSON で出す。
# 調査役が読むのは、dispatcher の中継の道具（inv_sql・inv_job_log・inv_github）と、入っている版の Engine の definitions/ と docs/ だけ。
# Engine のソースコードは取らない（一次と二次の境目。definitions/investigate.yaml の sources.engine_docs）。
# GitHub のトークンも DB の接続情報も持たない。何も書かない（起票・操作・台帳は dispatcher が結果を見て行う）。
# 標準入力: dispatcher が組み立てた JSON（問い・製品・時間帯・手がかりの参照・上限）。依頼者の言葉はデータとして渡す
# 環境変数: JOB_ID CLAUDE_CODE_OAUTH_TOKEN RUNNER_RELAY_URL RUNNER_RELAY_TOKEN
set -euo pipefail

: "${JOB_ID:?}" "${CLAUDE_CODE_OAUTH_TOKEN:?}" "${RUNNER_RELAY_URL:?}" "${RUNNER_RELAY_TOKEN:?}"
unset GH_TOKEN GITHUB_TOKEN DATABASE_URL || true
ENGINE_REPO="${ENGINE_REPO:-guccyon/factory-engine}"
ENGINE_REF="${ENGINE_REF:-main}"
cat > /work/input.json
echo "[investigate] job=${JOB_ID} engine=${ENGINE_REPO}@${ENGINE_REF} input_bytes=$(wc -c < /work/input.json)"
# Engine は定義・文書と、この調査を動かす道具だけを取る（ソースコードの本体は取らない）
# ENGINE_REF は入っている版（窓口の依頼）か、直す前の版（評価セットの再生）のコミット。短いハッシュでも取れるよう履歴は取り、中身は要るときだけ取る
git clone --quiet --filter=blob:none --no-checkout --sparse "https://github.com/${ENGINE_REPO}.git" /work/engine
git -C /work/engine sparse-checkout set --no-cone /definitions/ /docs/ \
  /core-gates/investigate.py /core-gates/investigate_check.py /core-gates/incident_check.py /core-gates/agent_runner.py
if ! git -C /work/engine checkout --quiet "${ENGINE_REF}" 2>/dev/null; then
  echo "[investigate] engine_ref ${ENGINE_REF} が取れないので main で読む"
  git -C /work/engine checkout --quiet main
fi
# 調査役の道具（investigate.py など）は版によらず今の main のものを使う（直す前の版には調査役がない）
if [ "${ENGINE_REF}" != "main" ]; then
  git -C /work/engine checkout --quiet main -- core-gates/investigate.py core-gates/investigate_check.py core-gates/incident_check.py core-gates/agent_runner.py definitions/investigate.yaml definitions/agents.yaml 2>/dev/null || true
fi
set +e
python3 /work/engine/core-gates/investigate.py --input /work/input.json --engine-dir /work/engine --out /work/out.json 2>/work/investigate.err
RC=$?
set -e
sed 's/^/[investigate.err] /' /work/investigate.err | tail -20 || true
if [ "${RC}" -ne 0 ] || [ ! -s /work/out.json ]; then
  echo "FACTORY_RESULT:$(jq -nc --arg rc "${RC}" '{kind:"investigate", ok:false, exit:($rc|tonumber)}')"
  exit 1
fi
echo "FACTORY_RESULT:$(jq -c '{kind:"investigate", ok:true} + .' /work/out.json)"
exit 0
