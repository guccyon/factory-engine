# Mac ランナー（ADR 0028 の決定 3、P5a の mac_runner）

工場の外の機械（macOS のホスト）に常駐し、iOS シミュレーターを決まった道具だけで工場に貸す。工場のコンテナ（dispatcher、QA のワーカー）からは `host.docker.internal:8793` に MCP（Streamable HTTP。応答は JSON だけで SSE は使わない）で話す。対話は要らないので Remote Control ではない。

```
工場（Docker）                                 ホスト（macOS）
 dispatcher ─ factoryctl runner check ──┐
 QA のワーカー ─ claude --mcp-config ───┼─ http://host.docker.internal:8793/mcp ─▶ runner.py（launchd が常駐）
                                        │       Bearer トークン                        ├ xcrun simctl（起動・入れる・撮る・URL・push・ログ）
                                        │                                              ├ idb（画面の木・押す・なぞる・打つ）
                                        │                                              └ flutter build ios --simulator（マージ済みのコミットだけ）
```

## 入れる（ホストで 1 回）

```sh
# 前提: Xcode（シミュレーターの OS を 1 つ以上）、Flutter、git。画面を押す道具には idb を足す:
#   brew install facebook/fb/idb-companion && pip3 install --user fb-idb
#   （idb は ~/Library/Python/<版>/bin に入る。install がそこを launchd の PATH に足す）
cd orchestrator
scripts/macrunner.sh install      # トークンを作り、launchd に常駐させる（install のときの PATH を渡す）
scripts/macrunner.sh status       # 常駐・/health・足りない道具
scripts/macrunner.sh token        # この値を .env の MAC_RUNNER_TOKEN に入れる
docker compose up -d dispatcher   # 環境変数を読み直す
docker compose exec dispatcher factoryctl runner check
```

`factoryctl runner check` は health → MCP の initialize → 道具の一覧 → 端末の一覧の順に確かめ、止まったところを 1 行で出す。

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

## 守り

工場の外の機械に、ジョブ（LLM が組み立てる引数）からの操作を通す初めての経路なので、次の線を引く。

- **道具は決まった一覧だけ**。任意のシェル・任意のコマンドの道具はない。サブプロセスは argv の配列で呼び、シェルを通さない
- **引数は形を確かめる**（UDID・bundle id・ref・URL・座標の範囲、知らない引数は断る）。`file:` と `javascript:` の URL は開かない
- **ビルドするのはマージ済みのコミットだけ**。ビルドは製品のコード（依存のパッケージのフックや Xcode のスクリプトを含む）をホストで動かすので、検査と judge を通って既定ブランチに入ったものに限る。PR のブランチを夜間に回したくなったら `MAC_RUNNER_ALLOW_UNMERGED=1` で外せるが、外すのは人の判断
- **ビルドしてよいアプリは許したものだけ**（`MAC_RUNNER_APPS=<owner/repo>:<パス>:<bundle id>` のカンマ区切り）
- **認証はトークン**（`~/.config/factory/macrunner.token`、0600）。待ち受けは `127.0.0.1` だけ（Docker Desktop の `host.docker.internal` はホストの loopback に届く）。`/health` だけはトークンなしで生死だけ返す
- **署名の鍵は扱わない**。debug・署名なし（`--no-codesign`）だけ。署名と TestFlight は日中に人の Mac のキーチェーンで通す（ADR 0028 の決定 3）
- **呼ばれた道具はすべて記録**（`~/Library/Logs/factory-macrunner/calls.jsonl`。打った文字と通知の中身は長さだけ）

## 設定（環境変数。`scripts/macrunner.sh install` が plist に書く）

| 名前 | 既定 | |
|---|---|---|
| `MAC_RUNNER_PORT` | 8793 | |
| `MAC_RUNNER_BIND` | 127.0.0.1 | |
| `MAC_RUNNER_APPS` | `guccyon/rally:apps/student-app:jp.rally.rallyStudentApp` | bundle id は ios/ がないときに作る値。ビルドしたものの Info.plist の値を優先する |
| `MAC_RUNNER_ALLOW_UNMERGED` | 空 | 1 でマージ前のコミットもビルドする |
| `MAC_RUNNER_HOME` | `~/Library/Application Support/factory-macrunner` | リポジトリの写しとビルドの作業場（新しい 3 本だけ残す） |
| `MAC_RUNNER_BUILD_TIMEOUT_SEC` | 1800 | |

## テスト

```sh
python3 -m pytest -q orchestrator/macrunner/tests   # xcrun・idb・flutter は呼ばずに、呼ぶ argv と受け答えを確かめる（Linux でも動く）
```

## この先（ADR 0028 の P5a の順番）

- `one_orchestrator`: QA のワーカーの claude に `macrunner_client.mcp_config()` を `--mcp-config` で渡し、サーバ側（環境・API・DB）と Mac 側を 1 つのセッションで動かす
- `nightly_explore`: 夜間（unattended）に同じ道具で探索する。keep-awake（ADR 0011 の 7）はランナーのジョブの間も起こしておく
