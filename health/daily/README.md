# health/daily

建設状況の日次レポート（ADR 0008）。`definitions/roadmap.yaml` の項目を取得元から測り、ロードマップの進捗・頼みたいこと・直近 24 時間の動きを出す。LLM は使わない。

```bash
python3 -m pytest health/daily/tests                # GitHub を差し替えたテスト（DB 不要）
python3 health/daily/status_report.py --dry-run     # 書かずに Markdown を標準出力へ
python3 health/daily/status_report.py --publish     # 固定 Issue（factory:construction）を書き換え、コメントを足し、report.daily を記録する
```

- 取得元: GitHub（リポジトリ単位のエンドポイントだけ）、制御プレーンの Postgres（`DATABASE_URL`）、建設作業フォルダ（`BUILD_WORK_DIR`）
- 届かない取得元の項目は「不明」と出し、進捗は 0 で数える
- Gen1 の完了の見込み（`gen1_forecast`）: ロードマップ（P0a〜P6）を終える日を、これまでの平均のペースとその半分の 2 本で出す。フェーズは順に片付くとみなす粗い見込み
- 商品化の条件: `roadmap.yaml` の `commercial` を並べる。P6 を終えても Gen1 の完了にすぎず、商品化はその先。測り方（`measure`）が決まるまで日付の見込みは出さない
- 頼みたいことの「承認待ちの PR」は、ラベル `factory:needs-approval`（supervisor の判定、`REPORT_REPOS`）と、マージに必ず人の承認が要るリポジトリ（`HUMAN_MERGE_REPOS`、既定 `guccyon/factory-engine`）の下書きでない開いた PR。待っている日数の長い順
- 使用量: スナップショットの `usage` に、操業コンソールの `/usage` と同じ集計（orchestrator/dispatcher/usage.py）を入れ、日報に契約の週の消費とアカウントを足すかの判断の材料を出す。朝の報告の「工場の操業」はこれを読む
- 夜間の探索: スナップショットの `explore` に、前日の正午（JST）からの `qa.explore`（Defect と印）と、回らなかった理由（`supervisor.nightly_explore_skipped`）を入れ、日報とページの「夜間の探索（昨夜）」に出す（ADR 0028 の nightly_explore）
- 工程ごとの ROI（集計は `orchestrator/dispatcher/stage_roi.py`。工場の操業レポートの節「工程と ROI」と同じもの）: スナップショットの `stage_roi` に、直近 14 日（`ROI_WINDOW_DAYS`）の工程×モデルごとの費用・作業と待ちの時間・一発で通った割合・混入させた Defect（`defect.recorded` の `introduced_job`）と、ボトルネック（時間・費用・一発で通りにくい・Defect）を入れる。モデルの試し（`definitions/agents.yaml` の `experiments`、`model.routed`）は、条件に当たったチケットを試す側と既定の側で比べる（通った 1 件あたりの費用・リードタイム・一発で通ったか・Defect）。既定のモデルを替える判断の根拠にする
- ふだんは supervisor が 1 日 1 回走らせる（orchestrator/README.md）

## claude.ai のページ

```bash
python3 health/daily/render_html.py --out construction.html                    # Issue に埋めたスナップショットから描く
python3 health/daily/render_html.py --snapshot snap.json --out construction.html
```

- テンプレートは `page.html`。データを差し込むだけで、描画はページの中の JavaScript（外部のライブラリなし）
- 毎朝 6:00 JST にクラウドの予約がこれを走らせ、同じ URL に公開し直す（ADR 0008）
- 見た目や指標を変えるときは `page.html` と `status_report.py` の `series()` を PR で直す
