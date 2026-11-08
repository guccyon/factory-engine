# orchestrator — P0 の制御プレーン（Docker Compose）

Issue にラベルを付けると、デスクトップ操作なしでコンテナ上の Claude Code が PR を出す経路。判断は [ADR 0003](../docs/adr/0003-p0-control-plane-postgres-queue.md)。

```
GitHub Issue（factory:implement）
   │ poll 30s / webhook
   ▼
dispatcher ──claim(SKIP LOCKED)──▶ postgres（jobs / events / artifacts）
   │ docker run（--rm なし、ADR 0020）    ▲
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
- `worker/Dockerfile.flutter`: 基本 ＋ Flutter（3.47.8 に固定。flutter analyze / flutter test まで。Android / iOS の SDK は入れない）。`docker build -t factory-worker:flutter -f worker/Dockerfile.flutter worker`
- `worker/Dockerfile.design`: 基本 ＋ ヘッドレスの Chromium と日本語の書体（Noto CJK）。デザイナー役（ADR 0025）が案のモックを撮る。`docker compose build worker worker-design`（`factory-worker:design`。`.env` の `UI_DESIGN_IMAGE` で替えられる）
- どのリポジトリにどのイメージを使うかは `.env` の `WORKER_IMAGES`（例: `guccyon/rally=factory-worker:rust`）
- Issue ごとに変えるときは、Issue に `factory:image:<名前>`（例: `factory:image:flutter`）のラベルを付ける。リポジトリの設定より優先する

## 手動操作

```bash
docker compose exec dispatcher factoryctl list          # 進行中と直近 1 日
docker compose exec dispatcher factoryctl stop 12       # 停止（コンテナを kill）
docker compose exec dispatcher factoryctl rerun 12      # 同じ Issue で attempt+1 を投入（最初から作り直す。続きのジョブは続きとして積み直す）
docker compose exec dispatcher factoryctl rerun 12 --continue   # 途中終了のドラフト PR のブランチから続ける（ADR 0016）
docker compose exec dispatcher factoryctl enqueue guccyon/rally 3   # ラベルを使わずに投入
docker compose exec dispatcher factoryctl events 12     # そのジョブのイベント
docker compose exec dispatcher factoryctl drain --wait 3000   # ラインを一時停止して、更新してよくなるまで待つ（ADR 0020）
docker compose exec dispatcher factoryctl resume        # ラインを再開
docker compose exec dispatcher factoryctl version       # サービスごと・ジョブごとに動いている版（ADR 0031）
```

## 続きのジョブと引き継ぎのボリューム（ADR 0016・0017）

工場の PR に合図が来たら、同じチケットの実装役を続きから動かし、同じブランチに push する。人に回すのは、直せなかったときと上限に達したときだけ。

| 合図 | 積む役 | 上限 | 直せなかったら |
|---|---|---|---|
| 衝突（mergeable_state=dirty） | supervisor | head ごとに 1 回 | 停止 `ci:` |
| CI 落ち（PR の見張りの `pr.ci_failed`） | supervisor | PR ごとに `CONTINUE_MAX` 回（既定 2） | 停止 `ci:` |
| 検査役がマージ前に採用した指摘（本文だけの指摘を除く） | dispatcher（judge の後） | PR ごとに `CONTINUE_MAX` 回 | 差し戻しの Issue（ADR 0006） |

実装役の作業の文脈は、チケットごとの Docker のボリューム `factory-ticket-<owner>-<repo>-<issue>` に残る（ワーカーの `/handoff`）。

```bash
docker run --rm -v factory-ticket-guccyon-rally-249:/h alpine cat /h/ticket.json   # ジョブの履歴・ブランチ・PR
docker run --rm -v factory-ticket-guccyon-rally-249:/h alpine ls -R /h/jobs        # ジョブごとの報告・仮決め・合図・差分
```

`agent/` の下はエージェントの会話の記録（Claude Code の `--resume` に使う近道）で、なくても続きは作れる。PR がマージか閉じられたら、後片付け（ADR 0021）が 5 分ごとにボリュームをそのまま tar.gz にしてオブジェクトストアの `tickets/<repo>/<issue>/handoff-<日時>.tar.gz` に置き（`handoff.archived`）、ボリュームを消す。ラインとは別の糸で動き、PR には何も足さない（`TIDY_ENABLED=0` で止める）。置けないまま `HANDOFF_KEEP_DAYS` 日（既定 7）たったボリュームは dispatcher が消す。`/handoff` を持たない古いワーカーのイメージでは、ボリュームなしで続ける（ラインは止めない）。

## 工場の更新（ADR 0020）

ワーカーのコンテナは dispatcher から独立して動く（`--rm` なし。名前 `factory-job-<id>`、ラベル `factory.job=<id>`）。dispatcher を上げ直すと、
running のジョブのコンテナにつなぎ直し（`job.reattached`）、出力を最初から読み直して終わりを待ち、いつもどおり記録する。コンテナが残っていないジョブだけ
`orphaned_by_restart` で積み直す。コンテナが残っていてもシグナル（終了コード 137・143）で終わっていたもの（Docker ごと止まった）は `killed_by_shutdown` で積み直す。終わったコンテナは dispatcher が記録を終えてから消す。

更新方は 2 通り:

- **工場のコードだけ**（dispatcher・supervisor・console・shipper、ワーカーのイメージの作り直し）: 一時停止は要らない。実行中のジョブはそのまま続く。出荷の処理中だけは shipper を上げ直さない（`drain status` で見る）

  ```bash
  git pull
  scripts/build.sh                 # イメージを依存の順に作る（土台 → 派生）。版のラベルと ENGINE_VERSION を焼き込む（ADR 0031）
  docker compose up -d             # すべてのサービスを上げる（一部だけ上げない）
  docker compose exec dispatcher factoryctl version --expect "$(git rev-parse --short HEAD)"   # どのサービスも新しい版で動いているか（ずれたら終了コード 1）
  ```

  - `docker compose build` や `up -d --build` は使わない。ワーカーのイメージを作らず、派生のイメージ（shipper・design）が古い土台に乗ることがある
  - `.env` に `ENGINE_VERSION` を書かない（古い値が残って来歴を誤る。イメージに焼き込んだ値が使われる）

- **制御プレーンや Mac ごと**（postgres・オブジェクトストア・Docker・OS の再起動）: ラインを一時停止して、更新してよくなるまで待つ

  ```bash
  docker compose exec dispatcher factoryctl drain --reason "postgres の更新" --wait 3000   # 更新してよければ終了コード 0
  # 更新する
  docker compose exec dispatcher factoryctl resume
  ```

- 一時停止の間、dispatcher は順番待ちを取り出さず、割り込みもしない。ラベルでの投入、supervisor の判定・マージは続く。一時停止は上げ直しをまたいで残り、再開は明示の操作だけ
- 「更新してよい」は、一時停止中で、実行中のジョブが 0 件で、出荷の依頼が処理中でないとき（`factoryctl drain status`）
- 操業コンソールの上の「ライン」のトグルでも同じ操作ができる（API は `POST /api/factory/drain`・`/api/factory/resume`、状態は `GET /api/state` の `factory.line`）
- 再開を忘れたら、番人が `WATCH_DRAIN_MIN` 分（既定 30）で停止 `drain:` を開く（分類 human）。再開すると閉じる

### 更新役に任せる（ADR 0031）

上の手順を、dispatcher が更新のたびに起動する使い捨ての更新役（コンテナ `factory-swap-<id>`、`--rm`）が行う。手で打つのは 1 行だけ:

```bash
docker compose exec dispatcher factoryctl swap run main      # main の先頭に更新する（コミットも渡せる。main に入っているものだけ）
docker compose exec dispatcher factoryctl swap status <id>   # 段ごとの記録。dispatcher を上げ直す間は答えないので、ホストでは docker logs -f factory-swap-<id>
```

- 事前の確かめ（ラインは止めない）: 作業ツリーがきれい、`target` が main に入っていて今の版から早送りできる、差分から種類を判定し直す（`definitions/swap.yaml`。申告より軽くしない）、
  出荷の処理中でない、新しい版の `compose.yaml` が今の `.env` で通る。今のイメージに前の版の名前（`factory-dispatcher:p0-<コミット>` など）を付けてから、新しい版の `scripts/build.sh` でイメージを作る
- `control_plane`（`compose.yaml` の変更）だけ一時停止し、更新してよくなるまで待つ。`code` は一時停止しない（つなぎ直し）
- 作業ツリーを早送りし、`docker compose up -d --no-build --remove-orphans` ですべてのサービスを上げる
- 確かめ（既定 5 分）: すべてのサービスが新しい版で動きヘルスチェックが通る、`dispatcher.started` と `supervisor.heartbeat` が新しい版で出る、コンソールと窓口が応える、実行中だったジョブがつなぎ直された
- 通らなければ前の版（作業ツリーと前の版の名前のイメージ）に戻して確かめ直す。戻せたら再開し、受信トレイに建設者への知らせを出す（同じ版では自動で更新しない）。戻しても通らなければ一時停止のまま停止 `swap:<id>` で人に回す
- 人に回すもの: DB の変更（`postgres/init/`）を含む版は事前の確かめで止め、受信トレイに流す SQL を出す（承認ボタンで流すのは順番 5。それまでは一時停止して手で流し、`swap run` をやり直す）。
  秘密情報・権限を広げる変更（ソケットのマウント・トークン・ポートを外に開く）、今の `.env` に足りない環境変数も人へ
- 更新役は作業ツリーを同じ絶対パスでマウントする（場所は compose が付けるラベルから読む。`SWAP_ROOT` で上書き）。`git worktree` の作業ツリーなら、指している git の置き場もマウントする（足りなければ `SWAP_EXTRA_MOUNTS`）。
  更新役のコンテナには ssh の鍵がないので、`origin` が ssh でも https で取り込む（工場のリポジトリは公開）
- 更新役を含む版を初めて入れるときは、更新役がまだないので上の手順（`build.sh` → `up -d` → `version --expect`）で入れる。次から `swap run` が使える
- `SWAP_ENABLED=0` で dispatcher は更新役を起動しない（郵便受けも見ない）

#### Issue で依頼する（郵便受け。ADR 0031 の順番 3）

クラウドの建設者は工場のマシンの中に届かないので、factory-engine に Issue を立てて `factory:swap` を付ける。dispatcher が 1 分ごとに拾い、依頼にして「いま」で受ける。
進み具合（受けた・起動した・一時停止した・結果）は同じ Issue にコメントで返り、更新したら閉じる（`factory:swapped`）。止まったら `factory:swap-failed` を付けて開いたまま。

```
target: main            # main かコミット（main に入っているもの）。省けば main
kind: code              # code / control_plane / db（自己申告。差分から判定し直し、重い方を採る）
urgency: normal         # normal / urgent
deadline: 2026-11-06T18:00+09:00   # 記録だけ（時刻の提案は順番 4）
```

- 拾うのは、ラベルを付けたのが `SWAP_REQUESTERS`（カンマ区切りの GitHub のログイン。既定は郵便受けのリポジトリの持ち主）のものだけ。ほかの人が付けたラベルは黙って飛ばす（リポジトリは公開）
- 本文は上の形の行だけ読み、値も決まった形（`main` か 16 進のコミットなど）に当たるものだけ受ける。ほかの文は記録だけで、シェルにもプロンプトにも渡さない
- 同時に 1 件。ほかの更新が終わっていなければ、その旨を 1 回返して待つ
- 一時停止を伴う更新（申告か判定が `control_plane`・`db`）は、人が並走する時間帯（attended。`FACTORY_MODE`・`MODE_SCHEDULE`・`factoryctl mode`）だけ始める（決めどころ S4）。
  申告が `code` で判定が重かったときは、更新役が何も変えずに止め、attended に入ったら同じ Issue で依頼し直す。手で打った `swap run` は時間帯で止めない
- 人に回す条件（DB の変更・秘密情報と権限など）は手で打つときと同じ。更新役を起動する前に Issue を閉じれば取り消す
- 返すコメントには工場のマシンのパスとホスト名を出さない。`SWAP_MAILBOX_REPO`（既定 `guccyon/factory-engine`）・`SWAP_MAILBOX_INTERVAL_SEC`（既定 60）・`SWAP_MAILBOX=0` で止める。
  工場の GitHub のトークンに、郵便受けのリポジトリの Issues の読み書きが要る
- 時刻の合意（建設者の依頼にプランナーが時刻を提案する）は順番 4、窓口の `builder` の役割（`POST /desk/swaps`）は窓口を外に開くとき（表の役割の制約を変える DB の変更を伴う）

## 試験運転（GitHub にも Claude にも触らない）

`.env` に `WORKER_DRY_RUN=20` を入れて上げると、ポーリングとラベル操作を止め、ワーカーは 20 秒数えて偽の結果を返す。
停止・再実行・二重実行抑止の確認に使う。本番の値では `WORKER_DRY_RUN` を空にする。

## intake → clarify（要求の受付と精緻化、T20）

要求者（CS・マーケ・経営）の曖昧な要求を、短い往復で「確定した要求」（Request: 何を・何のために・いつまでに・どの精度で）にする最小の経路。Gen1 の要求者は架空のペルソナ（`worker/personas/requesters.yaml`）をエージェントが演じる。

- Issue に `factory:request` と、要求者を表す `requester:<id>`（例: `requester:cs-1`。なければ cs-1）を付ける
- dispatcher が Request（raw）を artifacts に残し、clarify のジョブを積む。ワーカーは `worker/clarify.sh` で動く（リポジトリはクローンしない）
- 問い手（tier-judge）が足りない項目を 1 つずつ Issue のコメントで聞き、ペルソナ（tier-small）が答える。最大 3 往復
- 4 項目が埋まれば Request（clarified）を artifacts に残してラベルを `factory:clarified` に。埋まらなければ failed（unresolved）
- 確定したら、採点役（`clarify.grader`、tier-judge）が確定した要求と架空の要求者の隠れた意図を比べる。問い手が推測で埋めて確定にしたものを見分けるため。本物の要求者（隠れた意図がない）は採点しない
- events に `request.received` / `request.clarified` / `request.unresolved`（往復回数、`converged`、`intent_match`、食い違った項目 `missed`）。`converged` は「確定・往復 3 回以内・意図と一致」で、P4 の完了条件の物差し（`definitions/roadmap.yaml` の `clarify_3_rounds`。ペルソナごとの最新の結果で 8 人以上）
- ペルソナは 10 人（CS・サクセス・マーケ・営業・経営・本部の運営・コーチ担当）。前半 5 人は数字を知りたい問い、後半 5 人は経営の問いと画面や機能への要望。全員を一巡させるのは `scripts/clarify-bench.sh guccyon/rally`（1 人ずつ要求の Issue を起票する。ペルソナを足したらワーカーのイメージを作り直す）
- triage と Deliver への接続は P4

## プランナー（ADR 0025・0026。着手判定と経路）

チケットの入口。役の一覧・よくある並び・決まりは `definitions/planner.yaml`。

- Issue に `factory:plan` を付けると、dispatcher が `plan` のジョブに積む。プランナー（`worker/plan.sh` → `core-gates/plan.py`、tier-small）が、Issue の本文とコメント、製品の `specs/`（デザインシステムの有無）と `.factory-config/knowledge/`（プロダクトの既定）を読み、着手判定と経路を返す。読むだけで、何も書き換えない
- 経路は型から選ばず、タスクに合わせて「どの役をどの順で通すか」の並び（`steps`）として組む。役は `design_system`（デザインシステムのデザイナー）・`ui_design`（プロダクトデザイナー）・`implement`（実装役）。よくある並び（実装だけ、画面 → 実装、デザインシステム → 画面 → 実装、デザインシステムだけ）は例で、縛りではない。役ごとに focus（何をさせるか）・why・effort（手間の見込み）を書く
- 指示役は最初の役のラベル（`factory:design-system`・`factory:design`・`factory:planned`）で配る。デザインシステムの仕事の後にほかの役が続く経路は、デザインシステムの仕事を別のチケットに切り（`plan.split`）、元のチケットは `factory:waiting-steps` で待つ。切ったチケットが閉じると、周回が次の役のラベルを付ける（`plan.advanced`）
- 決定論で当てるのは安全のための最小限（guards）だけ: デザインシステムがなく進め方の指定もないのにプロダクトデザイナーへ回す並びは問いに替える、確信度が低い・往復を使い切った・問いがない・経路が空なら人へ（`factory:plan-human`）。よくある例との食い違い（小さな改修にデザイナー、新しい画面にデザイナーなし、実装の後のデザイン）は印（advisories）を付けるだけで経路は変えない
- 着手できないときは、理由と選択肢つきの問いを Issue のコメントに書いて `factory:needs-info`。問いのコメントより後に返事が付くと、dispatcher の周回が判定し直しを積む（問いは 2 回まで）。Gen1 は建設者がオーナーの代理で答える
- 判断の根拠は後で見返せるように残す: artifacts の `Plan` に 3 層（根拠・解釈・確信度）、役ごとの理由、とらなかった並びと理由、当てた決まりと印、入力の版（Engine と製品のコミット、定義とプロンプトの版）。events の `plan.decided`（経路・手間・費用）・`plan.asked`・`plan.answered`。`factoryctl plan list` / `plan show owner/repo#123` で見る
- 型つきの上流の成果物（P4 の `typed_upstream`）: 着手できる判定（ready・split）では、プランナーが要件（受け入れ条件・やらないこと・できあがりの定義）と、このタスクが増やす（add）・変える（change）・守る（keep）ユーザーストーリーを出す。ストーリーの id は製品の `specs/stories/` と決定論で突き合わせ、ない id の変える・守るは落とす（印 `unknown_story`）、もうある id の増やすは変えるにする（印 `story_exists`）。dispatcher が artifacts の `Requirements` に残し、Issue の本文の節（`<!-- factory-requirements:v1 -->`）に書く（判定し直したら置き換える）。後の役（プロダクトデザイナー・実装役）は本文を読むので、増やす・変えるストーリーのファイルは実装役が同じ PR で `specs/stories/<id>.yaml` に書く。守るストーリーは QA 役が回帰で流す候補。実装の手前の判定（`phase=implement`）では出し直さない。値の形は `definitions/planner.yaml` の `upstream`。あわせて、このタスクが従う製品の ADR（`docs/adr/` の番号）と新しく残す判断（`ADR`、status proposed。実装役が同じ PR で `docs/adr/` に書く）、契約の差分（`ContractDiff`: 契約のパス・足す／変える／消す・互換への影響・操作）を出し、同じ節に書く。ADR の番号と契約の置き場は製品と決定論で突き合わせ、ないものは落とす（印 `unknown_adr`・`unknown_contract`）。消す差分は互換を壊すものとして扱い、互換を壊す差分には印 `breaking_contract`（expand → 移行 → contract、contract は人の承認）、契約の層を触る見積もりなのに差分がなければ印 `no_contract_diff`
- 今の入口（`factory:planned`・`factory:implement`・`factory:design`）はそのまま使える。新しいチケットは `factory:plan` から入れる
- 大きさで切る（ADR 0036 の 2）: 経路に実装役があれば、プランナーは実装役に回す仕事の大きさ（`size`: 触る層・ターンの帯・根拠）を見積もる。材料は dispatcher が入力に足す製品の実績の表（`sizes.md`。下の `plan sizes` と同じもの）。触る層が 3 つ以上（文書を除く。契約から生成した型は契約の層、試験や画面の撮影の値を合わせるだけの層は数えない）・変わる見込みのファイルが 30 を超える・帯が 60 ターンを超える、のどれかに当たれば、窓口の相談役と同じ型のタスクのまとまり（依存つき）に切る（`split`）。子は窓口と同じ起票（`desk_line.file_task`）で、依存のないものは `factory:plan`、あるものは `factory:desk-waiting`（依存が閉じたら `factory:plan`）。もとのチケットは `factory:waiting-steps` で待ち、子がすべて閉じたら周回が閉じる。切ったタスクがない・形が誤っていれば人へ（guard `oversized`）。実装役の前にデザインの役がある経路は、画面が決まってから切るので印（`split_after_design`）だけ付ける
- 実装の手前の判定（ADR 0036 の 3・6）: 大きいがデザインの役が前にあった（印 `split_after_design`）チケットは、画面が決まったら（デザインの線の引き渡し）`factory:planned` に戻さず、プランナーの判定を `phase=implement` でもう 1 度積む（`plan.pre_implement`）。経路は実装役だけにし、UXSpec を材料に見積もり直して、大きければ切る。親の依存（足りない部品の仕事など）は子にも付ける。途中終了の続き（`factoryctl rerun --continue`）は 1 回まで。続きも途中終了したら、`factoryctl plan resplit owner/repo#123` で残りをプランナーに切らせる（ドラフト PR の中身を仕上げるタスクを t1 にする。それでも続けるなら `--force`）
- 判定と結果の突き合わせ（ADR 0036 の 4a）: 工場の PR がマージされた・閉じたら、supervisor の周回が最新の Plan と実績（実装のジョブの数・ターン・費用・途中終了・続きの理由、PR で変わったファイルと層、見込みのファイルの再現率（分母は文書・試験・撮影の値を除いた本体のファイル）と適合率、経路の覆り、judge が人に回したか）を突き合わせて `plan.outcome` に残す（`plan_outcome.py`、決定的）。判定は `ok`・`undersized`（実績が切る基準を超えた・途中終了した）・`misrouted`（経路が覆った）・`oversized`（切った子が小さすぎた）。プランナーを通らなかったチケットも `planned=false` で残す。層と基準の値は `definitions/planner.yaml` の `sizing`
  - `factoryctl plan outcomes [--repo owner/repo] [--days 14]`: 突き合わせの一覧。`--backfill` で過去の PR も GitHub から引いて残す（初回に 1 回。`GH_TOKEN` が要る）。`--rescore` は層や再現率の数え方を変えたあとに、残した突き合わせを GitHub から引き直して組み立て直す
  - `factoryctl plan sizes [--repo owner/repo] [--days 60]`: 実績の表（層の組ごとのファイル数・ターン・途中終了・費用）。プランナーの見積もりの材料（順番 2 でプランナーの入力に渡す）
  - `factoryctl plan pass [--repo owner/repo]`: 合格線の実績の 4 つ（途中終了の率・ターンの中央値と 90 パーセンタイル・見込みのファイルの再現率・経路の覆りの率）を直近 14 日で測る。チケットが 10 件に満たなければ参考の値
