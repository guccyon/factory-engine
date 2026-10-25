# ADR 0002: P0 の結果テレメトリの取得元と帰属ルール

- 状態: 提案（2026-10-25、Tetsuro の了承待ち。閾値 N=7 日・21 日・変更率 50% の承認を含む）
- 関係: 開始定義 第 2 節「計測項目（PR 単位）」、第 3 節 Week 2、`definitions/events.yaml` `definitions/metrics.yaml`

## 文脈

学習の教師信号（revert・hotfix・churn・review_loops・lead_time・tokens）を PR に紐付ける結果テレメトリが、P0 のすべての前提になる。
現行の自動化はイベントをどこにも流していないので、取得元は GitHub・git log・Claude Code のセッションログの 3 つに限られる。
cornix-layout を実験台に直近 20 件で手動照合した（50_build-work/health-reports/2026-10-25.html）。

## 決定

1. 計測対象は「base が既定ブランチでマージされた PR」。統合ブランチ向けの段階 PR は親の統合 PR に含める
2. Ticket への帰属は closing reference → 本文の #N（このリポジトリの Issue に解決できるものだけ）の順。Ticket がない PR は pr_created を起点にして欠損として数える
3. 観測窓（7 日・21 日）が経過していない PR は「未確定」とし、失敗にも成功にも数えない。窓の途中で条件を満たせば確定する
4. churn は新規作成ファイルのうちコードだけを対象にし、`.claude/memories/`・`docs/`・CHANGELOG・README は除く
5. tokens は Claude Code のセッションログの `pr-link` と `cost-state` から PR に配る。1 セッションが複数 PR にリンクしていれば均等割り。クラウドのセッションは手元にログがないので欠損になる
6. 実装は状態を持たない Python スクリプト（標準ライブラリ＋ gh ＋ git）。成果物ストアができるまでは JSON を出すだけ
7. 閾値の初期値は revert/hotfix の窓 7 日、churn の窓 21 日、churn の変更率 50%

## 選ばなかった案

- **GitHub Actions でイベントを収集してリポジトリに記録**: 状態を製品リポジトリに置くことになり、実行基盤の約束（状態はサービスに）と衝突する
- **レビュー往復を GitHub の Review API だけで数える**: 現行の運用では別エージェントのレビューが Issue コメントとして付くため 0 になる。コメント本文の印で数える
- **tokens を API の請求から取る**: Max 契約は定額で請求に出ない。セッションログの modelUsage が唯一の取得元
- **churn から文書を除かない**: `.claude/memories/` と docs/ が真っ先に churn 判定され、コードの手戻りという意図から外れた

## 結果

- 20 件の照合での欠損率: Ticket 帰属 60%、tokens 70%、incident / gate_results 100%。revert/hotfix と churn は全件が観測窓の途中（マージから 5 日以内）で未確定
- Ticket のない PR が 6 割ある。チャットから直接始めた作業が多く、lead_time の起点がない。工場では intake → Ticket を通す経路を作る理由になる
- tokens の欠損はクラウドのセッション由来。P0 Week 3 のヘッドレスランナーが session.cost_recorded を直接出すようにする
- review_loops の印は粗い（「レビュー」を含むコメントを全部拾う）。P1 の InspectionReport で置き換える
