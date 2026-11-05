# ADR 0032: 報告・通知を種類ごとに宛先へ出し分ける（通知のルーティング）

- 状態: 提案（2026-11-05。決めどころ R1〜R7 はオーナーの確認待ち）
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

### 1. 宛先に名前を付けて定義する

宛先は「どの組織のもので、どう送るか」の組。

```yaml
destinations:
  provider:            # 名前は自由。通知の割り当てはこの名前で書く
    party: provider    # provider（運営元）/ business_owner（事業主）
    via: slack         # slack / github-issue / log（今の INBOX_NOTIFY の選択肢）
    webhook_env: SLACK_WEBHOOK_URL_PROVIDER   # 秘密は .env に置き、ここには変数の名前だけ
    quiet: "22-7"      # 送らない時間（REPORT_TZ の時）。明けた最初の周回でまとめて送る
    mentions: {builder: "<@U…>"}               # 誰の出番かごとのメンション
  owner_slack:
    party: business_owner
    via: slack
    webhook_env: SLACK_WEBHOOK_URL_OWNER
```

`party` が中身の境界（決定 3）の鍵になる。宛先の種類と URL の形は読み込むときに検査する（`slack` は `https://hooks.slack.com/` で始まること）。

### 2. 通知の種類は Engine の定義に置き、どの組織に出してよいかを種類が持つ

全工場に共通の種類の一覧を `definitions/notifications.yaml` に置く（ADR 0001 の決定 2）。工場ごとに変えるのは割り当てだけ。

| 種類 | 中身 | 出してよい組織 | 出どころ |
|---|---|---|---|
| `product.progress` | プロダクト開発の進捗（前日にマージ・出荷したもの、進めているもの、次に流すもの） | 運営元・事業主 | 工場（朝 1 回） |
| `product.release` | 本番への出荷の前後 | 運営元・事業主 | ship |
| `inbox.request` | 受信トレイの人の出番（選択・出荷の承認・依頼） | 運営元・事業主（決定 5） | `notify_inbox` |
| `factory.health` | 停止の始まり・終わり、段の残りが少ない、などの操業の知らせ | 運営元。事業主は操業を自走するときだけ | `stoppage` |
| `factory.daily` | 朝の報告の操業（ジョブの成否、費用、停止の時間） | 運営元。事業主は操業を自走するときだけ | 工場（朝 1 回） |
| `construction.progress` | 工場の建設の進捗（建設日報の要約とリンク） | 運営元だけ（上書きできない） | 工場（`report.daily` の後） |

### 3. 事業主の宛先に工場の内部の情報を混ぜない

- 種類ごとに「出してよい組織」を持ち、`business_owner` の宛先に出してはいけない種類を割り当てた設定は、読み込むときに拒む（通知を送らずにログと受信トレイの `error` に出す。ラインは止めない）
- 文面は宛先の `party` を引数にして作る。事業主の宛先には、運営元の操業コンソール（工場のマシンの中）へのリンク・費用・建設中の不具合を載せない（R5・R7）
- 運営元の宛先は複数の工場の通知が混ざる前提で、文面の頭に工場の名前（`FACTORY_NAME`）を付ける。事業主の宛先はその事業主の工場の通知だけを受ける（工場は 1 事業主・1 プロダクト）

### 4. 運用形態を 1 つ宣言すれば既定の割り当てが決まり、種類ごとに上書きできる

```yaml
operated_by: provider        # provider（運営元が操業する）/ business_owner（事業主が操業も自走する）
routes:                      # 書かなければ operated_by から決まる既定
  product.progress: [provider, owner_slack]
```

既定:

| 種類 | `operated_by: provider` | `operated_by: business_owner` |
|---|---|---|
| `product.progress`・`product.release` | 運営元と事業主の両方 | 運営元と事業主の両方 |
| `inbox.request` | 誰の出番かで分ける（決定 5） | 誰の出番かで分ける（決定 5） |
| `factory.health`・`factory.daily` | 運営元 | 事業主（運営元にも写しを送るかは R3） |
| `construction.progress` | 運営元 | 運営元 |

既定の「運営元」「事業主」は、その `party` を持つ宛先すべてを指す。

### 5. 受信トレイは、誰の出番か（who）で宛先を分ける

受信トレイの件は工場の健康状態だけではない。画面の案の選択（プロダクトオーナー）や見た目の選択（デザインのチーフ）は事業主の側の人の出番になる。`who` ごとに宛先の組織を決める。

| who | 既定の宛先 |
|---|---|
| `po`・`design_chief`・`requester` | 事業主 |
| `builder` | 運営元 |
| `owner`（停止の判断・出荷の承認など） | `operated_by` に従う（運営元が操業するなら運営元） |

代理への切り替え（`INBOX_PROXY`、ADR 0026 決定 7）は、代理の `who` の宛先にも送る。

### 6. 設定は工場ごとのファイルに置き、周回ごとに読み直す

- 置き場は工場の作業ツリーの外の `orchestrator/notify.yaml`（`.env` と同じくリポジトリに入れない。`notify.example.yaml` を置く）。パスは `NOTIFY_CONFIG` で変えられる
- dispatcher と supervisor は周回ごとにファイルを読み直す（変わったときだけ検査し、`notify.config_changed` に内容のハッシュと宛先の名前・割り当てを残す。秘密の値は残さない）。コンテナを作り直さずに変えられる
- 読めない・検査を通らないときは、前に通った設定のまま送り続け、受信トレイの `error` に出す

### 7. 送る口は 1 か所、失敗でラインを止めない

