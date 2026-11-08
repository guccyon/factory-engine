# Mac ランナー（ADR 0028 の決定 3、P5a の mac_runner）

工場の外の機械（macOS）に常駐し、iOS シミュレーターを決まった道具だけで工場に貸す。ランナーは待ち受けず、工場のランナーの窓口（`dispatcher/runner_gate.py`、compose の `runner-gate`）へ TLS で仕事を取りに行く（ADR 0033）。工場とはペアリングで繋ぎ、1 つの工場に何台でも並べられる。対話は要らないので Remote Control ではない。

```
工場（Docker）                                                   Mac（何台でも）
 dispatcher ─ factoryctl runner check ─┐                          runner.py（launchd が常駐）
 （one_orchestrator の中継）──────────┼▶ runner-gate ◀─ TLS ──── 取りに行く（/poll）・結果を返す（/result）
                     中向けの口 :8795 ─┘   :8794   ペアリングの鍵で署名     ├ xcrun simctl（起動・入れる・撮る・URL・push・ログ）
 操業コンソール ─ ペアリングの「許可」                                      ├ idb（画面の木・押す・なぞる・打つ）
                                                                            └ flutter build ios --simulator（マージ済みのコミットだけ）
```

## 入れる（ホストで 1 回）

```sh
# 前提: Xcode（シミュレーターの OS を 1 つ以上）、Flutter、git。画面を押す道具には idb を足す:
#   brew install facebook/fb/idb-companion && pip3 install --user fb-idb
#   （idb は ~/Library/Python/<版>/bin に入る。install がそこを launchd の PATH に足す）
cd orchestrator
scripts/macrunner.sh install      # ペアリングして、launchd に常駐させる（install のときの PATH を渡す）
scripts/macrunner.sh status       # 常駐・ペアリング・足りない道具
docker compose exec dispatcher factoryctl runner check   # 工場の側で
```

別の Mac の工場に繋ぐときは `MAC_RUNNER_GATE=https://<その Mac の名前か IP>:8794 scripts/macrunner.sh install`。工場の側は `.env` の `RUNNER_GATE_BIND=0.0.0.0` で窓口を LAN に開ける（既定は 127.0.0.1 で同じ Mac からだけ）。

### ペアリング（ADR 0033）

1. `install` がターミナルに 6 桁の番号を出す（例「482 913」）
2. 工場の操業コンソールの「登録済みのランナー」に、同じ番号の接続の依頼が出る。番号を見比べて「許可」を押す
3. ターミナルで `y` を押す。両方そろうと、そのランナー専用の鍵が両側に保存されて繋がる

依頼は 10 分で切れる。番号は両側の乱数と工場の証明書の指紋から作るので、間に入った者が番号を合わせられない。以後ランナーは、ペアリングのときの証明書の相手とだけ話す。工場の証明書を作り直した（`runnergate` ボリュームを消した）ときは、`scripts/macrunner.sh pair` でペアリングし直す。

`factoryctl runner check` は 窓口 → ペアリング済みの台数と窓口に来ているか → 道具 1 つ（`sim_devices`）の順に確かめ、止まったところを 1 行で出す。

## 道具

| 道具 | すること |
|---|---|
| `sim_devices` / `sim_boot` / `sim_shutdown` / `sim_erase` | iOS シミュレーターの一覧・起動（画面は出さない）・停止・初期化 |
| `app_build` → `build_status` | 許したアプリ（`MAC_RUNNER_APPS`）を、既定ブランチに入ったコミットからシミュレーター向けの debug（署名なし）でビルドする。数分かかるので build_id を返し、状態を聞いて待つ |
| `app_install` / `app_launch` / `app_terminate` | ビルドしたアプリを入れる・起動する・止める |
| `screenshot` | 画面を PNG で返す（MCP の image） |
| `open_url` / `push` / `logs` | ディープリンク・プッシュ通知・アプリのプロセスのログ |
| `ui_describe` / `ui_tap` / `ui_swipe` / `ui_text` / `ui_key` | 画面のアクセシビリティの木と操作（idb） |

製品に `ios/` がまだなければ（rally の生徒アプリは今ない）、ビルドの作業場の中だけで `flutter create --platforms=ios` を走らせる。製品のリポジトリには書かない。署名の設定と配布は出荷側のアダプタ（P5b の `preview_distribution`）。

## できることの範囲

顧客の機械にも置くものなので、範囲は [SCOPE.md](SCOPE.md) に宣言する。工場とランナーの間を通るメッセージの全部と、利用者をまたぐ穴の確かめは [PROTOCOL.md](PROTOCOL.md)。広げるときは SCOPE.md と道具を同じ PR で変え、オーナーの承認を通す。