- 固定の評価セットのベンチ（ADR 0036 の 4b）: `core-gates/plan_bench.py --repo-dir <製品の main の写し> --sizes sizes.json --out result.json` で、`eval-corpus/planner/cases.yaml` の各チケットをプランナーに判定させ、人が付けた正解（切るべきか・触る層・ターンの帯・経路）と比べる。切る／切らないの一致と、帯が ±1 帯の内の割合が合格線（`definitions/planner.yaml` の `sizing.bench`。12 件で 10 件以上）。切るべき件の当たりも別に出す（切らないだけのプランナーを見落とさないため。合格線ではない）。プランナーのプロンプト・基準・モデルを変えるときに回し、`--score result.json --baseline before.json` で前の版と比べてから入れる。1 件につきプランナーの呼び出しが 1 回

## デザインシステムのデザイナー役（ADR 0025・0026）

コンセプト・原則・ガイドラインから、トークン・部品・画面の型まで考える役。数値と決まりは `definitions/design_system.yaml`、何を基準化するかは `definitions/design_guideline.yaml`。

- Issue に `factory:design-system`（プランナーが付けるか、人か建設者が付ける）→ `design_system` のジョブ（`worker/design_system.sh` → `core-gates/design_system.py`、tier-large、`factory-worker:design` で動く）
- concept の段（デザインシステムがない製品、作り直し）: コンセプトとトーン＆マナーの案（2〜3）と、案ごとの見本の画面（今の代表の画面を案のトークンで描き直した静的 HTML とキャプチャ）を作る。案は `DesignConcept`、見本は `design/<job_id>/`。人の選択（`HumanChoice` の `concept_tone`。ブランドに効くので必ず人）に回して `factory:design-choice`
- 操業コンソールの「人の選択」で選ぶと、周回が spec の段を積む。「どれも違う」で戻すと、人の一言を付けて concept を作り直す
- spec の段（選ばれた案か、いまのデザインシステムへの追加）: デザインガイドラインの必須の節を `specs/design/` のファイル（principles・concept_tone・tokens・components・patterns・content・accessibility・governance）として書く。正本は `DesignSystemSpec` に残し、Issue の本文に足して `factory:planned`。実装役がファイルを置き、両アプリのテーマと基本の部品をトークンに合わせる（PR は今の検査と judge を通る）
- events: `design_system.concepts_drafted`・`awaiting_choice`・`choice_applied`（推す案との一致 `agreed`）・`handed_off`

