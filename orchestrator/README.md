# orchestrator — P0 の制御プレーン（Docker Compose）

Issue にラベルを付けると、デスクトップ操作なしでコンテナ上の Claude Code が PR を出す経路。判断は [ADR 0003](../docs/adr/0003-p0-control-plane-postgres-queue.md)。

```
GitHub Issue（factory:implement）
   │ poll 30s / webhook
   ▼
dispatcher ──claim(SKIP LOCKED)──▶ postgres（jobs / events / artifacts）
   │ docker run（--rm なし、ADR 0018）    ▲
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
- どのリポジトリにどのイメージを使うかは `.env` の `WORKER_IMAGES`（例: `guccyon/rally=factory-worker:rust`）
- Issue ごとに変えるときは、Issue に `factory:image:<名前>`（例: `factory:image:flutter`）のラベルを付ける。リポジトリの設定より優先する

## 手動操作

```bash
docker compose exec dispatcher factoryctl list          # 進行中と直近 1 日
docker compose exec dispatcher factoryctl stop 12       # 停止（コンテナを kill）
docker compose exec dispatcher factoryctl rerun 12      # 同じ Issue で attempt+1 を投入
docker compose exec dispatcher factoryctl enqueue guccyon/rally 3   # ラベルを使わずに投入
docker compose exec dispatcher factoryctl events 12     # そのジョブのイベント
docker compose exec dispatcher factoryctl drain --wait 3000   # ラインを一時停止して、入れ替えてよくなるまで待つ（ADR 0018）
docker compose exec dispatcher factoryctl resume        # ラインを再開
```

## 工場の入れ替え（ADR 0018）

ワーカーのコンテナは dispatcher から独立して動く（`--rm` なし。名前 `factory-job-<id>`、ラベル `factory.job=<id>`）。dispatcher を上げ直すと、
running のジョブのコンテナにつなぎ直し（`job.reattached`）、出力を最初から読み直して終わりを待ち、いつもどおり記録する。コンテナが残っていないジョブだけ
`orphaned_by_restart` で積み直す。終わったコンテナは dispatcher が記録を終えてから消す。

入れ替え方は 2 通り:

- **工場のコードだけ**（dispatcher・supervisor・console・shipper、ワーカーのイメージの作り直し）: 一時停止は要らない。実行中のジョブはそのまま続く。出荷の処理中だけは shipper を上げ直さない（`drain status` で見る）

  ```bash
  git pull && docker compose build && docker compose up -d dispatcher supervisor console
  ```

- **制御プレーンや Mac ごと**（postgres・オブジェクトストア・Docker・OS の再起動）: ラインを一時停止して、入れ替えてよくなるまで待つ

  ```bash
  docker compose exec dispatcher factoryctl drain --reason "postgres の入れ替え" --wait 3000   # 入れ替えてよければ終了コード 0
  # 入れ替える
  docker compose exec dispatcher factoryctl resume
  ```

- 一時停止の間、dispatcher は順番待ちを取り出さず、割り込みもしない。ラベルでの投入、supervisor の判定・マージは続く。一時停止は上げ直しをまたいで残り、再開は明示の操作だけ
- 「入れ替えてよい」は、一時停止中で、実行中のジョブが 0 件で、出荷の依頼が処理中でないとき（`factoryctl drain status`）
- 操業コンソールの上の「ライン」のトグルでも同じ操作ができる（API は `POST /api/factory/drain`・`/api/factory/resume`、状態は `GET /api/state` の `factory.line`）
- 再開を忘れたら、番人が `WATCH_DRAIN_MIN` 分（既定 30）で停止 `drain:` を開く（分類 human）。再開すると閉じる

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
  - `stall:<id>` 実行中のジョブの出力が 15 分（`WATCH_STALL_MIN`）止まっている。出力が再開するかジョブが終われば閉じる（完了まで毎周期見る）
  - `merge:<PR>` supervisor が工場はマージしない（execute=false）と記録した PR が、30 分（`WATCH_MERGE_WAIT_MIN`）マージも閉じもされない。分類は human。マージか閉じられたら閉じる
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
- 画面: 人の出番・作業中・順番待ち・投入待ち（同時に流す上限）・依存待ち・検査と判定とマージ待ち・今日の済み。上に工場長の生存・モード・作業中の数・今日の費用・開いている停止
- 操作: 優先度（p0 緊急・p1 急ぎ・p2 通常・p3 後回し）、保留・再開、止める。すべて events に `ticket.*` / `job.cancel_requested` として残る
- API: `GET /api/state`、`POST /api/tickets/<id>/priority`（`{"level":"p1"}`）、`POST /api/tickets/<id>/hold`・`resume`、`POST /api/jobs/<id>/stop`。操作には画面に埋めた合言葉のヘッダー `X-Console-Token` が要る

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

## PR の見張り（ADR 0015）

工場の PR に起きた動き（push、CI の結果、衝突、レビュー）を GitHub から拾い、抽象化したイベント（`pr.head_updated` / `pr.ci_failed` / `pr.ci_passed` / `pr.conflicted` / `pr.review_submitted`）として events に発行する、決定論的なモジュール（`dispatcher/pr_events.py`。dispatcher の中のスレッド）。オーケストレーター（supervisor）は GitHub を見ずにこのイベントを読む。

- 入口は 2 つ。Webhook（`WEBHOOK_SECRET` があれば dispatcher の `/webhook` が `pull_request` / `pull_request_review` / `check_suite` / `check_run` / `workflow_run` を受ける。商用・クラウドで即時）と、ポーリング（`PR_EVENTS_INTERVAL_SEC`、既定 60 秒。オンプレミスや手元）。どちらも「見に行く合図」で、事実は GitHub の今の状態を取り直して決める。同じ事実は dedupe で 1 回しか発行しないので、両方動いても二重にならない
- `PR_EVENTS_SOURCE=both|poll|webhook`（既定 both。webhook だけだと見逃しを拾えない）、`PR_EVENTS_ENABLED=0` で止める

## 工場長の supervisor（ADR 0004）

判断は持たず、状態と表を見て工程を起動するだけの常駐プロセス（compose の `supervisor`。dispatcher と同じイメージ）。60 秒ごとに:

- `supervisor.heartbeat` を出す（途絶えたら番人が停止を開く）
- 工場の PR が `mergeable_state=clean`（CI 通過・衝突なし・ドラフトでない）になったら、変更ファイルを `definitions/change_type_rules.yaml` で変更種別に写し、製品の manifest を引いて `supervisor.merge_decided` を記録する。表が承認を求めれば PR に `factory:needs-approval`。表が auto で、かつ工場の権限（`phase_grants` の merge）も auto のときは、**検査役の承認**（下の「検査役」）が出た周期に squash でマージする（ADR 0014）。それ以外は記録だけ（shadow）
- 検査役（ADR 0014。2026-10-31 オーナー）: 人がやる承認を委任する役で、いまは検査（inspect）と judge の組み合わせ。その head で検査と judge が終わり、judge が人に回した判断がなければ承認（`supervisor.inspector_approved`）し、表が auto の PR はその後にマージする。人に回した判断があれば停止 `judge:` を人が閉じるまで待つ。採用された指摘は差し戻しで直すのでマージは止めない（D7）。検査がなければ（`SUPERVISOR_INSPECT=0`）CI 緑で承認とみなす
- マージを判定した head ごとに検査ジョブ（kind=inspect）を積む。dispatcher がワーカーの `inspect.sh` で `core-gates/shadow_review.py`（Engine の main から毎回取る）を動かし、`gate.evaluated`（mode=shadow）と InspectionReport を残す。GitHub には書かない。`SUPERVISOR_INSPECT=0` で止める
- 判定した PR が閉じたら `pr.merged`（誰がマージしたか）/ `pr.closed_unmerged` を記録する。判定と人の判断の一致率の材料
- `factory:planned` の Issue の本文の「依存: #12 #13」がすべて閉じたら、優先度の高い順に `factory:implement` に替える。同時に流すのは `SUPERVISOR_MAX_INFLIGHT`（既定 1。実行中・投入済みのジョブと開いている工場の PR の合計。ただし人を待っている PR＝judge が人に回した・差し戻しを流さなかった・CI 落ち・衝突・表が承認を求めた、は数えない。2026-10-31）まで。p0 は上限を待たない。保留中は流さない
- 自律的な再開（ADR 0011）: 停止を閉じるときは `factoryctl stoppage close <key> --woken-by pr_event|watcher|schedule|human` で、builder を起こしたものを残す（人が起こした割合が `report stoppages` に出る）。番人の通知先は `STATUS_NUMBER`（Issue でも PR でもよい。空なら factory:status の Issue）。ホストのスリープは `scripts/keep-awake.sh install` で防ぐ（launchd が常駐させ、工場のコンテナが動いている間だけ `caffeinate` でスリープを止める。止まっていれば普段どおり眠る。`uninstall` で元に戻る、`status` で確認。操業コンソールのトグル「稼働中は Mac をスリープさせない」で切り替えられる。トグルは見張りから連絡があるときだけ出る）
- 使用量の枠ごとの予算（2026-10-29 オーナー）: Max の使用量は毎朝 7:00 から 5 時間ごとにリセットされるので、JST の枠（7・12・17・22・3 時）ごとに予算を置き、その枠の `job.finished` の費用（API 換算）の合計が予算に達したら、次の枠まで新しい実装を投入しない（作業中のもの・検査・judge・p0 は止めない）。既定は平日 $10・$15・$35・$25・$15、週末は各 $20（1 日 約 $100、平日は夕方以降に寄せる）。`BUDGET_WINDOWS_WEEKDAY` / `BUDGET_WINDOWS_WEEKEND`（「開始時:ドル」の並び。空で無効）。止めたことは `supervisor.budget_paused`

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

## Learn ループ（ADR 0013）

supervisor が 1 日 1 回（日報の後）、直近 30 日の教師信号を制御プレーンの記録から集め、signature（`rubric:<id>`、`judgment:<類型>`）で束ねて artifacts の `Retrospective` と events の `learn.retrospected` に残す。LLM は使わない。
教師信号は今のところ judge が採用した検査の指摘、judge が差し替えた仮決め、人が judge に同意しなかった採点の 3 つ。束ごとに eval ケースの元（元 PR と差し戻しの修正 PR の組）と、Stage 2（rubric）の候補か（3 件以上・2 チケット以上）を出す。
束ねた日に、Stage 2 の候補で負例の元がある束を 1 つ選び、製品に「ルール化」の Issue（`factory:planned`・`factory:learn`）を起票する（1 日 1 本まで、`LEARN_RULE_ISSUES=0` で止める）。通常の implement ジョブがルール変更 PR を書き、CI の rule-eval が緑なら factory_rule として自動でマージされる。
検査は製品の overlay の shadow・adopted のルールもルーブリックに足し、shadow のルールの指摘は judge が裁くだけで差し戻さない。成績（発火・的中率）は report learning に出る。adopted への昇格の自動化はこの後に足す（形と閾値は `definitions/learning.yaml`）。

```bash
docker compose exec dispatcher factoryctl report learning --days 30
```

## 建設状況の日次レポート（ADR 0008）

supervisor が JST の 5 時（`DAILY_REPORT_HOUR`）を過ぎたら 1 日 1 回 `health/daily/status_report.py` を走らせ、factory-engine の固定 Issue（`factory:construction`）の本文をダッシュボードに書き換え、日次レポートをコメントで足す。LLM は使わない。
進捗は `definitions/roadmap.yaml` から出す。項目が済んだら manual を書き換える PR を出す。

```bash
docker compose exec supervisor python /health/daily/status_report.py --dry-run           # 書かずに中身を見る
docker compose exec supervisor python /health/daily/status_report.py --publish --force   # 今日の分を出し直す
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
