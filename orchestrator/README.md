# orchestrator — P0 の制御プレーン（Docker Compose）

Issue にラベルを付けると、デスクトップ操作なしでコンテナ上の Claude Code が PR を出す経路。判断は [ADR 0003](../docs/adr/0003-p0-control-plane-postgres-queue.md)。

```
GitHub Issue（factory:implement）
   │ poll 30s / webhook
   ▼
dispatcher ──claim(SKIP LOCKED)──▶ postgres（jobs / events / artifacts）
   │ docker run --rm                      ▲
   ▼                                      │ 結果・usage・ログの場所
worker（浅いクローン → claude -p → push → gh pr create）──ログ──▶ objectstore（S3 互換）
```

## 動かす

```bash
cd orchestrator
cp .env.example .env          # CLAUDE_CODE_OAUTH_TOKEN と GH_TOKEN を入れる（builder が自分で発行）
docker compose build
docker compose up -d
docker compose logs -f dispatcher
```

- 対象リポジトリに工場のラベルを作っておく: `scripts/labels.sh guccyon/rally`
- Issue に `factory:implement` を付けると 30 秒以内にジョブになる。進み具合は Issue のラベルとコメントに出る

## ワーカーのイメージ

- `worker/Dockerfile`: 基本（git・gh・Claude Code・yq）
- `worker/Dockerfile.rust`: 基本 ＋ Rust ツールチェーン（cargo / rustfmt / clippy、C ツールチェーン）。`docker build -t factory-worker:rust -f worker/Dockerfile.rust worker`
- `worker/Dockerfile.flutter`: 基本 ＋ Flutter（stable。flutter analyze / flutter test まで。Android / iOS の SDK は入れない）。`docker build -t factory-worker:flutter -f worker/Dockerfile.flutter worker`
- どのリポジトリにどのイメージを使うかは `.env` の `WORKER_IMAGES`（例: `guccyon/rally=factory-worker:rust`）
- Issue ごとに変えるときは、Issue に `factory:image:<名前>`（例: `factory:image:flutter`）のラベルを付ける。リポジトリの設定より優先する

## 手動操作

```bash
docker compose exec dispatcher factoryctl list          # 進行中と直近 1 日
docker compose exec dispatcher factoryctl stop 12       # 停止（コンテナを kill）
docker compose exec dispatcher factoryctl rerun 12      # 同じ Issue で attempt+1 を投入
docker compose exec dispatcher factoryctl enqueue guccyon/rally 3   # ラベルを使わずに投入
docker compose exec dispatcher factoryctl events 12     # そのジョブのイベント
```

## 試験運転（GitHub にも Claude にも触らない）

`.env` に `WORKER_DRY_RUN=20` を入れて上げると、ポーリングとラベル操作を止め、ワーカーは 20 秒数えて偽の結果を返す。
停止・再実行・二重実行抑止の確認に使う。本番の値では `WORKER_DRY_RUN` を空にする。

## intake → clarify（要求の受付と精緻化、T20）

要求者（CS・マーケ・経営）の曖昧な要求を、短い往復で「確定した要求」（Request: 何を・何のために・いつまでに・どの精度で）にする最小の経路。Gen1 の要求者は架空のペルソナ（`worker/personas/requesters.yaml`）をエージェントが演じる。

- Issue に `factory:request` と、要求者を表す `requester:<id>`（例: `requester:cs-1`。なければ cs-1）を付ける
- dispatcher が Request（raw）を artifacts に残し、clarify のジョブを積む。ワーカーは `worker/clarify.sh` で動く（リポジトリはクローンしない）
- 問い手（tier-judge）が足りない項目を 1 つずつ Issue のコメントで聞き、ペルソナ（tier-small）が答える。最大 3 往復
- 4 項目が埋まれば Request（clarified）を artifacts に残してラベルを `factory:clarified` に。埋まらなければ failed（unresolved）
- events に `request.received` / `request.clarified` / `request.unresolved`（往復回数）。triage と Deliver への接続は P4

## 操業停止の記録と番人（ADR 0005）

工場が止まった・遅れた事例を Stoppage として `events` に記録する（`stoppage.opened` / `stoppage.closed` を key で対にし、view `stoppages` で読む）。既存の DB には `postgres/init/002_stoppages.sql` を `psql -f` で当てる。

- 番人は dispatcher の中で 60 秒ごとに制御プレーンだけを見る（トークンを使わない）。開く条件（閾値は仮置き）:
  - `idle:` 実行中 0 かつ投入可能 0 が 20 分（`WATCH_IDLE_MIN`）。ジョブが積まれたら閉じる
  - `heartbeat:` supervisor の heartbeat が 30 分ない（`WATCH_HEARTBEAT_MIN`。heartbeat が 1 度でも来てから有効）
  - `job:<id>` 予算・時間の超過と人の手当てを待つ失敗（使用量上限・時間切れ・ターン上限は budget、要求が確定しないは ticket、その他は factory-bug）。同じ Issue の次のジョブが始まったら閉じる
  - `stuck:<id>` 打ち切りの時刻を過ぎても running のまま