## デザインの線（ADR 0025。デザイナー役とデザイン検査役）

画面を含むタスクは、実装の前に画面の要素と振る舞い（UXSpec）と案を決める。数値と観点は `definitions/ui_design.yaml`。

- タスク（Issue）に `factory:design` を付ける（プランナーが経路 `ui_design` で付けるか、人か建設者が付ける）
- dispatcher が `ui_design` のジョブに積む。デザイナー役（`worker/ui_design.sh` → `core-gates/ui_design.py`、`factory-worker:design` で動く）が、製品の `specs/design/`（デザインシステム）・用語集・今の画面を読み、UXSpec と 2〜3 案の静的 HTML を書いて Chromium で撮る（外への通信は止める）。案のファイルは `design/<job_id>/` に置き、UXSpec は artifacts（`UXSpec`）に残す
- 続けてデザイン検査役（`ui_design_review` → `core-gates/ui_design_review.py`）が、別の文脈で案の画像・HTML・製品の `specs/` を見て、受け入れ条件・状態・デザインシステム・文言・アクセシビリティ・案の違いの観点で 承認／直し／人へ を返す。判断は judge と同じ 3 層（根拠・解釈・確信度）で `DesignVerdict` に残す
- 人へ回す条件はエージェントの答えにかかわらず決定論で当てる: 新しい画面、ブランドと印象に効く画面、確信度 0.7 未満、直しを 1 回使った後の直し。must の指摘が残る案は承認しない
- 承認: 決まった案の UXSpec（とモックの HTML）を Issue の本文の節に足し、`factory:planned` に戻す。実装役は同じ PR で `specs/ux/issue-<番号>.yaml` に置く。デザインシステムに足りない部品（`design_system_gaps`）は別のタスクを起票して依存にする（画面に直書きさせない）
- 直し: 指摘と前の UXSpec を渡してデザイナー役を 1 回だけ積み直す
- 人へ: `factory:design-choice` を付け、Issue に案と推す案を書く。人は `factoryctl design list` / `show` / `export`（案のファイルを tar で）で見て、`factoryctl design pick owner/repo#123 <案>` で選ぶ。dispatcher が次の周回で Issue に反映する。検査役の推す案との一致を `ui_design.chosen` の `agree` に残す（委任を広げる判断の材料）。操業コンソールの選択の画面は P4-7

## 操業停止の記録と番人（ADR 0005）

工場が止まった・遅れた事例を Stoppage として `events` に記録する（`stoppage.opened` / `stoppage.closed` を key で対にし、view `stoppages` で読む）。既存の DB には `postgres/init/002_stoppages.sql` を `psql -f` で当てる。

- 番人は dispatcher の中で 60 秒ごとに制御プレーンだけを見る（トークンを使わない）。開く条件（閾値は仮置き）:
  - `idle:` 実行中 0 かつ投入可能 0 が 20 分（`WATCH_IDLE_MIN`）。ジョブが積まれたら閉じる
  - `heartbeat:` supervisor の heartbeat が 30 分ない（`WATCH_HEARTBEAT_MIN`。heartbeat が 1 度でも来てから有効）
  - `job:<id>` 予算・時間の超過と人の手当てを待つ失敗（使用量上限・時間切れ・ターン上限は budget、要求が確定しないは ticket、その他は factory-bug）。同じ Issue の次のジョブが始まったら閉じる
  - `stuck:<id>` 打ち切りの時刻を過ぎても running のまま
  - `stall:<id>` 実行中のジョブの出力が 15 分（`WATCH_STALL_MIN`）止まっている。出力が再開するかジョブが終われば閉じる（完了まで毎周期見る）
  - `merge:<PR>` supervisor が工場はマージしない（execute=false）と記録した PR が、30 分（`WATCH_MERGE_WAIT_MIN`）マージも閉じもされない。分類は human。マージか閉じられたら閉じる
  - `inspect:<PR>` マージを判定した最新の head に検査が積まれないまま 30 分（`WATCH_INSPECT_WAIT_MIN`）。分類は factory-bug（検査がないと検査役が承認せず、PR が同時に流す枠を塞ぐ）。検査が積まれる、head が動く、PR がマージか閉じられたら閉じる
- 停止ではない知らせ: 投入待ちの段（`factory:planned` を写したチケットのうち保留でないもの）が `WATCH_LINE_LOW_BELOW`（既定 2）を切ると、イベント `watcher.line_low` を残して工場状況 Issue に 1 度だけコメントする。積み足されて戻ると `watcher.line_ok`。停止の一覧と停止時間には数えない。操業コンソールの「着手待ちのチケット」も下限を切ると赤くなる
- 通知は 1 経路。`NOTIFIER=github-issue` で `STATUS_REPO` の工場状況 Issue（ラベル `factory:status`。なければ作る）に開始と終了をコメントし、本文を開いている停止の一覧に書き換える。`NOTIFIER=log` はログだけ
- 番人に見えない停止（builder の端末の許可待ち、契約の上限など）は手で記録する。週次レポートの `stoppages.md` はレポートの出力にする

```bash
docker compose exec dispatcher factoryctl stoppage open perm "gh pr merge が許可待ち" --waiting-for "オーナー の許可" --key S17
docker compose exec dispatcher factoryctl stoppage close S17 --remedy "許可リストに追加" --prevention "1 Bash 1 コマンド"
docker compose exec dispatcher factoryctl stoppage list --open
docker compose exec dispatcher factoryctl report stoppages --from 2026-10-20 --to 2026-10-27   # Markdown（既定は直近 7 日、Asia/Tokyo）
```

## 操業コンソールと優先度（ADR 0009）

工場のいまを見て、順番・保留・停止を操作する画面と API。compose の `console`（dispatcher と同じイメージ）で動き、制御プレーンだけを読み書きする。GitHub にも外部の CDN にも触らない。

- 開く: http://localhost:8790 （既定は Mac の中だけ。同じ LAN の端末から見るときは .env に `CONSOLE_BIND=0.0.0.0` と、`CONSOLE_ALLOWED_HOSTS` にそのホスト名（例 `localhost,127.0.0.1,host.example.local`）を入れて console を上げ直す。ログインがないので、LAN の中の誰でも優先度・保留・停止を操作できる。インターネットには出さない）
- 画面（ページの構成は 2026-11-07。`dispatcher/console_page.py`）: どのページも上に指示役（supervisor）の生存・モード・流量・今日の費用・着手待ちのチケット・ラインのトグルと、ページの切り替え（使用量も同じ画面の中）。右上の ? がヘルプ（`/help`。ヘッダの数字と言葉の意味）、歯車が設定
  - ホーム `/`（ダッシュボード）: 対応が必要なもの（受信トレイの未回答の上位 5 件）・止まっているもの（開いている停止のアラート。下に別記）・指標の推移（毎朝の操業レポート「工場の健康」が残した最新の `report.built` の `charts` の指標を 14 日分。数え直さないのでレポートと同じ数）・ラインと出荷の要約
  - 受信トレイ `/inbox`・`/inbox/<key>`: 開いた直後は一覧が全面、選ぶと右にメッセージの詳細（2 ペイン）。詳細で中身を読んで答え、答えると次の未回答へ進む。画面の判断は実際の大きさの 1/4（160〜480px）で全部を並べ（押すと拡大、名前を押すとその画面について答える。← → で前後）、選んでいる画面に違和感を付ける。通知のリンクも `CONSOLE_URL/inbox/<key>` を指す。詳細の「プロンプト」で、課題のタイトル・key（パラメータ）・受信トレイの URL 入りのプロンプト（AI エージェントに工場の MCP の `inbox_read` でその key を読ませる）をコピーする。前の `/#choice-<id>`・`/#choices`・`/#ship` は読み替える
  - ライン `/line`: 上に止まっているもの（ホームと同じ）。対応が必要・作業中・順番待ち・投入待ち（同時に流す上限）・依存待ち・検査と判定とマージ待ち・今日の済み。カードに元のチケット（出どころ `tickets.source` の印と番号、URL）と PR。リポジトリと出どころで絞れる
  - 出荷 `/ship`: 製品ごとに出荷先のカード（今は Web の検証・本番だけ。モバイルのストアや CDN は同じ形のカードで足す）
  - 使用量 `/usage`: 下に別記。中身は `GET /usage/body` の HTML の断片で、タブとヘッダはほかのページと同じ
  - ヘルプ `/help`: ヘッダの数字（指示役・モード・流量・着手待ちのチケット）、止まっているものの種類、ラインの列の意味
  - ホームの「判断待ち」（受信トレイの未回答。プロダクトの判断）と「工場の不調」（保守の担当が直すもの）を分ける。人の判断待ちの停止（human）は受信トレイに出るので「工場の不調」には出さない（2026-11-08）
  - 工場の不調（2026-11-08、`dispatcher/alerts.py`）: 開いている停止を 1 行ずつ「何が止まり・どれのことか（チケット・PR・更新の依頼）・何分前から」で出す（最初の 5 件、残りは「ほか N 件」）。押すと次にすること・対応する人・記録・リンク・「やり直す」（失敗した作業の積み直し。`factoryctl rerun` と同じ `rerun.py`）・作業の記録の最後の部分（`GET /api/jobs/<id>/log`、鍵らしい文字列は伏せる）。文は停止の種類と関係する記録から組み立て、工場の内側の記録の文（stoppage の what / waiting_for）は「工場の記録の文」として添えるだけ。× で画面から消せる（`POST /api/alerts/dismiss`、events の `alert.dismissed` / `alert.restored`。停止そのものは閉じない）。消したものは「消したもの」で見られ、戻せる。受信トレイの停止の件も同じ文。失敗した作業の停止は、次の作業が始まるかチケットが閉じると番人が閉じる
  - 設定 `/settings/<項目>`: ランナー・ラインと流量（`POST /api/line/scale` と同じ）・Mac（起こしたまま）・工場の版。連携・credential・Claude からのアクセス・工場の更新は今後
