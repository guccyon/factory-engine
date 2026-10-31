#!/usr/bin/env bash
# ワーカーの手順: 浅いクローン → エージェント（工程 implement）→ commit / push → PR → 結果を最終行に JSON で出す。
# エージェントの呼び出しは Engine の core-gates/agent_runner.py に集める（runner とモデルは definitions/agents.yaml。ADR 0010）。
# 環境変数: JOB_ID REPO ISSUE_NUMBER MODEL_TIER MAX_TURNS GH_TOKEN CLAUDE_CODE_OAUTH_TOKEN（任意: ENGINE_REPO ENGINE_REF）
# 続きのジョブ（任意）: CONTINUE_BRANCH CONTINUE_PR CONTINUE_REASON（incomplete | conflict）。新しいブランチと PR を作らず、
#   既存の工場の PR のブランチに既定ブランチを取り込んでから続ける。途中終了の続き（rerun --continue）と、工場の PR どうしの衝突の解消に使う
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
CONTINUE_BRANCH="${CONTINUE_BRANCH:-}"
if [ -n "${CONTINUE_BRANCH}" ]; then
  # 続ける先は工場のブランチと PR の番号だけ（dispatcher も同じ形を確かめてから渡す）
  if ! [[ "${CONTINUE_BRANCH}" =~ ^factory/issue-${ISSUE_NUMBER}-job-[0-9]+$ ]] || ! [[ "${CONTINUE_PR:-}" =~ ^[0-9]+$ ]]; then
    echo "[worker] bad continue target"; exit 2
  fi
  case "${CONTINUE_REASON:-}" in incomplete|conflict) ;; *) echo "[worker] bad continue reason"; exit 2 ;; esac
  BRANCH="${CONTINUE_BRANCH}"
fi
export GIT_TERMINAL_PROMPT=0

git config --global user.name "factory-worker"
git config --global user.email "factory-worker@users.noreply.github.com"
git config --global credential.helper '!f() { echo "username=x-access-token"; echo "password=${GH_TOKEN}"; }; f'
git clone --quiet --depth 1 --branch "${ENGINE_REF}" "https://github.com/${ENGINE_REPO}.git" /work/engine
AGENT=(python3 /work/engine/core-gates/agent_runner.py)
MODEL=$("${AGENT[@]}" resolve implement --tier "${MODEL_TIER:-tier-large}" | jq -r .model)
echo "[worker] job=${JOB_ID} repo=${REPO} issue=#${ISSUE_NUMBER} model=${MODEL} max_turns=${MAX_TURNS} engine=${ENGINE_REPO}@${ENGINE_REF}"

DEFAULT_BRANCH=$(gh api "repos/${REPO}" --jq .default_branch)
CONFLICTS=""
if [ -n "${CONTINUE_BRANCH}" ]; then
  # 取り込みに共通の祖先が要るので履歴ごと取る（中身は必要な分だけ）
  git clone --quiet --filter=blob:none --branch "${DEFAULT_BRANCH}" "https://github.com/${REPO}.git" repo
  cd repo
  git fetch --quiet origin "${BRANCH}:refs/remotes/origin/${BRANCH}"
  git checkout -q -b "${BRANCH}" "origin/${BRANCH}"
  if ! git merge --no-edit -q "origin/${DEFAULT_BRANCH}" >/dev/null 2>&1; then
    CONFLICTS=$(git diff --name-only --diff-filter=U | tr '\n' ' ')
  fi
  echo "[worker] continue ${CONTINUE_REASON} pr=#${CONTINUE_PR} branch=${BRANCH} conflicts=[${CONFLICTS}]"
else
  git clone --quiet --depth 1 --branch "${DEFAULT_BRANCH}" "https://github.com/${REPO}.git" repo
  cd repo
  git checkout -q -b "${BRANCH}"
fi

ISSUE_JSON=$(gh issue view "${ISSUE_NUMBER}" -R "${REPO}" --json title,body,labels)
TITLE=$(jq -r .title <<<"${ISSUE_JSON}")
BODY=$(jq -r '.body // ""' <<<"${ISSUE_JSON}")

CONTINUE_NOTE=""
if [ -n "${CONTINUE_BRANCH}" ]; then
  if [ "${CONTINUE_REASON}" = "incomplete" ]; then
    CONTINUE_NOTE="# 続きのジョブ
これは PR #${CONTINUE_PR} の続きです。前のジョブは上限で途中終了し、作業はこのブランチ（${BRANCH}）にコミットしてあります。git log と git diff origin/${DEFAULT_BRANCH}...HEAD で前の作業を確かめ、Issue の残りだけを仕上げてください。前の作業をやり直さないこと。"
  else
    CONTINUE_NOTE="# 衝突の解消のジョブ
