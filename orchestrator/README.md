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
