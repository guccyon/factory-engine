# ADR 0015: PR の見張り（PR の動きを抽象化したイベントにする）

- 状態: 承認（2026-10-31 オーナー。停止調査スレッド）
- 関係: ADR 0004（supervisor）、ADR 0005（番人）、ADR 0014（検査役の承認）、`definitions/events.yaml`

## 文脈

工場の PR は、作られた後に GitHub 側で動く（CI、衝突、レビュー）。その動きを拾って次の工程を起こすのはオーケストレーター（supervisor）の仕事だが、これまで supervisor 自身が毎分 GitHub に問い合わせていた（PR の状態、check-runs）。

オーナーの方針（2026-10-31）:

- 商用化の時点では、途切れた作業の再開はイベント駆動を前提にする
- オンプレミスでの提供もありうるので、ポーリングでイベント化する方式も残す
- オーケストレーターに全部やらせると責務が増える。PR を見張り（Webhook を受ける・ポーリングで見に行く）、動きを見つけたらオーケストレーターに抽象化したイベントとして発行するモジュールを分ける。決定論的に動くものにする

## 決定

1. **PR の見張り（`dispatcher/pr_events.py`）を置く**。工場の PR の動きを、GitHub の今の状態（snapshot: head、check-runs、mergeable_state、レビュー）から決定論的にイベントへ写す。LLM は使わない
2. **入口は 2 つで、どちらも「見に行く合図」**。Webhook（即時。dispatcher の `/webhook` が `pull_request` / `pull_request_review` / `check_suite` / `check_run` / `workflow_run` を受ける）と、ポーリング（`PR_EVENTS_INTERVAL_SEC`、既定 60 秒）。Webhook の payload を事実として使わず、必ず snapshot を取り直す。これで両方を同時に動かしても判断が揺れない
3. **同じ事実は 1 回だけ発行する**。dedupe_key は PR と head（レビューは review_id）ごと。入口がどちらでも、再起動しても、二重にならない
4. **イベントは 5 つ**: `pr.head_updated`、`pr.ci_failed`、`pr.ci_passed`、`pr.conflicted`、`pr.review_submitted`。マージと閉じは supervisor が判定の記録と一緒に残すので（`pr.merged` / `pr.closed_unmerged`）、見張りは発行しない
5. **オーケストレーターは GitHub を見ずにイベントを読む**。まず CI 落ち・衝突の停止（`ci:`）をイベントから開くようにし、次に実装役のセッションの再開（CI 落ち・指摘で同じ担当を起こす）の合図にする
6. **見張りは dispatcher の中で動く**（番人と同じ）。dispatcher が落ちれば止まるが、ポーリングが次の起動で取りこぼしを拾う。商用では Webhook を主、ポーリングを保険にする（`PR_EVENTS_SOURCE=both`）

## 選ばなかった案

- **supervisor が GitHub を見続ける**: オーケストレーターの責務が増え、イベント駆動（Webhook）に切り替えるときに supervisor を書き換えることになる
- **Webhook だけ**: 届かなかった通知（Mac に外からの URL がない、ネットワークの切断）を拾えない。オンプレミスでも使えない
- **Webhook の payload をそのまま事実にする**: 入口によって判断が変わり、重複や順序の問題が出る。合図とし、事実は取り直す
- **別のコンテナにする**: いまは dispatcher の中で足りる。分けるのは dispatcher の再起動と独立させたくなったとき

## 結果

- supervisor の `ci_failure_step` は GitHub に問い合わせず、見張りのイベントから停止を開く・閉じる
- Webhook を使うには GitHub 側の設定（イベントの種類に PR・チェック・ワークフローを足す）と、外から届く URL が要る。手元の Mac ではポーリングで動く
