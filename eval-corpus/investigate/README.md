# 工場の中での一次調査の評価セット（ADR 0048 の決定 8）

`cases.yaml` には、過去に工場の外（建設者の Claude がソースを読んで）調べて原因が分かった件を並べている。各件には、当時の問い、時間帯、直す前の Engine の版、正解の原因が入っている。一次調査の調査役が同じ原因に着くかを、当時の記録で再生して測る。

- 再生: 調査役には、直す前の版（`engine_ref`。`fixed_in` の親）の definitions/ と docs/ を読ませる。直した後の ADR や定義を読むと答えが分かってしまうため。DB とログは今のもので、時間帯（`window`）で絞る。events は消さないので、当時の記録が残っている
- 回し方: `docker exec -i factory-dispatcher-1 factoryctl investigation bench < eval-corpus/investigate/cases.yaml`（`--only id,...` で一部）。1 件ごとに調査役のジョブ（上限は 1 件 $3）を governance の枠に積む。1 日の上限の勘定には入れない（人が回すものなので）。結果は `factoryctl investigation bench-status`
- 採点は決定論で行う（`orchestrator/dispatcher/investigate_bench.py`）
  - `expected.outcome` と `expected.fix_target` が合っていること。`fix_target` は並びでも書け、そのどれかなら合格（原因が分からないまま、抜けを記録・通知する仕組みを工場に提案するのも正しい件など）
  - `expected.must_mention` の各組から 1 語以上が、報告・改善 Issue・仮説のどこかに出ていること
  - `expected.stop_reason` がある件は、止めた理由がそのどれかであること。特定できずに引き継ぐのが正しい件、たとえばソースを読まないと分からない件で使う
- 特定率（合った件 ÷ 回した件）が `definitions/investigate.yaml` の `eval.pass_rate`（0.5）に届くまでは、窓口への報告に「試行中」と添える
- 正解は、建設者がオーナーの代わりに付けた案。`confirmed: false` の件は、オーナーの確認がまだ済んでいない
- 製品の名前は rally（公開のデモ）だけを使う。事業主の製品の件は、その工場の `.factory-config/evals/` に置く