- 通知は 1 経路。`NOTIFIER=github-issue` で `STATUS_REPO` の工場状況 Issue（ラベル `factory:status`。なければ作る）に開始と終了をコメントし、本文を開いている停止の一覧に書き換える。`NOTIFIER=log` はログだけ
- 番人に見えない停止（builder の端末の許可待ち、契約の上限など）は手で記録する。週次レポートの `stoppages.md` はレポートの出力にする

```bash
docker compose exec dispatcher factoryctl stoppage open perm "gh pr merge が許可待ち" --waiting-for "Tetsuro の許可" --key S17
docker compose exec dispatcher factoryctl stoppage close S17 --remedy "許可リストに追加" --prevention "1 Bash 1 コマンド"
docker compose exec dispatcher factoryctl stoppage list --open
docker compose exec dispatcher factoryctl report stoppages --from 2026-10-20 --to 2026-10-27   # Markdown（既定は直近 7 日、Asia/Tokyo）
```

## 工場長の supervisor（ADR 0004）

判断は持たず、状態と表を見て工程を起動するだけの常駐プロセス（compose の `supervisor`。dispatcher と同じイメージ）。60 秒ごとに:

- `supervisor.heartbeat` を出す（途絶えたら番人が停止を開く）
- 工場の PR が `mergeable_state=clean`（CI 通過・衝突なし・ドラフトでない）になったら、変更ファイルを `definitions/change_type_rules.yaml` で変更種別に写し、製品の manifest を引いて `supervisor.merge_decided` を記録する。表が承認を求めれば PR に `factory:needs-approval`。表が auto で、かつ工場の権限（`phase_grants` の merge）も auto のときだけ squash でマージする。それ以外は記録だけ（shadow）
- マージを判定した head ごとに検査ジョブ（kind=inspect）を積む。dispatcher がワーカーの `inspect.sh` で `core-gates/shadow_review.py`（Engine の main から毎回取る）を動かし、`gate.evaluated`（mode=shadow）と InspectionReport を残す。GitHub には書かない。`SUPERVISOR_INSPECT=0` で止める
- 判定した PR が閉じたら `pr.merged`（誰がマージしたか）/ `pr.closed_unmerged` を記録する。判定と人の判断の一致率の材料
- `factory:planned` の Issue の本文の「依存: #12 #13」がすべて閉じたら `factory:implement` に替える。同時に流すのは `SUPERVISOR_MAX_INFLIGHT`（既定 1。実行中・投入済みのジョブと開いている工場の PR の合計）まで

`SUPERVISOR_DRY_RUN=1` で GitHub に書かずに判定だけ記録する。

## 来歴とロット（ADR 0007）

製造の来歴（Engine の版、ワーカーのイメージ、製品の既定ブランチの先頭、モード、モデルの tier）を `job.started` と PR の成果物の `payload.provenance` に付ける。属性はすべて optional で、取れなければ `unknown`。欠けてもラインは止めない。
ロット＝Release は後付けで、前の Release 以降にマージされた工場の PR をまとめて含める。利用者の報告や不具合は版から PR、製造の来歴へたどる。

```bash
docker compose exec dispatcher factoryctl release create guccyon/rally 0.1.0      # --prs URL,URL で明示もできる
docker compose exec dispatcher factoryctl report provenance --from 2026-10-20      # 来歴の付与率と Release への紐付け率
```

## ジョブ用の DB

dispatcher はジョブごとに制御プレーンと同じ Postgres に `job_<id>` を作り、ワーカーに `DATABASE_URL` で渡す（終了時に落とす）。ワーカーは自前で DB を立てずに DB のテストを動かせる。`JOB_DB_ENABLED=0` で止められる。

## 守っていること

- 状態は postgres とオブジェクトストアにだけある。dispatcher を落として上げ直しても続きから動く
- ワーカーは `--rm`。浅いクローンで、終了時に何も残さない。人間の手元の worktree 規約は持ち込まない
- 二重実行は DB の部分ユニーク索引とアドバイザリロックの 2 段で抑止する。ラベルは表示用
- 権限は PR 作成まで。ワーカーはマージしない
- 秘密情報は `.env` だけ。リポジトリに入れない

## まだないもの

- トンネル（Webhook を使うときに builder が cloudflared などを足す。既定はポーリング）
- 予算の途中停止（`claude -p` は止められないので max-turns で切り、超過を記録するだけ）
- テレメトリの統合（`health/telemetry/pr_metrics.py` の出力を `events` に流す経路は次）
- イメージの更新（CI でビルドしてレジストリから取る）。いまは手元で重ねて作り直している