- 操作: 優先度（p0 緊急・p1 急ぎ・p2 通常・p3 後回し）、保留・再開、止める。すべて events に `ticket.*` / `job.cancel_requested` として残る
- API: `GET /api/state`、`POST /api/tickets/<id>/priority`（`{"level":"p1"}`）、`POST /api/tickets/<id>/hold`・`resume`、`POST /api/jobs/<id>/stop`・`rerun`、`POST /api/alerts/dismiss`（`{"key", "on"?}`）。操作には画面に埋めた合言葉のヘッダー `X-Console-Token` が要る
- 使用量（`/usage`、API は `GET /api/usage?days=14`、CLI は `factoryctl report usage [--days 14] [--json]`）: トークンの消費と費用（API 単価換算）を、日ごと（予算の枠と同じ 7:00 区切り。予算の枠の合計・予算で止めた時間・契約の上限に当たった回数つき）・役割ごと・モデルごと・契約の週（`PLAN_WEEK_RESET`、既定 `sun 11:00`）で出し、先頭に「アカウントを足すかの判断の材料」を決まった規則で並べる（usage.py）。週の上限の目安を API 単価で `PLAN_WEEKLY_LIMIT_USD` に入れると、割合と届く見込みも出る。予算の枠は supervisor と同じ `BUDGET_WINDOWS_WEEKDAY` / `BUDGET_WINDOWS_WEEKEND` を console にも渡す（読み方は usage.py の 1 か所）。予算の宣言（下の「使用量の予算の宣言」）があればそちらが優先
- 人の選択（ADR 0025）: デザイン検査役が人に回した画面の案（`design_variants`）と、内部品質の抜き取り（`taste_sample`）を、上部の「人の選択」に案の画像つきで並べる。「この案にする」「どれも違う（戻す。一言は必須）」「違和感（一言は必須）」を押せる。記録は artifacts の `HumanChoice` と events の `choice.requested`・`choice.made`（推奨との一致 `agreed`）・`choice.returned`・`taste.flagged`（教師信号 taste_flag）。依頼は `choice.request()` で作り、画像はオブジェクトストアの `choices/` か `design/` の下に置く（コンソールが中継し、外の URL は出さない）。デザインの線（`design_line`）は検査役が人に回したときに依頼を作り、画面で選ぶと `factoryctl design pick` と同じく `ui_design.chosen` を残す（Issue への反映は dispatcher の周回）。「どれも違う」は人の一言を指摘にしてデザイナー役を積み直す
- 実装後の画面（ADR 0025 の 4、`exit_capture.py`）: dispatcher が `EXIT_CAPTURE_INTERVAL_SEC`（既定 300 秒）ごとに、工場の PR（開いているかマージから 48 時間以内）の head の状態「画面のキャプチャ / <アプリ>」を見て、`target_url` の CI の成果物（zip）から `manifest.json` と画像を読む。変わった・新しい・消えた画面の画像を `choices/capture/` の下に置き、人の選択（kind `exit_capture`）を作る。受信トレイには開いている分を「実装後の画面: N 件」の 1 件にまとめて出し（デザインのチーフの出番）、通知は最初の 1 回だけ（印は任意なので知らせ直さない）。開いている PR でも head が 48 時間動いていなければ見ない。画面ごとに「違和感」（一言は必須）で `taste.flagged`、見て違和感がなければ「見た」で `choice.seen`（印のない画面は負例の材料）。新しい head のキャプチャで前の依頼は閉じ、7 日見られなければ閉じる（`choice.closed`）。PR の本文には「実装後の画面のキャプチャ」の節（アプリごとの変わった画面の表と成果物のリンク）を書く。PR・head・アプリごとに 1 回（`capture.collected`）。`EXIT_CAPTURE=0` で止める。成果物を取るには工場のトークンに製品のリポジトリの Actions（読み取り）と Commit statuses（読み取り）が要る
- 受信トレイ（ADR 0026 の決定 7）: ページの切り替えの「受信トレイ」に対応が必要なものの件数がバッジで出て、押すと一覧（誰の出番か・何をすればよいか・待ち時間）が開く。集めるのは人の判断待ちの停止（分類 human）・開いている人の選択・staging にあって本番に出ていない版・他の工程が積んだ依頼（`inbox.request(cur, key, kind, who, title, url=, action=)` で積み、`inbox.resolve(cur, key, by, outcome)` で消す。レビューページの承認待ちや着手判定の問いはこれを使う）。API は `GET /api/inbox`（`/api/state` の `inbox` にも入る）。表は持たず、毎回制御プレーンから組み立てる
- 受信トレイで答える（ADR 0034 の順番 1）: 各件は答え方 `answer`（`type` が choose・approve・ack、選択肢、戻す・違和感の印などの `extra`、答えたら何が起きるかの `effect`、答えてよい役割 `allowed`、取り返しのつかない作用なら `irreversible`）を持ち、コンソールの受信トレイの件の上にボタンと一言の欄が出る（本番に出すのは 2 度押し）。答えはどの入口からも `inbox.answer(cur, key, option, by, via, note=, on=, explicit=, role=)` の 1 か所を通り（`POST /api/inbox/answer`。人の選択・レビューページ・「本番に出す」のボタンもここを通る）、件の種類ごとの今の関数（`choice`・`design_line`・`ship`・`drain`・`inbox.resolve`）を呼ぶ。件ごとのロックで先に通った答えが勝ち、後からの答えには「誰がどこで何と答えたか」を返す。記録は events の `inbox.answered`。答え済みで反映を待っている件は一覧に「答え済み」と出て、人の出番（件数・通知）からは外れる。この場で答えられるのは人の選択・レビューページの依頼・本番への出荷・ラインの一時停止の再開・更新を戻した知らせ（見た）で、マージ待ちなど GitHub で答える件はリンクのまま
- 書き戻し: `notify_inbox.tick` が、答え済みの件をその件を知らせた宛先に「○○ が操業コンソールで『案 B』と回答済み。この件は閉じました」と書き戻す（1 つの答え・1 宛先に 1 回。`inbox.written_back`。48 時間より前の答えは書き戻さない）
- 受信トレイの通知: dispatcher の番人の周回（`notify_inbox.tick`）が、新しく出た件・`INBOX_RENOTIFY_MIN` 分たっても残っている件（`INBOX_MAX_RENOTIFY` 回まで）・`INBOX_ESCALATE_MIN` 分を過ぎた件（代理 `INBOX_PROXY` にも）を 1 通にまとめて通知先に送る。通知先は工場を建てるときに `INBOX_NOTIFY` で選ぶ（`log`・`github-issue`＝工場状況 Issue へのコメント、Gen1 は見回りがこれを読んで Claude アプリに知らせる・`slack`＝`SLACK_WEBHOOK_URL` の Incoming Webhook、誰の出番かごとのメンションは `INBOX_SLACK_MENTIONS`）。値は .env に置く。`INBOX_QUIET`（例 `22-7`）の間は送らず明けてからまとめる。記録は events の `inbox.notified`・`inbox.escalated`・`inbox.cleared`（`waited_sec` が判断待ちの滞留の SLI）
- 通知の出し分け（ADR 0032）: 製品のリポジトリに `.factory-config/notifications.yaml`（例は `templates/notifications.example.yaml`）を置くと、通知の種類ごとに宛先へ出し分ける。dispatcher が `NOTIFY_CONFIG_POLL_SEC` ごとに既定ブランチの版を読み、検査を通った版を events の `notify.config_changed` に写して使う（通らない版は `notify.config_rejected` と受信トレイの error に出し、前の写しのまま送る）。credential は `secret: env:NAME` の参照だけを書き、値は `.env` に置く（dispatcher は `.env` を env_file で受け取る）。受信トレイは事業主の判断が要る件だけを事業主の宛先に送る（自走 `operated_by: business_owner` なら工場の判断も。建設の判断は送らない）。ファイルがなければ上の `INBOX_*` のまま
- 定期レポート（ADR 0032 の決定 9）: supervisor が `DAILY_REPORT_HOUR`（JST）を過ぎたら前日の分を種類ごとに 1 回組み立て（`report.factory_health`＝工場の操業レポートは `report_health.py`、`report.product`＝開発状況レポート（ロードマップ・ペースと見込み。工場のチケット・`pr.merged`・`release.created` から）は `report_product.py`。中身は `definitions/reports.yaml` の仮置き）、events の `report.built` に中身の JSON を残し、割り当ての宛先に送る（宛先ごとに `notify.sent`。失敗した宛先は 3 回まで送り直す。事業主の宛先には事業主に出せない節を落とす）。`notifications.yaml` のない工場は残すだけで送らない。提供元の宛先は文面の頭に `FACTORY_NAME`
- 報告書の添付（ADR 0032 の追記 2026-11-06）: Slack アプリの宛先（`via: slack_app`、ボットのトークンに `chat:write`・`files:write`・`channels:read`、チャンネルは名前か ID）には、定期レポートを 1 枚の HTML の報告書（`report_page.py`）として添付し、本文は見出しと 3 行の要約だけにする。Incoming Webhook の宛先は添付できないので全文を流す。見立て（各モジュールの `assess`、閾値は `THRESHOLDS` の仮置き）が順調・注意・危険と兆候・打ち手を出して所感にし、報告書は異常をハイライトして直近 `REPORT_TREND_DAYS`（14）日の推移を図にする。試し送りは `docker compose exec dispatcher factoryctl report send all [--date YYYY-MM-DD] [--to 宛先]`、送らずに報告書だけ見るなら `--html /tmp`

チケットと優先度:

- GitHub の Issue は工場のチケット（表 `tickets`）に写される。優先度の最初の値は Issue の `factory:priority:p0`〜`p3` のラベル（なければ p2）。人がコンソールや factoryctl で変えた値はラベルで上書きしない
- キューは優先度の高い順、同じなら受け付けた順に流れる。`factory:planned` も優先度の高い順に投入し、p0 は同時に流す上限を待たない
- **割り込み**: p0 のジョブが待っていて枠が空いていなければ、dispatcher が優先度の低い作業中のジョブ（実装・要求の確認）を止めて順番待ちに戻す。止めた時点までの作業は捨てて最初からやり直す（`job.preempted` に捨てた時間を残す）
- 止める操作は印を付けるだけで、コンテナは dispatcher が 5 秒以内に止める（`OPS_INTERVAL_SEC`）

