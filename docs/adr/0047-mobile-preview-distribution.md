# ADR 0047: モバイルアプリの審査前の配布（preview_distribution）の流れと境界

- 状態: 承認（2026-11-11 オーナー、#341）
- 関係: ADR 0012（出荷）、ADR 0028（QA と実行環境。決定 3 の Mac ランナー、決定 6 の配布は出荷側のアダプタ）、ADR 0033（ランナーの窓口とペアリング）、ADR 0043（P5b の O8）、ADR 0045（既存の製品のオンボーディング）
- 決めたこと（2026-11-09 オーナー）: 今回は Android だけを対象に Firebase App Distribution で配布する方式を確立し、その配布を通して配布の流れとインターフェースを決める。TestFlight は Gen2 で、パイロットの既存の iPhone アプリに出荷のレーンだけ使ってもらう形で作る

## 文脈

ADR 0043 の O8 は「日中の並走で TestFlight に 1 本配る」だった。オーナーは Apple Developer Program に入っておらず、iOS には会費なしで審査前のアプリを配る手段がない（Firebase App Distribution・DeployGate などのサービスも、iOS では有料の会員でしか作れない Ad Hoc 署名のアプリを配る）。一方で Android は会費なしで配れ、オーナーの確認用の端末も Android（Pixel）である。

Gen2 のパイロットの企業は iPhone アプリを出しており、工場のスコープはそのアプリの一部の機能を Web 版として作ることだが、既存のアプリを工場に取り込むことも当然検討に入る。だから配布は「Android を 1 回配って終わり」ではなく、配布先（チャネル）を足していける形で決めておきたい。

## 決定

### 1. 誰が何を持つか

| もの | 持ち主 | 置き場 |
|---|---|---|
| 何を配るか（アプリ・コミット・変更点） | 工場 | 依頼（`factoryctl distribute`、将来は便） |
| 配ってよいかの最後の判断 | Mac の前の人 | Mac の画面の確かめの窓（「配布する」） |
| 署名の鍵 | 製品の持ち主 | Mac（`~/.config/factory/android-upload.jks`、パスワードはキーチェーン） |
| 配布先へのログイン | 製品の持ち主 | Mac（`firebase login`） |
| 配布先の宛先（アプリの ID・テスターのグループ） | 製品の持ち主 | 工場（操業コンソールの出荷の「配布先」。2026-11-12 追記）。なければ Mac のランナーの設定（`MAC_RUNNER_FIREBASE_APP_ID`・`MAC_RUNNER_FIREBASE_GROUPS`）。宛先は秘密ではなく、上げるのは Mac の持ち主の `firebase login` なので、持ち主が触れないアプリには配れない。確かめの窓に宛先を出す |
| 依頼と結果の記録 | 工場 | events（`ship.preview_*`） |

鍵・パスワード・ログインは工場（Docker・DB・ワーカー）に入らない。工場が知るのは「配ったか、どのコミットを、どの番号で、APK の SHA-256 は何か、誰に」だけ。

### 2. 流れ

```
工場（dispatcher）                       窓口（runner-gate）            Mac ランナー                          人（Mac の前）
factoryctl distribute ──events: ship.preview_requested
   └─ preview_distribute ──────────────▶ /internal/call ──────▶ マージ済みか確かめる
                                                                  確かめの窓を出す ──────────────────▶ 「配布する」を押す
                                                                  （5 分押されなければ何もせずにやめる）◀──
                                                                  作業場で release ビルド
                                                                  キーチェーンのパスワードで署名
                                                                  持ち主のログインで配布先へ上げる
   └─ build_status を待つ ◀──────────────────────────────── distributed / failed（段つき）
events: ship.preview_distributed / ship.preview_failed                                            テスターの端末に招待が届く
```

- 人が押すのは 1 回だけ。押したあとは最後まで流れる
- 人の合図より前に、ビルド・署名・配布はしない（作業場も作らない）
- ビルドするのは既定ブランチに入ったコミットだけ（ランナーの既存の約束）
- 工場のワーカー（LLM のジョブ）はこの道具を呼べない。QA のジョブの中継（`runner_relay`）は `definitions/qa.yaml` の `runner_tools` だけを通し、そこに入れない

### 3. インターフェース

**ランナーの道具** `preview_distribute`（Mac ランナー 0.4.0）

| 引数 | |
|---|---|
| `app` | `<owner/repo>:<パス>`。`MAC_RUNNER_APPS` で許したものだけ |
| `channel` | 配布先。今は `firebase_android` だけ |
| `ref` | コミットかブランチ（既定 `main`）。マージ済みに限る |
| `release_notes` | テスターに見せる変更点（1000 文字まで。記録には長さだけ） |
| `dart_defines` | ビルドに渡す値（接続先など） |

返り値は `build_id`。`build_status` で状態を待つ: `waiting_for_human` → `building` → `distributed` か `failed`。段（`stage`）は `checkout` / `attend` / `build` / `sign` / `upload` / `done`。失敗は段で分かれ、`attend` は「人がいなかった・やめた」で、壊れたのではない。

**工場の記録**（correlation_id は `dist:<依頼の events の id>`）

