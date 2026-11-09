# templates/ci — 製品の CI に写すテンプレート

## Engine の版（`.factory-config/engine.lock`、ADR 0041 の決定 4）

製品の CI が Engine の道具（`core-gates/` など）を取る版は、ワークフローに書かず、製品の `.factory-config/engine.lock` に 1 か所で書く。
版上げはこのファイルの 1 行の変更になり、ワークフローの権限（特権のライン）が要らない。変更種別は factory_rule（`definitions/change_type_rules.yaml`）。

```text
# 製品の CI が使う factory-engine の版（main にある commit の 40 桁の SHA）
9d3713a69b7784fc31069bf370002d554bfc3a64
```

Engine の道具を取るワークフローは、checkout の直後に次の手順を置き、以後は `$ENGINE_REF` を使う。
factory-engine の main にない版（フォークのコミットなど）は受けない。PR の側で engine.lock を書き換えても、main に入った Engine のコードしか CI で動かないようにするため。

```yaml
      - name: Engine の版を読む（.factory-config/engine.lock。factory-engine の main にある版だけ受ける）
        run: |
          lock=.factory-config/engine.lock
          if [ ! -f "$lock" ]; then echo "ENGINE_REF=self" >> "$GITHUB_ENV"; exit 0; fi
          ref=$(grep -Ev '^[[:space:]]*(#|$)' "$lock" | head -1 | tr -d '[:space:]')
          if ! printf '%s' "$ref" | grep -qE '^[0-9a-f]{40}$'; then echo "::error::$lock には 40 桁の commit SHA を 1 行で書く"; exit 1; fi
          git init -q "$RUNNER_TEMP/engine"
          git -C "$RUNNER_TEMP/engine" fetch -q --filter=blob:none https://github.com/guccyon/factory-engine.git main
          if ! git -C "$RUNNER_TEMP/engine" merge-base --is-ancestor "$ref" FETCH_HEAD 2>/dev/null; then echo "::error::$lock の版 $ref は factory-engine の main にない"; exit 1; fi
          echo "ENGINE_REF=$ref" >> "$GITHUB_ENV"
```

`self`（engine.lock が無い）は factory-engine 自身のためのもので、自分の木の版を使う。`self` を扱わないワークフローは、engine.lock が無ければ落としてよい。

## テンプレート

| ファイル | 内容 |
|---|---|
| `diagnose.yml` | PR ごとの診断（依存 osv-scanner・静的解析 semgrep・秘密情報 gitleaks）。shadow。写すときは何も変えない |