```bash
docker compose exec postgres psql -U factory -f /docker-entrypoint-initdb.d/003_tickets.sql   # 既存の DB に表を足す（冪等）
docker compose up -d --build console dispatcher supervisor
docker compose exec dispatcher factoryctl tickets
docker compose exec dispatcher factoryctl priority guccyon/rally#170 p0 --reason "予約一覧の障害"
docker compose exec dispatcher factoryctl hold 12
```

## 事業部の窓口（ADR 0030）

工場の外の人（とその代理の Claude）が依頼と問い合わせを持ち込む口（`dispatcher/desk_api.py`、http://localhost:8792）。操業コンソールより狭く、相談と、依頼者に許した範囲のチケットの操作（優先度・保留・状態）だけを受ける。API が正本で、MCP のサーバー（順番 3、`desk-mcp/factory_desk_mcp.py`。stdio、標準ライブラリだけ）はこれを呼ぶだけの包み。手元の Claude Code・Claude Desktop への登録は `desk-mcp/README.md`。

- 順番 1: トークンと役割（運用主・依頼者）、会話の記録（`desk_conversations` と events の `desk.*`）、明確な指示（「#812 を先に」）を壁打ちを通さずに通す優先度・保留・チケットの状態
- 順番 2（依頼の壁打ち。`dispatcher/desk_line.py`、`core-gates/desk_consult.py`、`definitions/desk.yaml`）: 依頼（`kind: request`）を開くか返事を書くと、dispatcher が相談役のジョブ（`desk_consult`）を窓口の枠で積む。相談役（工場の中のプランナー。製品の `specs/`・`docs/`・流れているチケット・開いている Issue を読むだけ）は、理由と選択肢つきの問いを 1 つ返すか（`waiting_requester`、最大 3 回）、確定した Request とタスクのまとまりの下書き（依存つき、要件 → 画面 → 契約 → 実装の順、既存のチケットとの重なり、見込み）を返す（`drafted`）。問いを使い切ったら、決まったところまでを下書きにして残りを「未確定」と書く
- 依頼者が下書きを 1 回確認すると（`confirm`、優先度もここで決め直せる）、dispatcher が次の周回で製品の Issue として依存の順に起票する。依存のないタスクは `factory:plan`（プランナーの着手判定。ADR 0026）、あるタスクは `factory:desk-waiting` で、依存が閉じたら `factory:plan` に替わる。チケットはもとの会話（`desk_conversation_id`）と依頼者の優先度（`priority_set_by` は `desk:<名前>`）を持ち、依頼者は自分の依頼から作ったチケットの優先度・保留を変えられる
- 直したいところは下書きに返事を書けば出し直す（古い下書きは確認できない）。確認した相談には書けない（新しく開く）
- 窓口の枠は流量の宣言の `desk`（`factoryctl line scale --desk N`、既定 `DESK_PARALLEL=1`）。ラインの一時停止中も相談には答え、ラインのワーカーを奪わない。相談 1 件の費用は `DESK_CONSULT_MAX_USD`（既定 $2、仮置き）まで
- 順番 4（問い合わせの回答役。P5b の Answer ライン `answer_line` の最小形。`dispatcher/desk_answer.py`、`core-gates/desk_answer.py`、`worker/desk_answer.sh`）: 問い合わせ（`kind: inquiry`）を開くか返事を書くと、dispatcher が回答役のジョブ（`desk_answer`）を窓口の枠で積む。材料は制御プレーンの記録（製品のチケット・最近閉じたチケット・開いている停止と 7 日の停止・ラインの宣言・7 日のジョブの成否と費用とマージ・最新の日報・依頼者が名指しした `#番号` のチケットの経緯）で、どれにも参照の id（`ticket:owner/repo#12`・`job:345`・`event:678`・`stoppage:<key>`・`report:<id>`・`line:status`・`activity:7d`）を付ける。ワーカーは製品の `specs/`・`docs/` と工場のロードマップ・ADR も写す。回答役は立場を計画（`planner`）か工場長（`factory_head`）から選び、問い返しを 1 回まで挟むか（`waiting_requester`）、根拠つきの回答を返す（`answered`、`desk.answered` に立場・根拠・分からないこと）。根拠は材料にある id か写したファイルのパスだけを残し、1 つも残らなければ回答として出さない。回答役は読むだけで工場を変えない。依頼の話になったら依頼（`request`）として開くよう勧める。続けて聞けば同じ会話でもう一度答える。費用は相談と同じ上限（`DESK_CONSULT_MAX_USD`）。モデルは `definitions/agents.yaml` の `desk.answer`（Sonnet から）
- 受信トレイ（ADR 0034 の順番 2）: `GET /api/desk/inbox`・`GET /api/desk/inbox/<key>`・`POST /api/desk/inbox/<key>/answer`。トークンの役割と製品で答えてよい件だけを見せ、件の中身（`detail`）と答え方を返し、答えは `inbox.answer` を入口 `mcp` で通す。MCP の道具は `inbox_list`・`inbox_read`・`inbox_answer`
- GitHub の Issue で clarify まで済んだ依頼（`factory:clarified`）は `factoryctl desk from-issue owner/repo#番号` で窓口の依頼として開き直せる（会話の `source_ref` に Issue の URL）
- 単発の依頼のループ（ADR 0039 の順番 1、`postgres/init/006_desk_loop.sql`）: 会話の状態に `routed`（振り分けた）・`awaiting_confirm`（確認待ち）・`running`（実行中）・`delivered`（提出した。受取確認待ち）・`accepted`（依頼者が受け取った）・`handed_off`（チケット化で製品のラインに渡した）を足した。状態を進めるのは dispatcher だけ（`desk.advance`。向きは `desk.TRANSITIONS`）で、終わり方は `desk.finish` を通り、閉じる理由（`accepted`・`declined`・`superseded`・`withdrawn`・`handed_off`）と、閉じたときの記録（対応の種類・最初の返事／振り分け／提出までの秒・費用・往復・結末）を `desk.closed` に残す。チケット化は起票した時点で `handed_off` で終わる。依頼者が閉じると取り下げ（`withdrawn`）で、動いているジョブを止める。断りから別案として開いた依頼は `derived_from` で元と結ぶ。振り分けた後の依頼者の言い足しは記録だけ（相談役に回し直さない）
- 決まった操作の 1 本（ADR 0039 の順番 2、ADR 0037 の決定 3・4。`definitions/operations.yaml`、`dispatcher/operations.py`）:
  1. 受付: 依頼を開くと、中身を読まずに「受け付けた・次は振り分け」をすぐ返す（`kind: ack`）
  2. 振り分け: 相談役に操作の一覧を渡し、操作を 1 回動かせば済む依頼なら `route`（`path: operate`・操作の id・引数）が返る。dispatcher が宣言と照らして引数を検査し（宣言にない引数・範囲の外の値・通知の設定にない宛先は拒む）、`desk.routed` に対応の種類・引数・確認の段・見本（何をどこへ）を残す
  3. 確認: 段が `send` なら受信トレイに依頼者の件 `task:desk_confirm:<会話>:<回>`（実行する・やめる）を出す。窓口の `confirm` に `{"answer": "run"|"cancel"}`、操業コンソールの受信トレイのどちらから答えても `inbox.answer` を通る。`read` の段は飛ばす
  4. 実行: dispatcher の周回（`desk_line.run_operations`）が操作を 1 回だけ動かす（`op.started` で同じ回を 2 度動かさない）。最初の操作は `report.send`（`factoryctl report send` と同じ `report.send_now`）
  5. 提出: 会話に結果を書き、受信トレイに受取確認の件 `task:desk_receipt:<会話>:<回>`（受け取った・続きを頼む・差し戻す）を出す。窓口の `messages` に `{"answer": "accept"|"more"|"return", "body": 一言}`。受け取ったら `accepted` で完了、続き・差し戻しは同じ会話で相談役の振り分けからやり直す（続きは 5 回まで）。返事がなくても閉じない
- 応答時間の目安・途中の様子・断り・上限（ADR 0039 の順番 3。値は `definitions/desk.yaml` の `loop`、dispatcher の写しは `desk.LOOP`）:
  - 目安: 最初の返事 1 分、振り分け 5 分（`analysis` は 15 分）、提出は確認から `operate` 5 分・`quick` 15 分・`analysis` は振り分けで示した時刻（`deliver_by`。なければ 2 時間）。マイルストーンは `desk.milestones` が events から決定論で切り出す（振り分けは依頼者の発言から工場の次の返事まで。問いの返事待ちの間は数えない）
  - 受付の返事で振り分けを返せる時刻を言う（窓口の枠に先に積まれた相談があれば、その分を足した時刻）
  - dispatcher の周回（`desk_line.watch`）が、目安の 8 割を過ぎても返事のないマイルストーンに「今どこまで・目安の時刻」を会話に書き（`desk.progress`）、目安を過ぎたら `desk.late` を残す（どちらも 1 つのマイルストーンに 1 回）。閉じたときの記録に `late`・`met_ack`・`met_route`・`met_deliver` を足した
  - 断り: 相談役が `route.path: decline`（理由は決まった語、代わりの案を 1〜3 つ）を返すと、`desk.declined` を残して断りを提出し、受信トレイに `task:desk_decline:<会話>:<回>` を出す。答えは受取確認と同じ語で、`accept` は納得した（`declined` で完了）、`more` は別案で進める（代わりの案を別の依頼として新しい会話で開き、元に `superseded_by`・新しい方に `derived_from`。元は `superseded` で完了）、`return` は断りを差し戻す（理由が要る。同じ会話で振り分けから）
  - 上限: 実行役が `cap_hit`（と残り `remaining`）を返したら、決まったところまでを提出し、次の選択肢（続ける・`analysis` に切り替える・やめる）を添えて受取確認を取る（`desk.cap_hit`）。`quick`・`analysis` の実行役を入れたときに使う
- 受取確認の知らせ直しと、依頼者に見えるもの（ADR 0039 の順番 4）:
  - 依頼者の答え（確認・受取確認・断り）がないと、確認を頼んだ・提出した時から 3 日・7 日で会話に知らせ直し（`desk.reminded`）、10 日で運用主の受信トレイに「依頼者の返事待ち」（`task:desk_waiting:<会話>:<回>`、見たら閉じる）を出す（`desk.escalated`）。会話は閉じない。依頼者が答えたら運用主の件も消える。値は `loop.remind_after_days`・`loop.to_operator_after_days`
  - 会話を読むと `status`（今のステップ・次に起きること・いつ頃か・振り分けの結果・途中の様子・提出・断りの理由と代わりの案・答えの選択肢・終わり方）が付く（`desk.status_view`）。窓口・MCP・コンソールのどこから読んでも同じ
  - 依頼者の受信トレイ（`inbox_list`）に、自分の会話の件（確認・受取確認・断り）が出る。ほかの依頼者の件と、運用主に回した件は見せない
  - 会話を終えたら（取り下げを含む）、その会話の受信トレイの件を消す