これは PR #${CONTINUE_PR} のブランチ（${BRANCH}）に既定ブランチを取り込むジョブです。Issue の実装はすでに済んでいます。範囲を広げず、取り込みで起きた衝突の解消と、取り込んだ変更に合わせて壊れたビルド・テストの直しだけを行ってください。"
  fi
  if [ -n "${CONFLICTS}" ]; then
    CONTINUE_NOTE="${CONTINUE_NOTE}
既定ブランチの取り込みで衝突しています（取り込みは途中で、衝突の印が残っています）: ${CONFLICTS}
両方の変更の意図を残して解消し、衝突の印（<<<<<<< ======= >>>>>>>）を残さないこと。CHANGELOG.md の追記どうしの衝突は、両方の行を残します。取り込みを中止（git merge --abort）したり、ブランチを作り直したりしないこと。"
  fi
  CONTINUE_NOTE="${CONTINUE_NOTE}
最後の出力の「## 概要」以下は、この続きのジョブでしたことだけを書く（PR の説明には節として追記する）。
"
fi

PROMPT=$(cat <<EOF
${CONTINUE_NOTE}
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

MERGING=""
[ -f .git/MERGE_HEAD ] && MERGING="1"   # 衝突のない取り込みはコミット済みなので、ここに来るのは衝突を解いた途中の取り込みだけ
AHEAD="0"
[ -n "${CONTINUE_BRANCH}" ] && AHEAD=$(git rev-list --count "origin/${BRANCH}..HEAD")
if [ -n "${CONFLICTS}" ] && git grep -qE '^(<<<<<<<|>>>>>>>) ' -- ${CONFLICTS} 2>/dev/null; then
  echo "[worker] conflict markers remain"
  echo "FACTORY_RESULT:$(jq -nc --argjson usage "${USAGE}" --arg cost "${COST}" --arg model "${MODEL}" --arg turns "${TURNS}" --arg pr "${CONTINUE_PR}" '{pr_url:null, reason:"conflict_unresolved", continue_pr:($pr|tonumber), usage:$usage, cost_usd:($cost|tonumber), model:$model, num_turns:($turns|tonumber)}')"
  exit 5
fi
if [ -z "$(git status --porcelain)" ] && [ -z "${MERGING}" ] && [ "${AHEAD}" = "0" ]; then
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
if [ -n "${CONTINUE_BRANCH}" ]; then
  # 既存の PR のブランチに積む（取り込みはマージコミットで、履歴は書き換えない）
  git add -A
  if [ -n "${MERGING}" ]; then
    git commit -q --no-edit
  fi
  if [ -n "$(git diff --cached --name-only)" ]; then
    git commit -q -m "$(printf '%s: %s (#%s)\n\n工場のヘッドレスワーカー（job %s）による続き。%s\n\nRefs #%s' "$([ "${CONTINUE_REASON}" = conflict ] && echo '衝突の解消' || echo '続き')" "${TITLE}" "${ISSUE_NUMBER}" "${JOB_ID}" "${INCOMPLETE:+ 途中終了（claude exit ${CLAUDE_RC}）のため未完了。}" "${ISSUE_NUMBER}")"
  fi
  git push -q origin "HEAD:${BRANCH}"
  PR_URL="https://github.com/${REPO}/pull/${CONTINUE_PR}"
  PR_NUMBER="${CONTINUE_PR}"
  OLD_BODY=$(gh pr view "${CONTINUE_PR}" -R "${REPO}" --json body --jq .body)
  SECTION_TITLE=$([ "${CONTINUE_REASON}" = conflict ] && echo "既定ブランチの取り込みと衝突の解消" || echo "続きのジョブ")
  NEW_BODY=$(printf '%s\n\n## %s（job %s）\n%s\n\n- model: %s、turns: %s、cost(API 換算): $%s\n' "${OLD_BODY}" "${SECTION_TITLE}" "${JOB_ID}" "${SUMMARY}" "${MODEL}" "${TURNS}" "${COST}")
  if [ "${CONTINUE_REASON}" = "incomplete" ] && [ -z "${INCOMPLETE}" ]; then
    # 仕上がったら未完了の印を外し、Issue を閉じる参照に直してドラフトを外す
    NEW_BODY=$(printf '%s' "${NEW_BODY}" | sed -e '/^\*\*未完了\*\*:/d' -e "s/^Refs #${ISSUE_NUMBER}$/Closes #${ISSUE_NUMBER}/")
    gh pr edit "${CONTINUE_PR}" -R "${REPO}" --title "${TITLE} (#${ISSUE_NUMBER})" --body "${NEW_BODY}" >/dev/null
    gh pr ready "${CONTINUE_PR}" -R "${REPO}" >/dev/null || true
  else
    gh pr edit "${CONTINUE_PR}" -R "${REPO}" --body "${NEW_BODY}" >/dev/null
  fi
  echo "[worker] continued pr=${PR_URL}"
else
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

fi

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
