# ADR 0001: Engine と Product Monorepo の分離、宣言的定義の置き場

- 状態: 提案（2026-10-25、Tetsuro の了承待ち）
- 関係: プロジェクト憲章 04 節（Engine / Cartridge 分離、base / overlay）、開始定義 第 1 節・第 3 節 Week 1

## 文脈

ノウハウ（Engine）は受託納品から守るために外に出し、個別工場はアプリコードを内包するモノレポにする（憲章 01 節「ファクトリー＝プロダクト、ただし頭脳は外」）。
P0 Week 1 で、ステージ・成果物の型・ゲート・工場の健康・評価仕様を宣言的ファイルとして置く場所を決める必要がある。

## 決定

1. リポジトリは 2 本に分ける。`guccyon/factory-engine`（Engine）と `guccyon/rally`（1 号機の Product Monorepo）。どちらも public
2. 全工場に共通の定義は Engine の `definitions/` に置く（`stages.yaml` `artifacts.yaml` `gates.yaml` `health.yaml` `evalspec.yaml`）。汎用ルーブリックは `core-gates/rubric.yaml`
3. 製品側は `.factory-config/` に `base/`（Engine 生成・編集禁止）、`overlay/`（学習。confidentiality 必須）、`evals/`（Golden Set）、`manifest.yaml`（autonomy・予算）を持つ
4. P0〜P5 の間、base は Engine の版を指すだけで手で置く。Blueprint → base のコンパイル（meta-controller）は 2 号機との差分から抽象を作る P6 まで実装しない
5. 用語・識別子は開始定義 第 1 節を正とし、YAML のキーとファイル名は英語、説明は日本語

## 選ばなかった案

- **1 リポジトリに Engine と製品を同居**: 立ち上げは速いが、受託納品で Engine が漏れる構造になり、憲章 09 節の構造条件（版付き成果物として配布）と衝突する
- **定義を製品側に置き、Engine は空で始める**: 2 号機の時点で共通部分を抽出し直すことになり、用語が工場ごとに分岐する
- **最初から JSON Schema で定義**: 項目の合意が先。P0a 中に YAML から JSON Schema を起こして型検証（Integrity）に使う

## 結果

- 製品側の manifest.yaml は Engine の定義（変更種別・操作）を参照する。Engine の定義を変えるときは rally の manifest との整合を同じ週に確認する
- 開発中は public。ライセンスは置かず、Gen2 の前に private 化と利用許諾を再判断する