- 提出の知らせ方（ADR 0039 の順番 5。決定 5a）:
  - 依頼を開くときに `notify`（通知の設定にある宛先の名前の並びか `desk`＝窓口の会話だけ）を選べる。会話の `wanted.notify` に持つ。依頼の本文の宛先には送らない
  - 提出・断り・知らせ直しを `desk.notice_requested` に積み、dispatcher の周回（`desk_line.send_notices`）が送る。宛先は `notify`、なければ `done.task` の既定（事業主の宛先。Gen1 は Slack）。中身は会話の番号・題・要約・答え方だけ
  - メンションは宛先の `mentions` に手で書いた分だけ（依頼者の名前か `requester` の鍵）。利用者の認証が入るまで自動では付けない
  - 宛先の送らない時間（`quiet`）は明けた周回で送る。送れなければ 5 分ずつ空けて 3 回までやり直し、それでもだめなら運用主の受信トレイに `task:desk_notice_failed` を出す
  - 受取確認・断りの件（`desk_receipt`・`desk_decline`）は受信トレイの通知（`request.inbox`）では重ねて送らない

```bash
docker compose exec postgres psql -U factory -f /docker-entrypoint-initdb.d/004_desk.sql   # 既存の DB に表を足す（冪等）
docker compose exec postgres psql -U factory -f /docker-entrypoint-initdb.d/006_desk_loop.sql   # 単発の依頼のループの状態と列（冪等。足すだけ）
docker compose exec postgres psql -U factory -f /docker-entrypoint-initdb.d/007_requester_preferences.sql   # 依頼者ごとの好み（ADR 0040 の決定 6。冪等。足すだけ）
docker compose up -d --build desk
docker compose exec dispatcher factoryctl desk token create builder --role operator     # トークンはこのときだけ出る
T=fdk_...   # 出たトークン
curl -s -H "Authorization: Bearer $T" localhost:8792/api/desk/tickets/guccyon%2Frally%23900
curl -s -H "Authorization: Bearer $T" -d '{"level":"p1","reason":"検証に使う"}' localhost:8792/api/desk/tickets/guccyon%2Frally%23900/priority
curl -s -H "Authorization: Bearer $T" -d '{"kind":"request","product":"guccyon/rally","body":"予約まわりの通知を先に進めて"}' localhost:8792/api/desk/conversations
curl -s -H "Authorization: Bearer $T" "localhost:8792/api/desk/conversations/1?after=0&wait=30"   # 問いか下書きが来るまで待つ
curl -s -H "Authorization: Bearer $T" -d '{"body":"前日の知らせのほう"}' localhost:8792/api/desk/conversations/1/messages
curl -s -H "Authorization: Bearer $T" -d '{"draft_id":42,"level":"p1"}' localhost:8792/api/desk/conversations/1/confirm   # 下書きを確認して投入
docker compose exec dispatcher factoryctl desk show 1                                        # 会話・下書き・作ったチケット
```

相談役のジョブは基本のワーカーのイメージに入っている `desk_consult.sh` で動くので、順番 2 を入れるときはワーカーのイメージと dispatcher を作り直す（`build.sh`）。

## 工場の前段（ADR 0035。工場をインターネットに置くとき）

パイロットの EC2 などに工場を置くときは、`compose.edge.yaml` を重ねて前段（Caddy ＋ oauth2-proxy）を足す。操業コンソールとプロトタイプは GitHub のログイン（許したアカウントだけ）、窓口は 1 人 1 本の窓口のトークンで入り、どれも HTTPS とセキュリティグループの IP 制限の内側に置く。`compose.yaml` と工場のコードは変えず、`.env` の `COMPOSE_FILE=compose.yaml:compose.edge.yaml` を抜けば外れる。手順は `edge/README.md`。Mac の工場では使わない。

## PR の見張り（ADR 0015）

工場の PR に起きた動き（push、CI の結果、衝突、レビュー）を GitHub から拾い、抽象化したイベント（`pr.head_updated` / `pr.ci_failed` / `pr.ci_passed` / `pr.conflicted` / `pr.review_submitted`）として events に発行する、決定論的なモジュール（`dispatcher/pr_events.py`。dispatcher の中のスレッド）。オーケストレーター（supervisor）は GitHub を見ずにこのイベントを読む。

- 入口は 2 つ。Webhook（`WEBHOOK_SECRET` があれば dispatcher の `/webhook` が `pull_request` / `pull_request_review` / `check_suite` / `check_run` / `workflow_run` を受ける。商用・クラウドで即時）と、ポーリング（`PR_EVENTS_INTERVAL_SEC`、既定 60 秒。オンプレミスや手元）。どちらも「見に行く合図」で、事実は GitHub の今の状態を取り直して決める。同じ事実は dedupe で 1 回しか発行しないので、両方動いても二重にならない
- `PR_EVENTS_SOURCE=both|poll|webhook`（既定 both。webhook だけだと見逃しを拾えない）、`PR_EVENTS_ENABLED=0` で止める

## 工場長の supervisor（ADR 0004）

判断は持たず、状態と表を見て工程を起動するだけの常駐プロセス（compose の `supervisor`。dispatcher と同じイメージ）。60 秒ごとに:

- `supervisor.heartbeat` を出す（途絶えたら番人が停止を開く）
- 工場の PR が `mergeable_state=clean`（CI 通過・衝突なし・ドラフトでない）になったら、変更ファイルを `definitions/change_type_rules.yaml` で変更種別に写し、製品の manifest を引いて `supervisor.merge_decided` を記録する。表が承認を求めれば PR に `factory:needs-approval`。表が auto で、かつ工場の権限（`phase_grants` の merge）も auto のときは、**検査役の承認**（下の「検査役」）が出た周期に squash でマージする（ADR 0014）。それ以外は記録だけ（shadow）
- 検査役（ADR 0014。2026-10-31 オーナー）: 人がやる承認を委任する役で、いまは検査（inspect）と judge の組み合わせ。その head で検査と judge が終わり、judge が人に回した判断がなければ承認（`supervisor.inspector_approved`）し、表が auto の PR はその後にマージする。人に回した判断があれば停止 `judge:` を人が閉じるまで待つ。採用された指摘は差し戻しで直すのでマージは止めない（D7）。検査がなければ（`SUPERVISOR_INSPECT=0`）CI 緑で承認とみなす
- マージを判定した head ごとに検査ジョブ（kind=inspect）を積む。dispatcher がワーカーの `inspect.sh` で `core-gates/shadow_review.py`（Engine の main から毎回取る）を動かし、`gate.evaluated`（mode=shadow）と InspectionReport を残す。GitHub には書かない。同じ PR の別のジョブ（body_fix など）が動いていて積めなかったときは、次の周回で積み直す（周回ごとに試す）。`SUPERVISOR_INSPECT=0` で止める
- 判定した PR が閉じたら `pr.merged`（誰がマージしたか）/ `pr.closed_unmerged` を記録する。判定と人の判断の一致率の材料
- `factory:planned` の Issue の本文の「依存: #12 #13」がすべて閉じたら、優先度の高い順に `factory:implement` に替える。同時に流すのは `SUPERVISOR_MAX_INFLIGHT`（既定 1。実行中・投入済みのジョブと開いている工場の PR の合計。ただし人を待っている PR＝judge が人に回した・差し戻しを流さなかった・CI 落ち・衝突・表が承認を求めた、は数えない。2026-10-31）まで。p0 は上限を待たない。保留中は流さない
- 自律的な再開（ADR 0011）: 停止を閉じるときは `factoryctl stoppage close <key> --woken-by pr_event|watcher|schedule|human` で、builder を起こしたものを残す（人が起こした割合が `report stoppages` に出る）。番人の通知先は `STATUS_NUMBER`（Issue でも PR でもよい。空なら factory:status の Issue）。ホストのスリープは `scripts/keep-awake.sh install` で防ぐ（launchd が常駐させ、工場のコンテナが動いている間だけ `caffeinate` でスリープを止める。止まっていれば普段どおり眠る。`uninstall` で元に戻る、`status` で確認。操業コンソールのトグル「稼働中は Mac をスリープさせない」で切り替えられる。トグルは見張りから連絡があるときだけ出る）
- 使用量の枠ごとの予算（2026-10-29 オーナー）: Max の使用量は毎朝 7:00 から 5 時間ごとにリセットされるので、JST の枠（7・12・17・22・3 時）ごとに予算を置き、その枠の `job.finished` の費用（API 換算）の合計が予算に達したら、次の枠まで新しい実装を投入しない（作業中のもの・検査・judge・p0 は止めない）。既定は平日 $10・$15・$35・$25・$15、週末は各 $20（1 日 約 $100、平日は夕方以降に寄せる）。`BUDGET_WINDOWS_WEEKDAY` / `BUDGET_WINDOWS_WEEKEND`（「開始時:ドル」の並び。空で無効）。止めたことは `supervisor.budget_paused`
- 使用量の予算の宣言（2026-11-08 オーナー。ADR 0029 の追記）: 操業コンソールの設定「使用量の予算」（`POST /api/budget`）か `factoryctl budget set --daily 700` / `factoryctl budget set --mode weekday --sat 1000 --sun 1000` で、毎日同じ額か曜日ごとの額を宣言する（events の `budget.set`）。1 日（7:00 JST から）を 1 つの枠にし、supervisor が周回ごとに読むので上げ直さずに効く。宣言がなければ上の `BUDGET_WINDOWS_*` の枠。今の決め方は `factoryctl budget`

`SUPERVISOR_DRY_RUN=1` で GitHub に書かずに判定だけ記録する。

## 仮決めと判断の記録（ADR 0006 の前半）

ワーカーは作業の途中で判断が要っても止まらず、最も妥当な案で仮決めして続ける。仮決めは PR 本文の「## 仮決め」に 1 件ずつ（類型 id・問い・選んだ・他の案・根拠・解釈・確信度・影響）書き、dispatcher がそれを分解して artifacts の Judgment（stage=provisional）と events の `judgment.provisional` に残し、PR に `factory:provisional` を付ける。類型と版は `definitions/judgments.yaml`。

### judge と差し戻し（後半）

- supervisor が、検査の結果が出た head ごとに judge のジョブを 1 回積む（仮決めも指摘もなければ積まない）。入力は制御プレーンの記録だけで組み立て（仮決めの Judgment、最新の gate.evaluated の指摘、類型ごとの一致率、manifest の制約、モード）、ワーカーの `judge.sh` に標準入力で渡す。judge（`core-gates/judge.py`）はツールを持たず、根拠に挙がったファイルは PR の head のクローンから読んで渡す
- 結果は Judgment（stage=judged）と `judgment.judged` に 3 層で残す。PR に `factory:judged:<モード>`、確信度が 0.6 未満なら `factory:confidence:low`、人に回した判断があれば `factory:needs-judgment`（停止としても開く）
- 採用された指摘（nit を含む）と差し替えた仮決めは、1 本の差し戻し Issue（`factory:sendback`、本文は引用＝データ、`依存: #元の Ticket`）に起票する。unattended は `factory:planned`（依存が閉じたら supervisor が流す）、attended は `factory:fix-proposed`（人が `factory:planned` に替える 1 タップ）
- 箇所が PR 本文だけの指摘（例: 「## 知見」の節がない）は Issue を切らず、kind=body_fix のジョブ（`core-gates/body_fix.py`、tier-small、ツールなし）が本文の末尾に節を足して直す（2026-10-27 オーナー）。差し戻しの回数には数えない。events に `pr.body_amended`
- 上限は 1 PR 1 回・1 日 5 件（仮置き）。差し戻しの修正 PR を再検査して指摘が残ったら、2 回目は流さず人へ（`sendback.residual`）
- モードは `factoryctl mode set` ＞ 時間帯（`MODE_SCHEDULE`）＞ 既定（`FACTORY_MODE`、なければ attended）

