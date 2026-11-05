# ADR 0032: 報告・通知を種類ごとに宛先へ出し分ける（通知のルーティング）

- 状態: 提案（2026-11-05。R1・R2 は決まり、R3〜R7 はオーナーの確認待ち）
- 関係: ADR 0005（工場状況 Issue と停止の通知）、ADR 0008（建設状況の日次レポート）、ADR 0026 決定 7（受信トレイの通知）、ADR 0029（流量の宣言）、ADR 0030（事業部の窓口）、Issue #174

## 文脈

工場を事業主に提供するようになると、報告や通知を読む人が 2 つの組織にまたがる。工場を作って運営する側（運営元）と、工場を自分の開発部門として使う側（事業主）。

オーナーの要望（2026-11-05）:

| 通知の種類 | 宛先 |
|---|---|
| アプリ開発（プロダクト開発）の進捗 | 事業主の Slack と運営元の Slack の両方 |
| 工場の健康状態 | 運営元の Slack のみ。事業主が操業も自走する場合は事業主の Slack |
| 工場の建設の進捗 | 運営元の Slack |

今の通知の経路は、どれも工場全体で 1 つに固定されている。

- 停止の始まり・終わりと、停止ではない知らせ（段の残りが少ないなど）: `stoppage.notify` / `stoppage.post_notice`。`NOTIFIER`（`github-issue` / `log`）で工場状況 Issue へ
- 受信トレイの人の出番: `notify_inbox.tick`。`INBOX_NOTIFY`（`log` / `github-issue` / `slack`）と `SLACK_WEBHOOK_URL` 1 つ、`INBOX_QUIET`・`INBOX_SLACK_MENTIONS` も工場に 1 つ
- 建設状況の日次レポート: supervisor が `report.daily` を作り `factory:construction` の Issue に書く。claude.ai のページ（建設日報）はクラウドが描く。Slack には届かない
- 朝の報告（プロダクト開発・操業）: 工場の外（クラウドの定期タスク）が組み立てる。Slack には届かない

Slack への通知は Claude 側のコネクタではなく、工場自身が Incoming Webhook で送る。事業主ごとに宛先を持てるのは工場の側だけのため。

## 決定

### 1. 連携（credential）と宛先を分けて定義する

「どの組織のどのサービスに、どの credential で送れるか」（連携）と、「どこに届けるか」（宛先）を分ける。Webhook の URL を通知ごとに発行しなくても、連携を 1 つ登録すれば、その組織の中の任意のチャンネルを宛先にできる。

```yaml
connections:                # 連携。credential の値は書かず、秘密の置き場の参照（secret）だけ
  provider_slack:
    party: provider         # provider（運営元）/ business_owner（事業主）
    via: slack_app          # Slack アプリ（ボットのトークン）。chat.postMessage で任意のチャンネルに送る
    secret: env:SLACK_BOT_TOKEN_PROVIDER
  owner_slack:
    party: business_owner
    via: slack_app
    secret: aws-sm:factory/rally/slack-owner   # 例 AWS Secrets Manager の名前（後で）
  owner_alerts_hook:
    party: business_owner
    via: slack_webhook      # Incoming Webhook。URL ごとに投稿先のチャンネルが決まる
    secret: env:SLACK_WEBHOOK_URL_OWNER_ALERTS

destinations:               # 宛先。通知の割り当てはこの名前で書く
  provider_dev:
    connection: provider_slack
    channel: "#factory-dev"           # slack_app のときだけ。チャンネル名か ID
    quiet: "22-7"                     # 送らない時間（REPORT_TZ の時）。明けた最初の周回でまとめて送る
    mentions: {builder: "<@U…>"}      # 誰の出番かごとのメンション
  provider_ops:
    connection: provider_slack
    channel: "#factory-ops"
  owner_dev:
    connection: owner_slack
    channel: "#dev-progress"
  owner_alerts:
    connection: owner_alerts_hook     # Webhook はチャンネルを書かない
```

- `secret` は `<置き場>:<名前>` の参照。値は送る直前に置き場から引き、制御プレーン・ログ・画面には出さない。置き場は差し替えられる口（`env` は環境変数＝Gen1 の `.env`、`aws-sm` は AWS Secrets Manager など）にし、Gen1 は `env` だけを作る
- 連携の種類（`via`）は `slack_app`・`slack_webhook`・`github-issue`・`log`。今の `INBOX_NOTIFY` の `slack` は `slack_webhook` にあたる
- Gen1 で試すときは `slack_webhook` と `env` だけを使う（オーナー 2026-11-05）。`slack_app` は形だけ決めておき、作るのは後
- 宛先の組織（`party`）は連携から引き継ぐ。これが中身の境界（決定 3）の鍵になる
- 読み込むときに形を検査する（`slack_webhook` は `https://hooks.slack.com/` で始まること、`slack_app` は `channel` があること）。`slack_app` のトークンには `chat:write` だけを付け、ボットを招いたチャンネルにだけ送れる形にする

