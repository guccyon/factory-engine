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
- 各ペアの `outcome` は agree / disagree / tie_held / tie_missed / abstained / error。一致率の分母は agree と disagree だけ。建設者の選好が tie のペアは `golden_ties_held`、建設者に選好があって判定器が tie か順序で割れたものは `judge_abstained` に数える（どちらも不一致には数えない）
- ペアの `ref` は `github:<owner>/<repo>#<PR>` のほかに `file:<golden からの相対パス>`（負例として作った差分）を取れる。`file:` には `derived_from`（元にした PR）を添え、題はその PR の題を使う（題で負例と分からないように）
- 出力の `by_polarity` は golden の `polarity` ごとの件数と一致。負例として作ったペア（`one_flawed`）だけで一致率を満たしても校正としては弱いので、`both_acceptable` と分けて見る
