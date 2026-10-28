#!/usr/bin/env bash
# ワーカーの手順: 浅いクローン → エージェント（工程 implement）→ commit / push → PR → 結果を最終行に JSON で出す。
# エージェントの呼び出しは Engine の core-gates/agent_runner.py に集める（runner とモデルは definitions/agents.yaml。ADR 0010）。
# 環境変数: JOB_ID REPO ISSUE_NUMBER MODEL_TIER MAX_TURNS GH_TOKEN CLAUDE_CODE_OAUTH_TOKEN（任意: ENGINE_REPO ENGINE_REF）
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
ENGINE_REPO="${ENGINE_REPO:-guccyon/factory-engine}"
ENGINE_REF="${ENGINE_REF:-main}"
BRANCH="factory/issue-${ISSUE_NUMBER}-job-${JOB_ID}"
export GIT_TERMINAL_PROMPT=0

git config --global user.name "factory-worker"
git config --global user.email "factory-worker@users.noreply.github.com"
git config --global credential.helper '!f() { echo "username=x-access-token"; echo "password=${GH_TOKEN}"; }; f'
git clone --quiet --depth 1 --branch "${ENGINE_REF}" "https://github.com/${ENGINE_REPO}.git" /work/engine
AGENT=(python3 /work/engine/core-gates/agent_runner.py)
MODEL=$("${AGENT[@]}" resolve implement --tier "${MODEL_TIER:-tier-large}" | jq -r .model)
echo "[worker] job=${JOB_ID} repo=${REPO} issue=#${ISSUE_NUMBER} model=${MODEL} max_turns=${MAX_TURNS} engine=${ENGINE_REPO}@${ENGINE_REF}"

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
- 先に README と specs/（あれば specs/README.md と、そこで最初に読むよう指示されたファイル）と docs/ を読み、用語・境界・不変条件をそれに合わせる。手元の推測で定義を作らない
- リポジトリの CLAUDE.md や README の規約に従う。コード・テスト・文書は同じ変更に含める（三位一体）
- Issue の範囲だけを変える。範囲外の改善はしない
- 秘密情報・権限設定・破壊的スキーマ・決済ロジックには触れない（必要なら作業を止めて理由を書く）
- 作業の途中で判断が要ったら（画面、API やイベントや用語、内部の作り、エラー処理、データ、スコープ）、止まらずに最も妥当な案で仮決めして続ける。人に問い返すための ADR や Question だけを作って止めない。仮決めは後で判定役が裁き、差し替えになったら別のチケットで直す
- 仮決めしたものは、最後の出力の「## 仮決め」に 1 件ずつ、次の形で書く（なければ見出しごと省く）:
  「### 類型 id: 問い（1 行）」の見出しの下に「- 選んだ: 」「- 他の案: 」「- 根拠: 」（参照したファイルのパスと見出し）「- 解釈: 」（根拠をどう読んでその案にしたか）「- 確信度: 」（0.0〜1.0 の数）「- 影響: 」（変えたファイル・契約・後のチケット）の 6 行
  類型 id は何について決めたかで選ぶ。ui（画面の並び・文言・操作の流れ）・system_interface（API・イベントの形、用語）・implementation（置き場所・層・型・テスト）・error_handling（失敗・重複・上限・境界のときの振る舞い）・data（保存の形・スキーマ・マイグレーション・seed）・scope（やるか、別に回すか）のどれか
  チケットの受け入れ条件が 2 通りに読めて、その解釈で決めたときは「- チケットの解釈: はい」の 1 行を足す
- 文書・コメント・PR の説明・コミットメッセージは日本語で書く（識別子・コマンド・ツールの出力は原文のまま）
- YAML を書いたら 「yq eval . ファイル名」 で読めることを確かめる
- 環境変数 DATABASE_URL があれば、それはこのジョブ専用の空の PostgreSQL（終了時に消える）。DB が要るテストはこれに向けて動かす。自前で PostgreSQL を立てない
- 作業が終わったら、最後の出力を次の見出しだけで構成する（PR の説明になる。該当しない見出しは省く）:
  「## 概要」（1〜3 文）「## 対応内容」（変更点ごとに箇条書き）「## 影響範囲・互換性」「## 動作確認」（実際に確かめたことだけ）「## 補足」（対象外にしたこと）
  「## 仮決め」（上の形）「## 知見」（次の作業に効く事実を 1 行ずつ、最大 3 行。なければ見出しごと省く）
- git commit / push / PR 作成はこちらで行うので実行しない
EOF
)

