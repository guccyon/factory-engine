#!/usr/bin/env bash
# clarify の収束を測るための一巡。要求者ペルソナ全員について、最初のひとこと（opening_examples の先頭）を
# 要求の Issue として起票し、窓口（clarify）にかける。結果は events の request.clarified / request.unresolved に
# converged（確定・往復 3 回以内・意図と一致）として残り、roadmap.yaml の clarify_3_rounds が人数を数える。
#
# 使い方: scripts/clarify-bench.sh <owner/name> [persona_id ...]
#   persona を省くと全員。1 人あたり問い手・ペルソナ・採点役で 7 回ほどの呼び出し（tier-judge 4・tier-small 3）。
#   起票した Issue は clarify が終わっても閉じない（往復の記録として残る）。見終えたら閉じてよい。
set -euo pipefail
repo="${1:?repo (owner/name)}"; shift
here="$(cd "$(dirname "$0")" && pwd)"
# YAML は ruby で JSON にして jq で読む（Mac の標準で動く。core-gates/agent_runner.py と同じ逃げ道）
personas=$(ruby -ryaml -rjson -e 'puts JSON.generate(YAML.safe_load(File.read(ARGV[0])))' "${here}/../worker/personas/requesters.yaml")
ids=("$@")
[ "${#ids[@]}" -eq 0 ] && while IFS= read -r id; do ids+=("${id}"); done < <(jq -r '.personas[].id' <<<"${personas}")

for id in "${ids[@]}"; do
  p=$(jq -c --arg id "${id}" '.personas[] | select(.id == $id)' <<<"${personas}")
  [ -z "${p}" ] && { echo "skip ${id}: ペルソナがない"; continue; }
  opening=$(jq -r '.opening_examples[0]' <<<"${p}")
  role=$(jq -r .role <<<"${p}")
  gh label create "requester:${id}" -R "${repo}" --color "ededed" --description "要求者ペルソナ ${id}（${role}、架空）" --force >/dev/null
  url=$(gh issue create -R "${repo}" --title "${opening}" --label "factory:request" --label "requester:${id}" \
    --body "${opening}

（clarify の収束の計測。要求者 ${id} は架空のペルソナで、往復は窓口と工場のエージェントが行う）")
  echo "${id}: ${url}"
done