## 登録済みのランナーと外し方

ランナーは窓口へ取りに行くたびに自己紹介し、コンソールの「登録済みのランナー」に機械・状態・版・置き場・できることが出る。「外す」を押すと、次に取りに来たときに窓口が鍵を捨て、ランナーも鍵を捨てて自分の LaunchAgent を外して止まる（10 分来なければ窓口の側だけ先に捨てる）。手で外すときはその機械で `scripts/macrunner.sh uninstall`（工場に知らせて両側の鍵を捨てる）。ビルドの作業場・ログは残す（消すのは人）。

## 守り

工場の外の機械に、ジョブ（LLM が組み立てる引数）からの操作を通す初めての経路なので、次の線を引く。

- **道具は決まった一覧だけ**。任意のシェル・任意のコマンドの道具はない。サブプロセスは argv の配列で呼び、シェルを通さない
- **引数は形を確かめる**（UDID・bundle id・ref・URL・座標の範囲、知らない引数は断る）。`file:` と `javascript:` の URL は開かない
- **ビルドするのはマージ済みのコミットだけ**。ビルドは製品のコード（依存のパッケージのフックや Xcode のスクリプトを含む）をホストで動かすので、検査と judge を通って既定ブランチに入ったものに限る。外す設定は置かない
- **ビルドと操作ができるアプリは許したものだけ**（`MAC_RUNNER_APPS=<owner/repo>:<パス>:<bundle id>` のカンマ区切り）。起動・停止・プッシュ通知もそのアプリだけで、端末の標準のアプリやほかのアプリには触らない
- **開ける URL は許したものだけ**（`MAC_RUNNER_URL_HOSTS` の http(s) と `MAC_RUNNER_URL_SCHEMES` のスキーム）
- **待ち受けない**。ランナーから工場の窓口へ TLS で取りに行く。相手はペアリングのときに覚えた証明書の指紋だけ、呼び出しはランナーごとの鍵（HMAC）で署名する（`~/.config/factory/macrunner.json`、0600）。窓口から来るのは道具の呼び出しと外す依頼だけ
- **署名の鍵は扱わない**。debug・署名なし（`--no-codesign`）だけ。署名と TestFlight は日中に人の Mac のキーチェーンで通す（ADR 0028 の決定 3）
- **呼ばれた道具はすべて記録**（`~/Library/Logs/factory-macrunner/calls.jsonl`。打った文字と通知の中身は長さだけ）

## 設定（環境変数。`scripts/macrunner.sh install` が plist に書く）

| 名前 | 既定 | |
|---|---|---|
| `MAC_RUNNER_GATE` | https://127.0.0.1:8794 | ペアリングする工場の窓口（ペアリングの後は鍵と一緒に覚えた先へ行く） |
| `MAC_RUNNER_APPS` | `guccyon/rally:apps/student-app:jp.rally.rallyStudentApp` | bundle id は ios/ がないときに作る値。ビルドしたものの Info.plist の値を優先する |
| `MAC_RUNNER_URL_HOSTS` | 127.0.0.1,localhost | open_url で開ける http(s) のホスト（触れる環境） |
| `MAC_RUNNER_URL_SCHEMES` | 空 | open_url で開けるアプリのスキーム（ディープリンク） |
| `MAC_RUNNER_HOME` | `~/Library/Application Support/factory-macrunner` | リポジトリの写しとビルドの作業場（新しい 3 本だけ残す） |
| `MAC_RUNNER_BUILD_TIMEOUT_SEC` | 1800 | |

## テスト

```sh
python3 -m pytest -q orchestrator/macrunner/tests   # xcrun・idb・flutter は呼ばずに、呼ぶ argv と受け答えを確かめる（Linux でも動く）
TEST_DATABASE_URL=… python3 -m pytest -q orchestrator/dispatcher/tests/test_runner_gate.py   # 窓口とペアリング・取りに行く形を本物の TLS で
```

## この先（ADR 0028 の P5a の順番）

- `one_orchestrator`: dispatcher がジョブごとに MCP の中継を立て（窓口の中向けの口 `macrunner_client.call` に繋ぐ。鍵はワーカーに渡さない）、QA のワーカーの claude がサーバ側（環境・API・DB）と Mac 側を 1 つのセッションで動かす。通す道具はジョブごとに絞り、呼んだ道具は events に残す
- `nightly_explore`: 夜間（unattended）に同じ道具で探索する（2026-11-08、orchestrator/README の「夜間の探索」）。探索のジョブも QA のジョブなので中継はそのまま渡る。keep-awake（ADR 0011 の 7）は工場のコンテナが動いている間は Mac を起こしておくので、夜のジョブの間も眠らない
