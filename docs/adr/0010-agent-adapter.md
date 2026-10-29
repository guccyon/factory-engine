# ADR 0010: 工程とエージェントを分け、呼び出しを 1 か所に集める

- 状態: 承認（2026-10-28 オーナー。スレッド「工場の使い方と仕様を確かめる」）
- 関係: ADR 0004（supervisor）、ADR 0006（judge）。物理構成図・対応表は建設者の手元の 50_build-work/strategy/2026-10-28_factory-physical-architecture.html、ケイパビリティ台帳の O8

## 文脈

工場の各工程（実装・要求の確認・検査・judge・本文の追記・ペア比較の評価）は、それぞれのスクリプトが `claude -p` を直接呼んでいた。モデル名も工程ごとに書かれ（`entrypoint.sh` の case、`clarify.sh`、`judge.py`、`body_fix.py`、`shadow_review.py`、`pairwise_judge.py`）、再試行や出力の読み方（`structured_output` / `result`、`total_cost_usd`）も 6 か所に別々にあった。
オーナー から「コードレビューだけは Codex に頼む、のように工程ごとにエージェントを替えられるか」と問われた（2026-10-28）。いまの作りでは、替えるには工程のコードを書き換えるしかない。

## 決定

1. **工程は「何をするか」だけを知り、どのエージェントとモデルで動かすかは `definitions/agents.yaml` で決める**。表は 3 つ: runner（エージェントの種類と実装済みか）、tier（`tier-large` / `tier-judge` / `tier-small` → runner とモデル）、step（工程 → tier、または runner とモデルを直接）。工程のコードに CLI 名やモデル名を書かない
2. **呼び出しは `core-gates/agent_runner.py` の 1 か所に集める**。Python からは `run(step, prompt, schema=...)`（ツールなしで 1 回答える）と `run_agentic(step, prompt)`（ツールありで作業する）、シェルからは CLI（`resolve` / `run` / `exec`）。出力は runner によらず `output`・`cost_usd`・`usage`・`model`・`runner`・`num_turns`・`exit` にそろえる。一時的な失敗の再試行（20・40・80 秒）もここに 1 つ
3. **いまは Claude だけを使う**（オーナー「差し替えられるように分離設計しつつ、今は Claude だけで良い」）。実装済みの runner は `claude-code` だけで、`codex` と `anthropic-api` は表に `not_implemented` として置く。実装していない runner を選ぶと、呼ぶ前に止まる（黙って Claude に戻さない）
4. 呼び出し側が tier を上書きできる口は残す（dispatcher の `MODEL_TIER`、`shadow_review.py` と `pairwise_judge.py` の `--tier`）。上書きしても runner とモデルの解決は表を通る
5. 実装と要求の確認のワーカーも、検査・judge と同じく実行時に Engine を浅く clone して `agent_runner.py` を使う（`ENGINE_REPO` / `ENGINE_REF`）。表を変えるとイメージを作り直さずに次のジョブから効く

## runner を足すとき

1. `agent_runner.py` にアダプター（1 回答える版と、作業する版の要るほう）を足し、出力を上の形にそろえる。構造化出力の取り方（JSON Schema を渡せるか）、ツールの止め方、費用の取り方は runner ごとに違うので、ここで吸収する
2. ワーカーのイメージにその CLI と認証の受け渡しを足す（認証の値は `.env` から。表には書かない）
3. `agents.yaml` の runner を `implemented` にし、使う step に runner とモデルを書く
4. 同じ PR を、評価（`health/evals/` のペア比較や golden）で前後比較してから切り替える。判定器を替えると judge の確信度の分布も変わるので、閾値は合わせて見直す

## 選ばなかった案

- **工程ごとに CLI を直接呼ぶまま、モデル名だけ環境変数にする**: モデルは替えられるが、エージェントの種類は替えられない。出力の読み方と再試行が 6 か所に残る
- **Codex のアダプターもいま作る**: オーナー の判断で見送り。使わないアダプターは試せず腐る。形（表と入口）だけ先に固める
- **API 直（SDK）に寄せる**: 実装の工程は Claude Code のツール（ファイル編集・コマンド実行）に頼っている。ツールなしの判定だけなら安くなりうるので、runner の候補として表に残す

## 結果

- 工程のコードから `claude` とモデル名が消え、`agents.yaml` を見れば「どの工程を何が動かしているか」が分かる
- 製造の来歴の `model` は、表で解決したモデル名が入る（いままでどおり）
- ワーカーの実装・要求の確認は Engine の clone が 1 回増える（浅い clone で数秒）