### 2. 通知は 3 つの系統に分け、種類は Engine の定義に置く。どの組織に出してよいかは種類が持つ

通知は大きく 3 つの系統に分ける（オーナー 2026-11-05）。

| 系統 | 何か | 今あるもの |
|---|---|---|
| `report` 定期レポート | 決まった周期で出す報告 | 朝の報告（クラウドで組み立て）、建設日報 |
| `request` 承認依頼 | ラインなどで人の判断待ちが出たときの依頼 | 受信トレイ（`notify_inbox`） |
| `done` 作業の完了報告 | 頼まれた作業（窓口の依頼。例 データを抜き出して CSV にする）が終わったときの報告。頼まれたものだけ | なし（ADR 0030 の窓口の依頼の後に作る） |

全工場に共通の種類の一覧を `definitions/notifications.yaml` に置く（ADR 0001 の決定 2）。工場ごとに変えるのは割り当てだけ。種類は `<系統>.<名前>` で書く。

| 種類 | 中身 | 出してよい組織 | 周期・きっかけ | 今回 |
|---|---|---|---|---|
| `report.factory_health` | 工場の健康状態（ジョブの成否、停止の件数と時間、費用、受信トレイの滞留） | 運営元。事業主は操業を自走するときだけ | 毎日（後で頻度を落とすかもしれないので周期は種類の設定に持つ） | 作る |
| `report.product` | プロダクト開発の進捗（まずはロードマップの進捗。前日にマージしたもの、進めているもの、次に流すもの） | 運営元・事業主 | 毎日 | 作る |
| `request.inbox` | 受信トレイの人の出番（選択・出荷の承認・停止の判断・依頼） | 運営元・事業主（決定 5） | 出たとき・知らせ直し・代理への切り替え | 今の通知を載せ替える |
| `done.task` | 窓口の依頼の完了報告 | 依頼した人の組織 | 終わったとき | 後で |

- リリースして運用の段に入ると定期レポートは増える見込み（未定）。足すときは `report.*` に 1 行と文面の関数を 1 つ足す
- 工場の建設の進捗は工場の通知に載せない。建設は工場の外（建設者）の仕事で、建設日報は今どおり建設者が出す（R6）
- 停止の始まり・終わりと停止ではない知らせ（段の残りが少ないなど）は、どの系統にも入れない。人の判断が要る停止は受信トレイに出るので `request.inbox` で届く。それ以外は今どおり工場状況 Issue に書き、件数と時間は `report.factory_health` に載せる

### 3. 事業主の宛先に工場の内部の情報を混ぜない

- 種類ごとに「出してよい組織」を持ち、`business_owner` の宛先に出してはいけない種類を割り当てた設定は、読み込むときに拒む（通知を送らずにログと受信トレイの `error` に出す。ラインは止めない）
- 文面は宛先の `party` を引数にして作る。事業主の宛先には、運営元の操業コンソール（工場のマシンの中）へのリンク・費用・建設中の不具合を載せない（R5・R7）
- 運営元の宛先は複数の工場の通知が混ざる前提で、文面の頭に工場の名前（`FACTORY_NAME`）を付ける。事業主の宛先はその事業主の工場の通知だけを受ける（工場は 1 事業主・1 プロダクト）

### 4. 運用形態を 1 つ宣言すれば既定の割り当てが決まり、種類ごとに複数の宛先へ上書きできる

1 つの種類は複数の宛先に送れる（例 プロダクト開発の進捗を、事業主の `#dev-progress` と運営元の `#factory-dev` の両方へ）。割り当ては宛先ごとに有効・無効を持つ。

```yaml
operated_by: provider        # provider（運営元が操業する）/ business_owner（事業主が操業も自走する）
routes:                      # 書かない種類は operated_by から決まる既定
  report.product:
    - {to: owner_dev}
    - {to: provider_dev}
  report.factory_health:
    - {to: provider_ops}
    - {to: owner_alerts, enabled: false}   # いったん止めておく
```

既定:

| 種類 | `operated_by: provider` | `operated_by: business_owner` |
|---|---|---|
| `report.product` | 運営元と事業主の両方 | 運営元と事業主の両方 |
| `report.factory_health` | 運営元 | 事業主（運営元にも写しを送るかは R3） |
| `request.inbox` | 誰の出番かで分ける（決定 5） | 誰の出番かで分ける（決定 5） |
| `done.task` | 依頼した人の組織 | 依頼した人の組織 |

既定の「運営元」「事業主」は、その `party` を持つ宛先すべてを指す。

### 5. 受信トレイは、誰の出番か（who）で宛先を分ける

