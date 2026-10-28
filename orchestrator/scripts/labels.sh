#!/usr/bin/env bash
# 対象リポジトリに工場のラベルを作る。使い方: scripts/labels.sh guccyon/rally
set -euo pipefail
repo="${1:?repo (owner/name)}"
create() { gh label create "$1" -R "$repo" --color "$2" --description "$3" --force >/dev/null && echo "label $1"; }
create "factory:implement" "1f5f8b" "工場に実装を依頼する（付けると 30 秒以内にジョブになる）"
create "factory:queued"    "9aa5b4" "工場が受け付け、順番待ち"
create "factory:running"   "e39255" "ワーカーが実行中"
create "factory:pr-open"   "2e7d4f" "PR を作成済み。マージは人間の判断"
create "factory:failed"    "a63a3a" "失敗。factoryctl rerun で再実行"
create "factory:request"   "c5def5" "要求者の要求を窓口（intake / clarify）にかける"
create "factory:planned" "bfd4f2" "依存（本文の「依存: #N」）がすべて閉じたら supervisor が factory:implement に替える"
create "factory:needs-approval" "d93f0b" "supervisor の判定でマージに人の承認が要る（manifest の表）"
create "factory:provisional" "fbca04" "ワーカーが作業中に仮決めした判断を含む PR（ADR 0006。出口で judge が裁く）"
create "factory:judged:attended" "c2e0c6" "judge が attended（人が並走）で裁いた（ADR 0006）"
create "factory:judged:unattended" "5319e7" "judge が unattended（全面委任）で裁いた（ADR 0006）"
create "factory:confidence:low" "e99695" "judge の確信度が低い判断を含む"
create "factory:needs-judgment" "b60205" "judge が人に回した判断がある"
create "factory:sendback" "fef2c0" "差し戻し（judge が採用した指摘の修正）"
create "factory:fix-proposed" "d4c5f9" "差し戻しの提案。流すときは factory:planned に替える（attended の 1 タップ）"
create "factory:status" "5319e7" "工場の状況（番人が操業停止を通知する Issue。ADR 0005）"
create "factory:clarified" "0e8a16" "要求が確定した（何を・何のために・いつまでに・どの精度で）"
create "factory:priority:p0" "b60205" "緊急: 作業中のものを止めてでも先に流す（障害の調査・hotfix。ADR 0009）"
create "factory:priority:p1" "d93f0b" "急ぎ: 順番待ちの先頭に回す（ADR 0009）"
create "factory:priority:p2" "c5def5" "通常（付けなくても同じ）"
create "factory:priority:p3" "ededed" "後回し: ほかに流すものがないときに流す"
for p in cs-1 cs-2 mkt-1 mkt-2 sales-1; do create "requester:${p}" "ededed" "要求者ペルソナ ${p}（Gen1 は架空）"; done
