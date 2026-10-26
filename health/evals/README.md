# health/evals

検査官の校正（Who inspects the inspector）。`definitions/evalspec.yaml` の `l4-pairwise` を実装する。

```bash
python3 health/evals/pairwise_judge.py \
  --golden ~/src/github.com/guccyon/rally/main/.factory-config/evals/golden \
  --out pairwise.json [--only-confirmed]
```

- 各ペアを a/b の順序を入れ替えて 2 回判定し、両方が同じ側を選んだときだけ数える
- 出力は pairwise_accuracy と Wilson 95% 区間。N < 100 の暫定指標で、ブレーカーには使わない
- 判定器は builder の Mac の `claude` CLI（tier-judge = claude-opus-5-5）。`--bare` は認証を読まないので付けない
- govern の経路。learn（overlay）の出力は入力にしない