- `notify.send(kind, payload, who=None)` を 1 つ置き、割り当てを引いて宛先ごとに文面を作って送る。`stoppage`・`notify_inbox`・ship・supervisor の朝の処理はここを呼ぶ
- 宛先ごとに送る。1 つの宛先が失敗しても他は送る。記録は events の `notify.sent`（種類・宛先の名前・成否・dedupe_key）。受信トレイの `inbox.notified` はそのまま残す（`channels` に宛先の名前を入れる）
- `quiet` の時間は宛先ごとに見る。夜は事業主にだけ送らず、運営元には送る、ができる

### 8. 今の設定のまま動き続ける

`notify.yaml` がないときは、今の環境変数から宛先 `default`（`party: provider`）を 1 つ組み立て、すべての種類をそこに割り当てる。

- 受信トレイ: `INBOX_NOTIFY`・`SLACK_WEBHOOK_URL`・`INBOX_QUIET`・`INBOX_SLACK_MENTIONS` をそのまま使う
- 停止と知らせ: `NOTIFIER`・`STATUS_REPO` をそのまま使う
- 新しい種類（`product.progress`・`factory.daily`・`construction.progress`）は、`notify.yaml` で宛先を書くまで送らない（今は届いていないものが勝手に届き始めないように）

### 9. 工場の外で作っている報告は、工場が送る

建設日報と朝の報告はクラウドで描いているが、元のデータ（`report.daily`、ジョブ・停止・費用）は工場の制御プレーンにある。Webhook の秘密を工場の 1 か所に保つため、Slack への送信は工場が行う。

- `construction.progress`: supervisor が `report.daily` を書いた後、全体の進捗・前日の数・頼まれていることの要約と、建設日報のページへのリンクを送る
- `factory.daily`・`product.progress`: 朝の報告と同じ数を工場が要約して送る。朝の報告のプロダクト開発タブを工場から出す件（積み残し）と同じ組み立てを使う
- 建設者（工場の外の Claude）は宛先を持たない。建設者が文章を足したいときは、今どおり Issue やページに書き、工場の通知はそのリンクを載せる

## 決めどころ（オーナーの承認が要る）

| # | 問い | 推奨 |
|---|---|---|
| R1 | 設定の置き場と形 | 工場ごとのファイル `orchestrator/notify.yaml`（リポジトリの外、周回ごとに読み直す）。秘密は `.env`。流量（ADR 0029）のような events での宣言にはしない（秘密が要り、変える頻度も低い） |
| R2 | 通知の種類の一覧 | 決定 2 の 6 種類。種類の一覧は Engine の定義、割り当ては工場ごと |
| R3 | 事業主が操業も自走するとき、健康状態を運営元にも写すか | 写す（運営元は保守の契約がある限り工場の具合を知る必要がある）。契約で保守をしないときは `routes` で外す |
| R4 | 受信トレイの振り分け | 誰の出番か（who）で分ける。PO・デザインのチーフ・起票者は事業主、建設者は運営元、オーナーは運用形態に従う |
| R5 | 事業主に費用を見せるか | `product.progress` には載せない。費用は `factory.daily`（操業）に載せ、事業主が操業を自走するときだけ事業主に届く。パイロットで事業主が実費を見たいなら、週 1 回の費用の要約を別の種類として足す |
| R6 | 建設の進捗の出し方 | 工場が `report.daily` の後に要約を送る。建設者は宛先を持たず、Webhook の秘密は工場の 1 か所 |
| R7 | 事業主の宛先に付けるリンク | 操業コンソールは工場のマシンの中なので付けない。プロダクトのリポジトリ（PR・Issue）へのリンクだけ。コンソールを外に開いたら（ADR 0030 の窓口のログイン）付ける |

## 順番

1. 宛先と種類の定義、`notify.yaml` の読み込みと検査、`notify.send`、環境変数からの宛先 `default`（決定 8）。停止・知らせ・受信トレイをこの口に載せ替える（挙動は今と同じ）
2. 受信トレイの who による振り分け（決定 5）と、宛先ごとの `quiet`・メンション
3. `construction.progress`（`report.daily` の後の要約）
4. `factory.daily`・`product.progress`（朝の報告のプロダクト開発タブを工場から出す件と合わせる）と `product.release`

## 選ばなかった案

- **Claude の Slack コネクタで送る**: 事業主ごとに宛先を持てず、工場の外（Claude のアカウント）に宛先が散る。送った記録も工場の events に残らない
- **環境変数を宛先ごとに増やす**（`SLACK_WEBHOOK_URL_OWNER`・`INBOX_NOTIFY_OWNER` …）: 種類 × 宛先の組み合わせが変数の名前に埋まり、検査もできない。秘密の URL だけを環境変数に残す
- **割り当てを製品のリポジトリ（`.factory-config/`）に置く**: 運用形態は契約で決まる運営元の設定で、製品の変更ではない。製品のリポジトリは public のこともあり、宛先の名前や組織の分け方を載せたくない
- **種類ごとに出してよい組織を持たず、割り当てだけに任せる**: 設定の書き間違い 1 つで工場の内部の情報が事業主に出る。読み込みで拒む方が安い

## 結果

- 運営元は工場を建てるときに `notify.yaml` を 1 つ書けば、事業主ごとに通知の出し先を決められる。操業中も書き換えるだけで変わる
- 今の工場は `notify.yaml` を置かなければ今と同じに動く
- 事業主の宛先に何を出したかは `notify.sent` で後から確かめられる
- 種類を足すときは `definitions/notifications.yaml` に 1 行と、文面の関数を 1 つ足す。宛先に Slack 以外（メール、Teams など）を足すときは `via` を 1 つ足す
