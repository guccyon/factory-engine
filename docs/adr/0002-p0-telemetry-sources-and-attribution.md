# ADR 0002: P0 の結果テレメトリの取得元と帰属ルール

- 状態: 承認（2026-10-25。閾値を含む判断は オーナー が Claude に委ね、運用の中で調整する）
- 関係: 開始定義 第 2 節「計測項目（PR 単位）」、第 3 節 Week 2、`definitions/events.yaml` `definitions/metrics.yaml`

## 文脈

学習の教師信号（revert・hotfix・churn・review_loops・lead_time・tokens）を PR に紐付ける結果テレメトリが、P0 のすべての前提になる。
現行の自動化はイベントをどこにも流していないので、取得元は GitHub・git log・Claude Code のセッションログの 3 つに限られる。
式は、建設者の手元にある既存リポジトリの直近 20 件で一度だけ通して動作と帰属の落とし穴（他リポジトリの番号の混入、統合 PR の段階 PR、文書の churn 誤検出）を確かめた。その数値は捨て、以後の計測対象は工場自身のリポジトリ（factory-engine / rally）だけにする（2026-10-25 オーナー「既存プロダクトは一切考慮に入れない」）。

## 決定

1. 計測対象は「base が既定ブランチでマージされた PR」。統合ブランチ向けの段階 PR は親の統合 PR に含める
2. Ticket への帰属は closing reference → 本文の #N（このリポジトリの Issue に解決できるものだけ）の順。Ticket がない PR は pr_created を起点にして欠損として数える
3. 観測窓（7 日・21 日）が経過していない PR は「未確定」とし、失敗にも成功にも数えない。窓の途中で条件を満たせば確定する
4. churn は新規作成ファイルのうちコードだけを対象にし、`.claude/memories/`・`docs/`・CHANGELOG・README は除く
5. tokens は Claude Code のセッションログの `pr-link` と `cost-state` から PR に配る。1 セッションが複数 PR にリンクしていれば均等割り。クラウドのセッションは手元にログがないので欠損になる
6. 実装は状態を持たない Python スクリプト（標準ライブラリ＋ gh ＋ git）。成果物ストアができるまでは JSON を出すだけ
7. 閾値の初期値は revert/hotfix の窓 7 日、churn の窓 21 日、churn の変更率 50%。精度の根拠はまだない。週次の FactoryHealthReport で実測と並べて見直し、変えたら metrics.yaml と本 ADR の後継に記録する
8. Ticket のない PR は工場では作らない。intake → Ticket（Issue）を必ず通し、lead_time の起点を欠損させない。builder の手作業も Issue を切ってから始める

## 選ばなかった案

- **GitHub Actions でイベントを収集してリポジトリに記録**: 状態を製品リポジトリに置くことになり、実行基盤の約束（状態はサービスに）と衝突する
- **レビュー往復を GitHub の Review API だけで数える**: 現行の運用では別エージェントのレビューが Issue コメントとして付くため 0 になる。コメント本文の印で数える
- **tokens を API の請求から取る**: Max 契約は定額で請求に出ない。セッションログの modelUsage が唯一の取得元
- **churn から文書を除かない**: `.claude/memories/` と docs/ が真っ先に churn 判定され、コードの手戻りという意図から外れた

## 結果

- 工場自身のリポジトリにはまだマージ済み PR がなく、第 1 号の健康レポートは「取得元と式が決まった」ことの記録になる。数値は PR が流れ始めてから
- tokens はクラウドのセッションのログが手元にないと欠損する。P0 Week 3 のヘッドレスランナーが session.cost_recorded を直接出す
- review_loops の印は粗い（「レビュー」を含むコメントを全部拾う）。P1 の InspectionReport で置き換える
- 既存リポジトリの数値を根拠にしないので、閾値は運用で決まるまで「仮」の印を付けて出す