```bash
docker compose exec dispatcher factoryctl mode set unattended --until 2026-10-28T08:00:00+09:00
docker compose exec dispatcher factoryctl judgment list --ungraded
docker compose exec dispatcher factoryctl judgment grade 123 agree --note "SSOT どおり"
docker compose exec dispatcher factoryctl report judgments --from 2026-10-27
```

## 来歴とロット（ADR 0007）

製造の来歴（Engine の版、ワーカーのイメージ、製品の既定ブランチの先頭、モード、モデルの tier）を `job.started` と PR の成果物の `payload.provenance` に付ける。属性はすべて optional で、取れなければ `unknown`。欠けてもラインは止めない。
ロット＝Release は後付けで、前の Release 以降にマージされた工場の PR をまとめて含める。利用者の報告や不具合は版から PR、製造の来歴へたどる。

```bash
docker compose exec dispatcher factoryctl release create guccyon/rally 0.1.0      # --prs URL,URL で明示もできる
docker compose exec dispatcher factoryctl report provenance --from 2026-10-20      # 来歴の付与率と Release への紐付け率
```

## 出荷と Rollback（ADR 0012）

Release ごとに製品のイメージを作り、建設者の端末の Docker に staging（自動）と prod（承認ボタン）の環境を立てる。compose の `shipper` が events の依頼を受けて手を動かす（入口は依頼を書くだけ）。製品の出荷の形は製品の `.factory-config/ship.yaml`。
Rollback は前の版のイメージに戻すだけで、DB は戻さない（製品の expand/contract が支える）。押してから確かめ終わるまでの秒数を `release.rolled_back` に残す（目標 60 秒）。

```bash
docker compose up -d --build shipper                                                  # 初回（factory-dispatcher:p0 に git と docker compose を重ねる）
docker compose exec shipper factoryctl release ship guccyon/rally@0.2.0             # イメージを作って staging へ
docker compose exec shipper factoryctl release approve guccyon/rally@0.2.0          # 承認ボタンと同じ。prod へ
docker compose exec shipper factoryctl release rollback guccyon/rally --env prod    # 1 つ前の版へ
docker compose exec shipper factoryctl release status guccyon/rally
```

合成シード: 製品の ship.yaml に `seed` があれば、その環境（rally は staging だけ）に起動を確かめたあと 1 回だけ書く（`release.seeded`）。環境のボリュームを消したら、その記録も消してから出し直す。

出荷の列車: supervisor が 1 日 1 回、`RELEASE_TRAIN_HOUR`（JST、既定 17 時）を過ぎたら、まだ Release に入っていないマージ済みの工場の PR があり、出荷が処理中でも承認待ちでもない製品（`RELEASE_TRAIN`）について、次の版（minor を 1 つ上げる）の Release を切って staging に出す。prod は承認ボタンを待つ。

操業コンソールの上部に製品ごとの検証と本番の版が出て、「本番に出す」（承認ボタン）と「本番を前の版に戻す」を押せる（どちらも 2 度押しで実行）。コンソールは依頼を書くだけで、docker のソケットは持たない。

## 触れる環境（ADR 0028 の touchable_env）

Release 候補ごとに、人がスマートフォンと PC で触れる環境を 1 コマンドで立てる。Gen1 は staging を QA の環境と兼ねる（使い捨ての環境は P5b の `ephemeral_env`）。
手を動かすのは shipper で、`factoryctl env up` は依頼を順に書いて結果を待ち、入口（URL・QR・ログインの手がかり）を出すだけ。

```bash
docker compose exec dispatcher factoryctl env up guccyon/rally            # 未 Release の PR があれば次の版を切って staging へ。なければいまの版の入口を出す
docker compose exec dispatcher factoryctl env up guccyon/rally --fresh    # 出したあとに検証の DB を空にして合成シードから入れ直す
docker compose exec dispatcher factoryctl env up guccyon/rally --release guccyon/rally@0.9.0   # 版を選ぶ（Rollback ではなく、その版を staging に出し直す）
docker compose exec dispatcher factoryctl env show guccyon/rally          # いまの版の入口
docker compose exec dispatcher factoryctl env restart guccyon/rally --advance-hours 24   # 再起動してサーバーの時計を 24 時間進める（DB は残す）
docker compose exec dispatcher factoryctl env restart guccyon/rally --reset-clock        # 進めた時計を 0 に戻して再起動
```

- 入口の形は製品の `.factory-config/ship.yaml` の `touch`（入口の名前とポートの変数・QR を付けるか・ログインの手がかりを出すコマンド・DB を空にするコマンド）。shipper は出す・戻す・入れ直すたびに `release.touchable` に残し、操業コンソールの出荷の欄にも URL・QR・手がかりと「検証の DB をシードから入れ直す」（2 度押し）が出る
- rally の入口は生徒アプリの Web 版（Flutter。スマートフォンのブラウザで開く）と管理画面（本部・コーチ）。ログインの手がかりはモックのトークン（本部 1 つと、シードのコーチ 3 人）
- スマートフォンから開くには、この機械の名前（または IP）と画面の待ち受けを `.env` に書いて shipper を上げ直す。端末ごとの値なのでリポジトリには書かない。DB と backend は外に出ない（画面が同じオリジンの `/api/*` を中継する）。合成シードとモックのトークンだけの環境だが、同じ網の誰でも触れるので、家や事務所の閉じた網に限る

  ```bash
  SHIP_TOUCH_HOST=<スマートフォンから届くこの機械の名前か IP>
  SHIP_TOUCH_BIND=0.0.0.0
  ```

  どちらも空なら 127.0.0.1 のままで、入口はこの機械からだけ開ける（`env show` と操業コンソールにそう出る）
- `--fresh` は検証の環境だけ（ship.yaml の `touch.environments` と `seed.environments` の両方に入っている環境）。本番の DB は入れ直さない
- iOS のネイティブのビルドを入れるのは Mac ランナー（`mac_runner`）の側。この入口はブラウザで触れる Web 版まで
- 時間をまたぐシナリオ（ADR 0028 の `scenario_time`）: `env restart` は ship.yaml の `touch.restart` の部品（なければ `verify` の部品）を作り直して再起動し、`touch.clock_env` の変数にサーバーの時計を進める秒数を渡す（rally は backend の `CLOCK_OFFSET_SECONDS`）。DB は残す。進めた量は前の再起動に足していき、1 回 7 日・合計 62 日まで。次に版を出す・戻す・入れ直すと 0 に戻る。本番は受け付けない。依頼は events の `release.restart_requested`、結果は `release.restarted`（`clock_offset_seconds`）

## QA 役（ADR 0028 の決定 2。P5a の qa_verdict）

触れる環境（Gen1 は staging を兼ねる）に、製品の `specs/stories/` のユーザーストーリーを利用者として流し、ストーリーごとに Verdict（pass・hold・blocked、確信度、根拠）を出す。形と決まりは `definitions/qa.yaml`。

```bash
docker compose exec dispatcher factoryctl env up guccyon/rally            # 先に触れる環境を立てる（版とログインの手がかりが残る）
docker compose exec dispatcher factoryctl qa run guccyon/rally --priority p0 [--stories id,id] [--through-pause]
docker compose exec dispatcher factoryctl qa show guccyon/rally
```

- ジョブは製品の固定 Issue（`factory:qa`、なければ作る）に `kind=qa` で積む。ワーカーは `factory-worker:design`（Chromium と playwright-core）で `worker/qa.sh` → `core-gates/qa.py`。画面には `host.docker.internal`（`QA_ENV_HOST` で上書き）と ship.yaml の `touch` のポートで届く
- 記録は `qa.verdict`（ストーリーごと。roadmap の `qa_verdict` が数える）と artifacts の `QAVerdict`。根拠のスクリーンショットはオブジェクトストアの `qa/<job_id>/`
- hold の Defect は製品の Issue（`factory:qa-defect`・`factory:fix-proposed`）に起票する（1 回 5 件まで。ストーリーの `known_gaps` に書いてあるものは起票せずまとめに載せる。同じストーリーの同じ歩で開いている Defect があれば、起票せずにその Issue に再現したことを書く）。流すときは差し戻しと同じく `factory:planned` に替える
- 見た目と言葉の温度は判定しない。印としてまとめに載せて人に出す
- Mac ランナー（iOS シミュレーター）は、dispatcher の中継（`runner_relay.py`、`one_orchestrator`）越しに QA のワーカーへ MCP の道具として渡す。ワーカーに渡すのはそのジョブだけの合言葉で、ランナーの鍵と窓口の合言葉は渡さない（`macrunner/PROTOCOL.md` の決めたいこと 1）。生徒アプリのストーリーはまず iOS で、ビルドできなければ Web 版で流す。`RUNNER_RELAY=0` で止める
- 時間をまたぐ手順（ストーリーの `time`。`scenario_time`）: 中継で Mac ランナーの道具と一緒に `env_restart` を渡す（Mac には送らず dispatcher が受け、QA を積んだときの環境で `factoryctl env restart` と同じ依頼を書いて終わるまで待つ）。エージェントは翌日なら 24 時間進めて再起動し、ブラウザを開き直して次の歩を流し、最後に時計を 0 に戻す。中継がない（`RUNNER_RELAY=0`）ときは、その歩を skipped にする
- Defect（ADR 0028 の決定 5。`defect_metrics`）: hold の Defect は、起票したもの・既存の Issue に当たったもの・known_gaps のもの・上限で起票しなかったものも events の `defect.recorded`（`found_in: qa`）に残す。judge が採用した検査の指摘（`found_in: pr`）と工場の PR の CI が落ちたチェック（`found_in: ci`）も同じ型で残す（`dispatcher/defect.py`、型は `definitions/defects.yaml`）。流出防止率と発見工程の分布は建設の日報（`health/daily/status_report.py` の「品質（Defect）」）に出る。QA 自身には採点させない
- まだないもの: ラインを差し止める裁量、QA で見つけた Defect の変更と原因の工程の紐付け（直した PR が分かったときに足す）

### 夜間の探索（P5a の nightly_explore）

台本（ストーリーの steps）をなぞる判定とは別に、人のいない夜にチャーター（その夜の狙い）に沿って利用者として寄り道し、ストーリーが書いていないところの食い違いを探す。形と数値は `definitions/qa.yaml` の `explore`。

```bash
docker compose exec dispatcher factoryctl qa explore guccyon/rally [--through-pause]   # 今すぐ 1 回（人が積んだものは無人の夜に数えない）
docker compose exec dispatcher factoryctl qa show guccyon/rally                        # 判定と最近の探索
```