set +e
CLAUDE_OUT=$("${AGENT[@]}" exec implement --tier "${MODEL_TIER:-tier-large}" --max-turns "${MAX_TURNS}" <<<"${PROMPT}" 2>/work/claude.err)   # stderr はリポジトリの外に置く（コミットに混ざらないように）
CLAUDE_RC=$?
set -e
echo "[worker] claude exit=${CLAUDE_RC}"
sed 's/^/[claude.err] /' /work/claude.err | tail -50 || true
SUMMARY=$(jq -r '.output // ""' <<<"${CLAUDE_OUT}" 2>/dev/null | tail -80)   # 「## 知見」も PR 本文に残す（人と検査官が読めるように。Lesson にも別に記録する）
LESSONS_JSON=$(jq -r '.output // ""' <<<"${CLAUDE_OUT}" 2>/dev/null | awk 'f{print} /^## 知見/{f=1}' | sed -nE 's/^[-*] *//p' | jq -R . | jq -sc .)
[ -z "${LESSONS_JSON}" ] && LESSONS_JSON='[]'
# 「## 仮決め」の節はそのまま渡す（分解は dispatcher が行う。ADR 0006）
PROVISIONAL=$(jq -r '.output // ""' <<<"${CLAUDE_OUT}" 2>/dev/null | awk '/^## /{f=($0 ~ /^## 仮決め/); next} f{print}')
USAGE=$(jq -c '.usage // {}' <<<"${CLAUDE_OUT}" 2>/dev/null || echo '{}')
COST=$(jq -r '.cost_usd // 0' <<<"${CLAUDE_OUT}" 2>/dev/null || echo 0)
TURNS=$(jq -r '.num_turns // 0' <<<"${CLAUDE_OUT}" 2>/dev/null || echo 0)

if [ -z "$(git status --porcelain)" ]; then
  echo "[worker] no changes produced"
  echo "FACTORY_RESULT:$(jq -nc --argjson usage "${USAGE}" --arg cost "${COST}" --arg model "${MODEL}" --arg turns "${TURNS}" '{pr_url:null, reason:"no_changes", usage:$usage, cost_usd:($cost|tonumber), model:$model, num_turns:($turns|tonumber)}')"
  exit 3
fi

INCOMPLETE=""
if [ "${CLAUDE_RC}" -ne 0 ]; then
  INCOMPLETE="1"   # max-turns などで途中終了。作業は捨てずにドラフト PR として残し、ジョブは失敗にする
  echo "[worker] claude exited ${CLAUDE_RC}: opening a draft PR marked 未完了"
fi
LINK_KW="Closes"
[ -n "${INCOMPLETE}" ] && LINK_KW="Refs"   # 未完了は Issue を閉じない
PR_TITLE="${TITLE} (#${ISSUE_NUMBER})"
[ -n "${INCOMPLETE}" ] && PR_TITLE="[未完了] ${PR_TITLE}"
git add -A
git commit -q -m "$(printf 'feat: %s (#%s)\n\n工場のヘッドレスワーカー（job %s）による実装。%s\n\n%s #%s' "${TITLE}" "${ISSUE_NUMBER}" "${JOB_ID}" "${INCOMPLETE:+ 途中終了（claude exit ${CLAUDE_RC}）のため未完了。}" "${LINK_KW}" "${ISSUE_NUMBER}")"
git push -q -u origin "${BRANCH}:${BRANCH}"

PR_BODY=$(cat <<EOF
${INCOMPLETE:+**未完了**: ワーカーの Claude Code が途中で終了しました（exit ${CLAUDE_RC}、turns ${TURNS}、max_turns ${MAX_TURNS}）。差分は残してありますが、Issue の範囲を満たしているかは建設者が確認してください。続きは factoryctl rerun で。

}${LINK_KW} #${ISSUE_NUMBER}

${SUMMARY}

## 工場の記録
- job: ${JOB_ID}（attempt は dispatcher の記録を参照）
- model: ${MODEL}、turns: ${TURNS}、cost(API 換算): \$${COST}
- 権限: P0 は PR 作成まで。マージは人間の判断（manifest.yaml autonomy）
EOF
)
PR_URL=$(gh pr create -R "${REPO}" --base "${DEFAULT_BRANCH}" --head "${BRANCH}" --title "${PR_TITLE}" --body "${PR_BODY}" ${INCOMPLETE:+--draft})
PR_NUMBER=$(basename "${PR_URL}")
echo "[worker] pr=${PR_URL}"

CHANGED=$(git diff --name-only "origin/${DEFAULT_BRANCH}...HEAD" | tr '\n' ' ')
CHANGE_TYPE="code_change"
if ! git diff --name-only "origin/${DEFAULT_BRANCH}...HEAD" | grep -vqE '(\.md$|^docs/|^CLAUDE\.md$)'; then CHANGE_TYPE="docs_only"; fi

echo "FACTORY_RESULT:$(jq -nc --arg pr "${PR_URL}" --arg n "${PR_NUMBER}" --argjson usage "${USAGE}" --arg cost "${COST}" --arg model "${MODEL}" --arg turns "${TURNS}" --arg ct "${CHANGE_TYPE}" --arg files "${CHANGED}" \
  --argjson lessons "${LESSONS_JSON}" --arg provisional "${PROVISIONAL}" \
  --arg incomplete "${INCOMPLETE}" --arg rc "${CLAUDE_RC}" \
  '{pr_url:$pr, pr_number:($n|tonumber), usage:$usage, cost_usd:($cost|tonumber), model:$model, num_turns:($turns|tonumber), change_types:[$ct], files:$files, lessons:$lessons, provisional_raw:$provisional, incomplete:($incomplete=="1"), claude_exit:($rc|tonumber)}')"
# 途中終了なら、PR は残したうえでジョブとしては失敗にする（dispatcher が factory:failed にして rerun を案内する）
# 最後の行を `[ … ] && exit 4` にすると、条件が偽のときスクリプトの終了コードが 1 になる（T08 job 30 で成功が失敗扱いになった）
if [ -n "${INCOMPLETE}" ]; then exit 4; fi
exit 0
