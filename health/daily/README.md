# health/daily

建設状況の日次レポート（ADR 0008）。`definitions/roadmap.yaml` の項目を取得元から測り、ロードマップの進捗・頼みたいこと・直近 24 時間の動きを出す。LLM は使わない。

```bash
python3 health/daily/status_report.py --dry-run     # 書かずに Markdown を標準出力へ
python3 health/daily/status_report.py --publish     # 固定 Issue（factory:construction）を書き換え、コメントを足し、report.daily を記録する
```

- 取得元: GitHub（リポジトリ単位のエンドポイントだけ）、制御プレーンの Postgres（`DATABASE_URL`）、建設作業フォルダ（`BUILD_WORK_DIR`）
- 届かない取得元の項目は「不明」と出し、進捗は 0 で数える
- ふだんは supervisor が 1 日 1 回走らせる（orchestrator/README.md）

## claude.ai のページ

```bash
python3 health/daily/render_html.py --out construction.html                    # Issue に埋めたスナップショットから描く
python3 health/daily/render_html.py --snapshot snap.json --out construction.html
```

- テンプレートは `page.html`。データを差し込むだけで、描画はページの中の JavaScript（外部のライブラリなし）
- 毎朝 6:00 JST にクラウドの予約がこれを走らせ、同じ URL に公開し直す（ADR 0008）
- 見た目や指標を変えるときは `page.html` と `status_report.py` の `series()` を PR で直す
