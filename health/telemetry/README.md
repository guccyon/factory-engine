# health/telemetry

PR 単位の結果テレメトリ。`definitions/events.yaml`（イベント辞書）と `definitions/metrics.yaml`（式と帰属ルール）を実装する。

```bash
python3 health/telemetry/pr_metrics.py \
  --repo guccyon/rally \
  --git-dir ~/src/github.com/guccyon/rally/main \
  --limit 20 \
  --out telemetry.json
```

- 依存は python3（標準ライブラリ）、認証済みの `gh`、`git` だけ
- 状態を持たず、毎回取得元から再構成する。P0a で成果物ストアができたら出力の JSON を流し込む
- `--as-of` で基準時刻を固定すると、右打ち切り（7 日・21 日）の判定を再現できる
- 対象は工場自身のリポジトリ（factory-engine / rally）だけ。既存の別プロダクトは計測対象にしない
- P0 は手元の Mac で builder が走らせる。ヘッドレス化（Week 3）後はワーカーのジョブにする

出力の `summary.missing_rates` が欠損率。P0a の完了条件「計測の取得元・欠損率が分かる」はこの数値で確認する。
