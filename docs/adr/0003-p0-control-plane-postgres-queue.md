# ADR 0003: P0 の制御プレーン（Postgres 単体のキュー）とヘッドレスランナー

- 状態: 承認（2026-10-25。ローカル Mac 構成の細部は オーナー の委任により工場側で決めた）
- 関係: 憲章 04 節「構造条件：状態はサービスに、ワーカーは使い捨て」、開始定義 第 1 節「実行基盤の約束」、第 3 節 Week 3、Blueprint `runtime:`

## 文脈

P0a の完了条件は「Issue にラベルを付けると、デスクトップ操作なしでコンテナ上の Claude Code が PR を出す経路が 1 本通り、手動停止・再実行・二重実行の抑止が確認できる」こと。
守る約束は 3 つ。状態（キュー・ロック・成果物メタデータ・テレメトリ）をローカル FS に置かない、ワーカーは 1 タスク 1 コンテナで使い捨て、制御プレーンとワーカーを分けてステップを冪等にする。
建設場所は P0〜P1 はローカル Mac の Docker Compose。

## 決定

1. **制御プレーンは Postgres 単体**。`jobs` を `SELECT … FOR UPDATE SKIP LOCKED` で取り出すジョブキュー、`pg_try_advisory_lock(hashtext(repo#issue))` による所有ロック、`events`（テレメトリ）、`artifacts`（成果物メタデータ）を 1 つの DB に持つ。オブジェクト（ワーカーのログ）は S3 互換のオブジェクトストア 1 サービス。開始定義は MinIO を想定していたが、2026-10-26 時点で MinIO の公式イメージが Docker Hub から取得できなくなっていたため versitygw（posix バックエンド）を使う。クライアントは S3 API なので差し替え可能
2. **二重実行の抑止は 2 段**。DB の部分ユニーク索引（同じ repo#issue で `queued|running` は 1 件だけ）と、実行時のアドバイザリロック。GitHub 側のラベル（`factory:implement` → `factory:queued` → `factory:running` → `factory:pr-open | factory:failed`）は人間向けの表示で、抑止の根拠にしない
3. **起動はポーリングを既定にする**。dispatcher が 30 秒ごとに対象リポジトリの `factory:implement` ラベルの Issue を見る。Webhook 受け口（`/webhook`、HMAC 検証付き）も同じプロセスに持ち、`WEBHOOK_SECRET` を設定したときだけ有効にする。トンネル（cloudflared 等）は Webhook を使うときに builder が足す
4. **ワーカーは使い捨てコンテナ**。dispatcher が Docker ソケット経由で `docker run --rm` し、ワーカーは浅いクローン（`--depth 1`）→ Claude Code（`claude -p`）→ commit / push → `gh pr create` → 結果を標準出力の最終行に JSON で出して終了する。ワーカーの中に状態を残さない。ログは dispatcher がオブジェクトストアに置く
5. **手動停止と再実行**は `factoryctl stop <job>`（cancel_requested を立て、dispatcher がコンテナを kill）と `factoryctl rerun <job>`（同じ correlation_id で attempt を増やして再投入）
6. **権限は P0 の autonomy のとおり pr_create まで**。ワーカーの GitHub トークンは PR 作成とブランチ push だけに使い、マージしない。モデルは tier 名で指定し、ワーカーが具体名に写像する
7. **同時実行とトークン予算**は Blueprint の値から始める。`MAX_PARALLEL=2`（Blueprint の llm_max_parallel 4 の半分から。実測で上げる）、`MAX_TURNS=40`、`TOKEN_BUDGET_PER_JOB=2000000`。`claude -p` は途中で予算停止できないので、P0 は max-turns で上限を切り、結果の usage を予算と比べて `over_budget` を記録する
8. **耐久ワークフロー（Temporal 等）は入れない**。ワークフローは dispatcher の中の薄いインターフェース（enqueue / claim / finish / cancel）で切り、裏を差し替えられるようにする。採否の判断基準は下記
9. **実装言語は Python**（dispatcher / factoryctl）と shell（ワーカーの entrypoint）。Gen1 はプロトタイプで Engine ごとリライトする前提（憲章 02 節）なので、立ち上げの速さを優先する。Gen2 の Engine で Rust を中心軸にするかは、そのときの ADR で決める

## Temporal（耐久ワークフロー）の採否基準

次のいずれかが実測で出たら P4〜P5 で採用を検討する。それまでは Postgres 単体で足りる。

| 基準 | Postgres 単体で苦しくなる兆候 |
|---|---|
| 長時間待機 | 人間の Approval / Question の回答待ちが日単位になり、待機中のジョブの状態機械を自前で書く量が増える |
| 再開性 | ワーカーの途中失敗からの再開を「最初からやり直す」で済ませられなくなる（1 ジョブが 30 分を超える、外部副作用が多段になる） |
| 並列度 | 同時実行が 10 を超え、コンテキストごとのマージ直列化とレート制限の制御を dispatcher 1 プロセスで持てなくなる |
| 運用負荷 | 失われたジョブ・二重実行・タイムアウトの調査が週に複数回起きる |

## 選ばなかった案

- **GitHub Actions をランナーにする**: 起動は簡単だが、状態が Actions のログと Issue に散り、手動停止・再実行・二重実行抑止を自前で制御できない。Max 契約の OAuth トークンを GitHub の Secrets に置くことにもなる
- **Temporal を最初から入れる**: 他 LLM レビューの指摘どおり P0 では過剰。Compose のサービスが 3 つ増え、学ぶことが増える
- **Webhook ＋トンネルを既定にする**: ローカル Mac では URL が変わり、GitHub 側の設定を繰り返す。ポーリング 30 秒で P0 の目的（デスクトップ操作なしで PR が出る）は満たせる
- **dispatcher をホストで直接動かす**: Compose に揃えるほうが「すべて compose / IaC に書き、手作業の構成を残さない」に合う。Docker ソケットのマウントが必要になるが、ローカル Mac の P0 では許容する

## 結果

- 秘密情報（Claude Code の OAuth トークン、GitHub トークン）は `.env` にだけ置き、リポジトリに入れない。トークンは builder が自分で発行して置く（`claude setup-token`、GitHub の fine-grained PAT）
- ワーカーのイメージは `node:22-bookworm-slim` に git・gh・Claude Code CLI を入れたもの。iOS ビルドは P0 対象外
- 成果物ストアができたので、Week 2 のテレメトリ（`health/telemetry/pr_metrics.py`）の出力と、ワーカーが返す usage を `events` に流す経路を次に作る