受信トレイの件は工場の健康状態だけではない。画面の案の選択（プロダクトオーナー）や見た目の選択（デザインのチーフ）は事業主の側の人の出番になる。`who` ごとに宛先の組織を決める。

| who | 既定の宛先 |
|---|---|
| `po`・`design_chief`・`requester` | 事業主 |
| `builder` | 運営元 |
| `owner`（停止の判断・出荷の承認など） | `operated_by` に従う（運営元が操業するなら運営元） |

代理への切り替え（`INBOX_PROXY`、ADR 0026 決定 7）は、代理の `who` の宛先にも送る。

### 6. 設定は製品のリポジトリに置き、制御プレーンはその写し（キャッシュ）を持つ

工場はプロダクトごとに専用のものを建てる。同じ工場を並べて動かす（並列稼働）こともありうるので、設定の正本はマシンの中ではなくリポジトリに置く。

- 正本は製品のリポジトリの `.factory-config/notifications.yaml`（`ship.yaml`・`manifest.yaml` と同じ置き場）。連携の名前と credential の変数の名前・宛先・運用形態・割り当て（有効・無効を含む）を書く。credential の値は書かない
- credential の値は秘密の置き場（決定 1 の `secret`。Gen1 は `.env`、将来は AWS Secrets Manager など）。並べて動かす工場は同じ置き場を引く
- dispatcher と supervisor は、既定ブランチの今の版を周回ごとに読み（変わったときだけ）、検査を通った版を制御プレーンに写す（`notify.config_changed` に commit・内容のハッシュ・連携と宛先の名前・割り当て。秘密の値は残さない）。送るときはこの写しを引く。並べて動かす工場も同じ版に揃う
- 読めない・検査を通らないときは、前に通った写しのまま送り続け、受信トレイの `error` に出す
- 行き先: 操業コンソールに通知の設定画面を置き、種類ごとに宛先を選び、有効・無効を切り替える。画面での変更は `notifications.yaml` を書き換える PR になり、マージされたら写しが更新される（正本はリポジトリのまま。変えた人・時刻・理由は PR に残る）。連携の credential を画面から登録するときも値は秘密の置き場に書き、リポジトリ・制御プレーン・画面には出さない

### 7. 送る口は 1 か所、失敗でラインを止めない

- `notify.send(kind, payload, who=None)` を 1 つ置き、割り当てを引いて宛先ごとに文面を作って送る。`stoppage`・`notify_inbox`・ship・supervisor の朝の処理はここを呼ぶ
- 宛先ごとに送る。1 つの宛先が失敗しても他は送る。記録は events の `notify.sent`（種類・宛先の名前・成否・dedupe_key）。受信トレイの `inbox.notified` はそのまま残す（`channels` に宛先の名前を入れる）
- `quiet` の時間は宛先ごとに見る。夜は事業主にだけ送らず、運営元には送る、ができる

### 8. 今の設定のまま動き続ける

`notifications.yaml` がないときは、今の環境変数から連携と宛先 `default`（`party: provider`）を 1 つずつ組み立て、すべての種類をそこに割り当てる。

- 受信トレイ: `INBOX_NOTIFY`・`SLACK_WEBHOOK_URL`・`INBOX_QUIET`・`INBOX_SLACK_MENTIONS` をそのまま使う
- 停止と知らせ: `NOTIFIER`・`STATUS_REPO` をそのまま使う
- 新しい種類（`report.product`・`report.factory_health`）は、`notifications.yaml` で宛先を書くまで送らない（今は届いていないものが勝手に届き始めないように）

### 9. 定期レポートは工場が組み立てて送る

朝の報告は今クラウドで組み立てているが、元のデータ（ジョブ・停止・費用・マージした PR・ロードマップ）は工場の制御プレーンと GitHub にある。Slack の credential を工場の 1 か所に保つため、定期レポートは工場（supervisor の周回）が組み立てて送る。

- `report.factory_health`・`report.product`: 朝の報告と同じ数を工場が要約して送る。朝の報告のプロダクト開発タブを工場から出す件（積み残し）と同じ組み立てを使う
- 出す時刻と周期は種類ごとに設定で持つ（既定は毎朝、`DAILY_REPORT_HOUR` の後）
- 朝の報告のページ（claude.ai）は当面そのまま。Slack の文面は要約で、詳しくはページか（事業主向けには）プロダクトのリポジトリへのリンク（R7）

## 決めどころ（オーナーの承認が要る）

