# 事故記録の一覧

core-gates/incident_check.py --index で作る。手で編集しない。

| id | 状態 | 資源 | 仕組み | 帯 | 題 |
|---|---|---|---|---|---|
| FIR-2026-0001 | draft | llm_tokens | contention | high | 全 PR が共有の追記ファイルを書き換え、衝突の解消が止まらずに 1 日の予算の枠を大きく超えた |
| FIR-2026-0002 | draft | llm_tokens | self_feedback | high | 知見の担保の PR が同じ担保の起票を呼び戻し、閉じた後も同じ題の起票と PR が繰り返された |