| events | payload |
|---|---|
| `ship.preview_requested` | app, channel, ref, by, via, notes_chars |
| `ship.preview_distributed` | request_id, app, channel, sha, build_number, sha256, testers, runner, build_id, seconds |
| `ship.preview_failed` | request_id, app, channel, stage（`request` / `attend` / `build` / `sign` / `upload` / `timeout`）, error, runner, build_id, seconds |

**入口**（2026-11-12 追記。オーナー: ターミナルの操作はできるだけ避け、コンソールで押す・Mac で押すの 2 回にする）

- 操業コンソールの出荷のページの「配布する」（`POST /api/distribute`）。依頼を書くだけで、dispatcher の `distribute_loop` が拾って動かす。拾うときに `ship.preview_started`（dedupe_key `dist-start:<id>`）を書いて 1 回だけ動かす。同じアプリの配布が終わるまで次の依頼は断る
- 配布先（宛先）は同じページで入れる（`POST /api/distribute/target`、events の `ship.preview_target_set`）。依頼のときの宛先を依頼の payload に写し、ランナーに `firebase_app_id`・`groups` で渡す
- `factoryctl distribute [--channel ..] [--app ..] [--ref ..] [--notes ..] [--define K=V].. [--timeout 秒]` と `factoryctl distribute list`（依頼を書いてその場で動かす）
- Mac と Firebase の準備の手順は操業コンソールのヘルプの「モバイルの配布」（/help/mobile）

### 4. チャネルの足し方

チャネルは「ビルドの形・署名・上げ先」の 3 つで決まる。チャネルを足すときは、ランナーの `CHANNELS` と `_distribute` の分岐、SCOPE.md・PROTOCOL.md、工場の `distribution.CHANNELS` を同じ PR で変え、オーナーの承認を通す。

| チャネル | ビルド | 署名 | 上げ先 | いつ |
|---|---|---|---|---|
| `firebase_android` | `flutter build apk --release` | Mac の keystore（パスワードはキーチェーン）で `apksigner` | Firebase App Distribution（`firebase appdistribution:distribute`） | 今（Gen1） |
| `testflight_ios` | `flutter build ios --config-only` → `xcodebuild archive` | 持ち主の Xcode のサインイン（`-allowProvisioningUpdates`） | App Store Connect（`xcodebuild -exportArchive`、destination=upload） | Gen2（パイロットの既存アプリ） |

`testflight_ios` は今回いちど作りかけ、方針を変えたので外した（この ADR の PR の最初のコミットに残る）。Gen2 で既存のアプリに出荷のレーンだけ使ってもらうときは、製品のリポジトリに `ios/` があり署名の設定も先方の持ち物なので、「作業場で `ios/` を作る」部分は要らず、先方の設定に Team ID を渡すだけになる見込み。

### 5. 今の形の限り（Gen1 の範囲）

- 配布先の宛先はランナー 1 台に 1 つ（Firebase のアプリ 1 つ）。製品が増えたら `MAC_RUNNER_APPS` と同じく、アプリごとに宛先を持たせる
- 製品のリポジトリに `android/` がない（rally の生徒アプリ）ので、作業場の中だけで `flutter create --platforms=android` する。このときの applicationId は `--org` と pubspec の名前から決まる（rally は `jp.rally.rally_student_app`）。Firebase に登録するアプリはこれに合わせる
- 便（ADR 0043 の O6）からは呼ばない。便で staging に出たあとに配る流れは、Gen1.5 のオンボーディングで「Release の束」が決まってから
- 版の番号（versionCode）は分の通し番号。製品が自分の番号の決め方を持つなら、それに従う形に変える

## 選ばなかった案

- **Developer Program に入って TestFlight をいま作る**: 会費がかかり、Gen1.5 の製品には iOS の開発が入らない。TestFlight を本物で確かめられるのは Gen2 のパイロットのアプリなので、そこで作るほうが手戻りが少ない
- **Xcode の無料のアカウントで iPad に直接入れる**: 配布ではなく（ケーブルでつないだ自分の端末だけ、7 日で切れる）、O8 の回路の確認にならない
- **CI（GitHub Actions）で署名して配る**: 鍵を GitHub の秘密に預けることになり、「鍵は工場に預けない」（ADR 0028 の決定 3）に反する。人が Mac の前にいる合図もとれない
- **署名の鍵のパスワードをランナーの設定（plist）に書く**: plist はふつうのファイルで、キーチェーンより守りが弱い
- **ジョブ（LLM）に配布の道具を渡す**: 配布は外の人に届く操作で、取り消せない。依頼は工場の決まったコード（`distribution.py`）からだけにする

## 承認が要る点

- Mac ランナーの範囲が広がる（SCOPE.md: キーチェーンの読み取り、Firebase への送信）
- 秘密情報（署名の鍵とパスワード）の置き場を決める

## 結果

- O8 の済の条件を「TestFlight に 1 本」から「Firebase App Distribution で Android に 1 本（Mac での承認 1 回）」に読み替える
- 配布の流れ・記録・境界は、チャネルを足すだけで TestFlight にも使える
