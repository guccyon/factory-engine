# health/quality

内部品質の定期診断（`definitions/quality.yaml`、ADR 0017）。コードの作りが長い目で悪くなっていないかを数値で残す。

- `snapshot.py measure`: 製品のチェックアウトを測って、指標・スコア・ホットスポットを JSON に出す。どのパスを測るかは製品の `.factory-config/quality.yaml`
- `snapshot.py floor`: PR の base と head の結果を比べて、下限割れを Markdown に出す（shadow。終了コードは常に 0）

測るのは製品の CI（`.github/workflows/quality.yml`）。main は 1 日 1 回測って成果物 `quality-snapshot` に残し、日次レポートが描く前に `quality.snapshot` に写す（`ingest.py`）。マージ済みの工場の PR の難易度（`pr.difficulty`）も同じときに残す。日報に推移と難易度別の変更のコストを出す。

LLM を使わない。数値はワーカーのプロンプトに入れない（目標にさせない）。

## 製品の設定（.factory-config/quality.yaml）

```yaml
schema_version: "0.1"
languages:
  rust:
    src: ["apps/backend/crates/*/src/**/*.rs"]
    tests: ["apps/backend/crates/*/tests/**/*.rs"]
    inline_test_marker: "#[cfg(test)]"   # これより後ろの行はテストのコードとして扱う
  dart:
    src: ["apps/student-app/lib/**/*.dart"]
    tests: ["apps/student-app/test/**/*.dart"]
exclude:                                 # 正規表現。生成物や検査そのものを外す
  - "^apps/admin-web/src/api/schema\\.ts$"
```

## 手元で動かす

```sh
pip install pyyaml lizard
python3 health/quality/snapshot.py measure --root <製品> --config <製品>/.factory-config/quality.yaml \
  --definitions definitions/quality.yaml --extra layer_violations=0
python3 -m pytest health/quality/tests
```

## 既知の誤差

- 複雑さと関数の長さは lizard が読める言語だけ（Gen1 は Rust と TypeScript）。Dart は行数・重複・テストの数だけ
- Rust のファイルの中のテスト（`#[cfg(test)]`）は、その行から後ろをテストとみなす（テストのモジュールがファイルの末尾にある前提）
- 重複は 6 行の窓の一致で、import と記号だけの行は見ない。言い換えた重複（名前だけ違う）は拾わない