| # | 問い | 推奨 |
|---|---|---|
| R1 | 設定の置き場と形 | 決まり（2026-11-05 オーナー「工場の設定ファイルで問題ない。1 つの通知を複数の宛先に送れること。Webhook だけでなく、Slack 連携でチャンネル名を指定して送れる credential の登録も残す。最終的にはダッシュボードの設定画面から通知ごとに通知先と有効・無効を変えたい」）。補足（同日オーナー「DB に移すのはキャッシュの扱いが良さそう。工場は並列稼働する可能性が 0 ではないので、設定はリポジトリのソースコードに置く方が都合が良い」）。正本は製品のリポジトリの `.factory-config/notifications.yaml`、制御プレーンは写し。連携と宛先を分け、割り当ては複数の宛先と有効・無効を持つ。設定画面の変更はこのファイルへの PR にする |
| R2 | 通知の種類の一覧 | 決まり（2026-11-05 オーナー「通知は定期レポート・承認依頼・作業の完了報告の 3 種類。まず欲しいのは定期レポートで、工場の健康状態とプロダクト開発（ロードマップ進捗）の日次レポートの 2 つ」）。決定 2 の系統と種類。今回作るのは `report.factory_health`・`report.product` と、受信トレイの通知の載せ替え（`request.inbox`） |
| R3 | 事業主が操業も自走するとき、健康状態を運営元にも写すか | 写す（運営元は保守の契約がある限り工場の具合を知る必要がある）。契約で保守をしないときは `routes` で外す |
| R4 | 受信トレイの振り分け | 誰の出番か（who）で分ける。PO・デザインのチーフ・起票者は事業主、建設者は運営元、オーナーは運用形態に従う |
| R5 | 事業主に費用を見せるか | `report.product` には載せない。費用は `report.factory_health` に載せ、事業主が操業を自走するときだけ事業主に届く。パイロットで事業主が実費を見たいなら、週 1 回の費用の要約を別の種類として足す |
| R6 | 建設の進捗を工場の通知に載せるか | 載せない。建設は工場の外（建設者）の仕事で、建設日報は今どおり建設者が出す。工場の通知は工場自体（健康・プロダクト開発）だけ |
| R7 | 事業主の宛先に付けるリンク | 操業コンソールは工場のマシンの中なので付けない。プロダクトのリポジトリ（PR・Issue）へのリンクだけ。コンソールを外に開いたら（ADR 0030 の窓口のログイン）付ける |

## 順番

1. 宛先と種類の定義、`notifications.yaml` の読み込みと検査・制御プレーンへの写し、`notify.send`、環境変数からの宛先 `default`（決定 8）。停止・知らせ・受信トレイをこの口に載せ替える（挙動は今と同じ）
2. 受信トレイの who による振り分け（決定 5）、宛先ごとの `quiet`・メンション
3. `report.factory_health`（朝の報告の操業タブと同じ数）
4. `report.product`（ロードマップの進捗。朝の報告のプロダクト開発タブを工場から出す件と合わせる）
5. 後で: Slack アプリの連携（`slack_app`）、秘密の置き場の `aws-sm`、操業コンソールの通知の設定画面（種類ごとの宛先と有効・無効）。変更は `notifications.yaml` への PR にする

## 選ばなかった案

- **Claude の Slack コネクタで送る**: 事業主ごとに宛先を持てず、工場の外（Claude のアカウント）に宛先が散る。送った記録も工場の events に残らない
- **Webhook だけにする**: 通知の種類 × チャンネルごとに URL を発行して `.env` に足すことになり、面倒（オーナー 2026-11-05）。Webhook も選べるように残す
- **環境変数を宛先ごとに増やす**（`SLACK_WEBHOOK_URL_OWNER`・`INBOX_NOTIFY_OWNER` …）: 種類 × 宛先の組み合わせが変数の名前に埋まり、検査もできない。秘密の URL だけを環境変数に残す
- **設定をマシンの中のファイル（`.env` の隣）に置く**: 工場を並べて動かすと設定がマシンごとに分かれ、ずれても気づけない。変えた記録も残らない
- **設定の正本を制御プレーン（DB）に置く**: 並べて動かす工場のあいだで正本が割れる。DB は写しにとどめる
- **種類ごとに出してよい組織を持たず、割り当てだけに任せる**: 設定の書き間違い 1 つで工場の内部の情報が事業主に出る。読み込みで拒む方が安い

## 結果

- 運営元は工場を建てるときに `.factory-config/notifications.yaml` を 1 つ書けば、通知の出し先を決められる。操業中も書き換えてマージすれば変わる。実際の製品のリポジトリと工場は private で運用する（rally は見本のため public）
- 今の工場は `notifications.yaml` を置かなければ今と同じに動く
- 事業主の宛先に何を出したかは `notify.sent` で後から確かめられる
- 種類を足すとき（運用の段の定期レポート、完了報告）は `definitions/notifications.yaml` に 1 行と、文面の関数を 1 つ足す。連携に Slack 以外（メール、Teams など）を足すときは `via` を 1 つ足す