- supervisor が JST の `explore.hours`（既定 1〜5 時）に、`NIGHTLY_EXPLORE` の製品（既定は `RELEASE_TRAIN` と同じ。空で止める）ごとに 1 晩 1 回、固定 Issue（`factory:qa`）に QA のジョブ（入力の `mode: explore`、優先度 p2）を積む。チャーターは夜ごとに順番に替える（寄り道・別の利用者・戻ってくる・読み手の言葉・初めての人）
- 予算の枠を使い切っていれば積まずに次の枠を待つ。ラインの一時停止中は `explore.through_pause` が真ならこのジョブだけ流す（`drain allow`）。積まなかった理由は `supervisor.nightly_explore_skipped` に 1 晩 1 回
- ワーカーは判定と同じ `worker/qa.sh` → `core-gates/qa.py`（Mac ランナーの中継があれば生徒アプリは iOS でも）。結果は `out/explore.yaml`（試したこと・見つけたもの・印）
- dispatcher が artifacts の `QAExplore` と events の `qa.explore`（1 晩 1 件。roadmap の `nightly_explore` は supervisor が積んだ夜の数を数える）を残す。見つけたもののうち s1〜s3 で known_gap でないものを判定と同じラベルで起票する（1 晩 3 件まで。同じ題の開いた Defect があれば起票せずにその Issue に書く）。固定 Issue にまとめを書く
- 朝 5 時の日報（`health/daily/status_report.py`）とページに「夜間の探索（昨夜）」として、Defect（s1・s2 は太字）と印、回らなかった理由を出す。印は人が採点して Golden Set に入れる材料

## Learn ループ（ADR 0013）

supervisor が 1 日 1 回（日報の後）、直近 30 日の教師信号を制御プレーンの記録から集め、signature（`rubric:<id>`、`judgment:<類型>`）で束ねて artifacts の `Retrospective` と events の `learn.retrospected` に残す。LLM は使わない。
教師信号は今のところ judge が採用した検査の指摘、judge が差し替えた仮決め、人が judge に同意しなかった採点の 3 つ。束ごとに eval ケースの元（元 PR と差し戻しの修正 PR の組）と、Stage 2（rubric）の候補か（3 件以上・2 チケット以上）を出す。
束ねた日に、Stage 2 の候補で負例の元がある束を 1 つ選び、製品に「ルール化」の Issue（`factory:planned`・`factory:learn`）を起票する（1 日 1 本まで、`LEARN_RULE_ISSUES=0` で止める）。通常の implement ジョブがルール変更 PR を書き、CI の rule-eval が緑なら factory_rule として自動でマージされる。
検査は製品の overlay の shadow・adopted のルールもルーブリックに足し、shadow のルールの指摘は judge が裁くだけで差し戻さない。成績（発火・的中率）は report learning に出る。adopted への昇格とカナリアの戻しは、supervisor が 1 時間に 1 回判定して `state` を書き換える Issue を起票する（`LEARN_RULE_TRANSITIONS=0` で止める。閾値は `definitions/learning.yaml`）。

事例型の分析役（ADR 0023）: supervisor が 1 日に数件（`definitions/learning_taxonomy.yaml` の per_day）、差し戻し（後片付けが会話の記録を保存した後）と 60 分以上の停止（予算・一時停止を除く）を選び、`case_review` のジョブを積む。dispatcher は起こすときに後片付けのアーカイブからセッションの抜粋（エージェントの文とツールの名前だけ。秘密情報に見える文字列は伏せる）を入力に足す。結果（観点ごとのなぜなぜ・真因・テーマごとの学びの候補）は events の `case.analyzed` と artifacts の `CaseAnalysis` に残すだけ。`CASE_REVIEW=0` で止める。

知見の変更セット（ADR 0024 の 3）: supervisor が `case.analyzed` のうち最初に回すテーマ（`learning_taxonomy.yaml` の `status: first`、いまは 4 と 6）の学びがあるものを、製品の Issue（`factory:planned`・`factory:learn`）にする。本文の個人情報・環境の名前に見える形は伏せる。工場の implement ジョブが事例記録 `cases/C-NNNN.md` と知見の下書き（`status: draft`・`confidence: hypothesis`）を 1 本の PR に書き、`knowledge_check.py --index` で `INDEX.md` を作り直す。PR は変更種別 `knowledge`（`change_type_rules.yaml`）で、委員会ができるまでは manifest に無い種別として人の承認に倒れる。1 日に `KNOWLEDGE_ISSUES_PER_DAY`（既定 2）件まで。`KNOWLEDGE_ISSUES=0` で止める。置き場（`.factory-config/knowledge/README.md`）の無い製品には起票しない。

知見の委員会（ADR 0024 の 4）: 変更種別 `knowledge` の工場の PR は、CI を通った後に supervisor が `committee_review` のジョブを PR ごとに 1 回積む（同じ PR の検査が動いている間は次の周回で）。ワーカーの `core-gates/committee_review.py` が、変更の差分・知見の置き場・`specs/` の写しを 4 役（整合・根拠・担保・反対）の別々のエージェントに読ませ、票を決定論で集める（`knowledge.yaml` の `committee.aggregation`）。差し戻し以外は議決 `committee/D-NNNN.yaml` と、下書きを active にした知見を PR のブランチに積む。dispatcher は PR にまとめを書き、sendback は閉じ、human は承認のラベル、enforce は担保の Issue を起票する。変更種別 `knowledge` を自動でマージするのは委員会が採用したときだけ（manifest の表で `knowledge` を auto にしたとき）。`COMMITTEE=0` で止める。

知見の委員会の週 1 の定例（ADR 0024）: supervisor が週 1 回（`knowledge.yaml` の `committee.weekly`、既定は日曜 3 時 JST）、知見の置き場のある製品ごとに固定 Issue（`factory:knowledge-committee`）へ `committee_weekly` のジョブを積む。ワーカーの `core-gates/committee_weekly.py` が、期限切れと数を決定論で出し、議長役のエージェントが矛盾・確かさを上げる・廃止・担保・置き場の構造の候補を出す。dispatcher は固定 Issue にまとめを書き、見直しを 1 本の変更セットの Issue に、担保と構造の変更をそれぞれ Issue にする（1 回に `max_issues` 件まで）。`COMMITTEE_WEEKLY=0` で止める。

```bash
docker compose exec dispatcher factoryctl report learning --days 30
```

知見をワーカーに渡す（ADR 0024 の 6）: implement のワーカーは、製品の知見のうち active で確認済み以上（settled で決定論の担保が効いているもの・再確認の期限を過ぎたもの・client の区分は除く）を `core-gates/knowledge_context.py` で選び、確かさと確認日を付けて `/work/context/knowledge.md` に置く。プロンプトには場所と扱い方（目の前の事実を優先し、食い違いは「## 補足」に書く）だけを書く。渡した id は events の `knowledge.used` に残る。`KNOWLEDGE_CONTEXT=0` で止める。

知見の制限付きの保管先（ADR 0024 の 5）: 障害対応などで利用者の情報をどうしても含む知見の根拠は、git に置かず制御プレーンの Postgres の専用スキーマ `restricted` に置き、git の知見からは evidence の `restricted: R-NNNN` で指すだけにする。
守り方は利用者を分けること。`restricted` は保管先の専用の利用者 `factory_restricted` の持ち物で、ほかの利用者（PUBLIC）からは何もできない。`factory_restricted` は最初は NOLOGIN で、管理者が合言葉を決めて有効にし、合言葉は管理者だけが持つ（`.env` に置かない）。ワーカーのジョブの DB は別の利用者 `factory_job` で、制御プレーンの DB には繋げない（「ジョブ用の DB」）。
一覧には中身を出さず、読むたびに誰が・何のために読んだかを `restricted.reads` と events の `restricted.read` に残す。ワーカー・操業コンソール・窓口の API には出さない（factoryctl だけ）。制御プレーンのサービスは今も Postgres の管理者で動くので、そこからは見える（サービスの利用者を分けるのは別の段）。

```bash
docker compose exec postgres psql -U factory -f /docker-entrypoint-initdb.d/005_restricted.sql   # 既存の DB にスキーマと利用者を足す（冪等）
docker compose exec postgres psql -U factory -c "alter role factory_restricted login password '...'"   # 管理者が 1 度だけ。合言葉は手元に
export RESTRICTED_DATABASE_URL='postgresql://factory_restricted:...@postgres:5432/factory'   # シェルの履歴に残さない
docker compose exec -T -e RESTRICTED_DATABASE_URL dispatcher factoryctl restricted add guccyon/rally --knowledge K-0012 < incident.md   # R-NNNN が出る
docker compose exec -e RESTRICTED_DATABASE_URL dispatcher factoryctl restricted list
docker compose exec -e RESTRICTED_DATABASE_URL dispatcher factoryctl restricted show R-0001 --purpose "再発の確認"
docker compose exec -e RESTRICTED_DATABASE_URL dispatcher factoryctl restricted retire R-0001 --reason "保管の期限"   # 中身を消し、id と記録は残す
```

## 建設状況の日次レポート（ADR 0008）

supervisor が JST の 5 時（`DAILY_REPORT_HOUR`）を過ぎたら 1 日 1 回 `health/daily/status_report.py` を走らせ、factory-engine の固定 Issue（`factory:construction`）の本文をダッシュボードに書き換え、日次レポートをコメントで足す。LLM は使わない。
進捗は `definitions/roadmap.yaml` から出す。項目が済んだら manual を書き換える PR を出す。

```bash
docker compose exec supervisor python /health/daily/status_report.py --dry-run           # 書かずに中身を見る
docker compose exec supervisor python /health/daily/status_report.py --publish --force   # 今日の分を出し直す
```

## Mac ランナー（ADR 0028 の決定 3）

iOS シミュレーターを工場から動かすための、macOS の常駐プロセス（何台でも）。ランナーは工場のランナーの窓口（compose の `runner-gate`、:8794）へ TLS で仕事を取りに行き、工場とはペアリングで繋ぐ（ADR 0033）。道具は決まった一覧だけで、ビルドはマージ済みのコミットだけ。入れ方・道具・守りは [macrunner/README.md](macrunner/README.md)。

```bash
scripts/macrunner.sh install                               # Mac で。6 桁の番号が出るので、操業コンソールで同じ番号の依頼を「許可」して y
docker compose exec dispatcher factoryctl runner check     # 届くか: 窓口・ペアリング済みの台数・道具・端末
```

## ジョブ用の DB

dispatcher はジョブごとに制御プレーンと同じ Postgres に `job_<id>` を作り、ワーカーに `DATABASE_URL` で渡す（終了時に落とす）。ワーカーは自前で DB を立てずに DB のテストを動かせる。`JOB_DB_ENABLED=0` で止められる。
ジョブの DB は管理者でない専用の利用者 `factory_job`（LOGIN と CREATEDB だけ。合言葉は `JOB_DB_PASSWORD`）の持ち物にし、制御プレーンの DB（`factory`）の CONNECT は PUBLIC から外す（`dispatcher/jobdb.py`。dispatcher が最初のジョブの DB を作るときに利用者を作るかそろえる）。ワーカーとその中のエージェントは、制御プレーンの events や窓口の会話、知見の保管先を読めない。CREATEDB は rally の `sqlx::test` がテストごとに DB を作るため。

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
