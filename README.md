# factory-engine

自律型ソフトウェア事業部（Factory of Factories）の Engine。
Blueprint を 1 枚渡すと、そのプロダクト専用の仮想事業部（Product Monorepo ＋ 工場の設定）が立ち上がり、以後は人間の承認と審美眼の供給だけで企画・開発・出荷・運用・改善が続く状態を目指す。

- 1 号機の Product Monorepo: [guccyon/rally](https://github.com/guccyon/rally)
- 現在の Phase: **P0（計測基盤）Week 1**。宣言的定義の初版を起案中で、実行系はまだない

## 構成

| ディレクトリ | 役割 | 状態 |
|---|---|---|
| `definitions/` | ステージ・成果物の型・ゲート・工場の健康・共通評価仕様の宣言（`stages.yaml` `artifacts.yaml` `gates.yaml` `health.yaml` `evalspec.yaml`） | P0 起案 |
| `core-gates/` | 汎用ルーブリック（`rubric.yaml`）・AST ルール・互換性チェッカー | P0 起案（ルーブリック v0） |
| `orchestrator/` | 制御プレーン（ジョブキュー・ロック・予算）。P0 は Postgres 単体 | P0 Week 3 |
| `core-skills/` | 役割別スキルのテンプレート（現行の Claude スキルが母体） | 未着手 |
| `eval-corpus/` | 汎用の Golden Set・ルール検証の正例 / 負例 | 未着手 |
| `meta-controller/` | Blueprint → base のコンパイルと Reconcile | P6 |
| `health/` | 工場の健康指標の計算（govern。learn から独立） | P0 Week 2 |
| `docs/adr/` | 設計判断の記録 | 随時 |

## 用語

用語・ステージ名・成果物の型名は、プロジェクトの開始定義（第 1 節）に従う。
ステージは `observe` `intake` `clarify` `triage` `define` `design` `implement` `verify` `ship` `operate` `learn` の 11、ラインは `deliver` `answer` `respond` `report`（横断: `govern` `learn`）。

## 実行基盤の約束

- 状態（成果物ストア・キュー・ロック・テレメトリ）はローカル FS に置かず、別サービスにする
- ワーカーは 1 タスク 1 コンテナで使い捨て（浅いクローン、終了時に何も残さない）
- 制御プレーンとワーカーを分け、ステップは冪等にする
- モデルは tier 名（`tier-small` / `tier-large` / `tier-judge`）で抽象化する

## 利用について

Engine は顧客に納品する仮想開発事業部の一部として版付きで渡すが、知財は建設者（guccyon）の会社に帰属する。利用許諾（企業内利用限定・再販禁止・ノウハウの流用禁止）の文言は Gen2（2 号機）の前に確定する。開発中のため public で公開しているが、ライセンスはまだ定めていない。
Gen1 はプロトタイプで、次世代では Engine も含めてリライトする前提。Engine とアプリコードの責務を分けておくのは、そのリライト範囲を明確にするため（docs/adr/0001）。
