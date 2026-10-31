# health/design

週 1 回の設計の診断（ADR 0022）のページ。所見は dispatcher が製品の固定 Issue（ラベル `factory:design-review`）の本文に書き、末尾にスナップショットを埋める。ここではそれを読んでページにする。LLM は使わない。

```bash
python3 health/design/render_html.py --repo guccyon/rally --out design.html    # Issue に埋めたスナップショットから描く
python3 health/design/render_html.py --snapshot snap.json --out design.html
```

- テンプレートは `page.html`。データを差し込むだけで、描画はページの中の JavaScript（外部のライブラリなし）
- 観点の名前は `definitions/design_review.yaml` から差し込む
- 週 1 回（土曜の朝）にクラウドの予約がこれを走らせ、同じ URL に公開し直す。建設日報からリンクする
