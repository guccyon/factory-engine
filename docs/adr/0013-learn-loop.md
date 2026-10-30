# ADR 0013: Learn ループ（教師信号 → Lesson → ルールのライフサイクル）

- 状態: 承認（2026-10-30。製品の manifest の factory_rule はオーナーが「採用まで自動」を選択。数値は委任の範囲で仮置き）
- 関係: 憲章 05 節の図 4（自己進化ループ）・06 節（Govern は Learn から独立）・07 節の P3・09 節（機密区分）、ADR 0006（judge と判断の記録）、ADR 0004（supervisor）、`definitions/learning.yaml`、`core-gates/rule_eval.py`

## 文脈

P3 の Exit は「自動生成されたルールが shadow を経て採用され、採用後に対応する教師信号が減ったことを示せる」。
材料はすでに溜まり始めている。judge の裁き（検査の指摘の採用・仮決めの差し替え）と人の採点が artifacts の Judgment と events に、差し戻しが `sendback.*` に、停止が `stoppage.*` に、Golden Set の負例が製品の `evals/golden/` に、ワーカーの「## 知見」が Lesson（episodic、記録だけ）にある。
足りないのは、それらを束ねて Lesson にし、ルールとして検査とワーカーに戻し、効いたかを測って残すか捨てるかを決める流れ。
製品の manifest では factory_rule（overlay と evals の変更）が `pr_create: shadow, merge: forbidden`（P0 は自己進化を動かさない）のままになっている。

P2 の Exit のうち 2 つ（承認ボタンだけのリリース、1 分以内の Rollback）は、仕組みはできていて、オーナーがコンソールのボタンを押すことだけが残っている。
憲章の Exit ゲートは「条件を満たさなければ上流へ進まない」だが、残りが人の操作だけで、P3 の作業は P2 の出荷の経路に触れないので、P3 に並行で入る（2026-10-30 オーナー了承「次のフェーズに進めるなら別スレで進めておいてほしい」）。P2 の 2 項目は roadmap.yaml で todo のまま残し、押された時点で done にする。

## 決定

1. **教師信号は一次ソースから取れるものだけ**（`definitions/learning.yaml` の teacher_signals）。検査の指摘の採用、仮決めの差し替え、人の不同意、差し戻し後の残り、revert・hotfix、短期の Churn、Golden Set の負例、停止。KPI 予測誤差・ストア却下・データ要求への訂正・本番障害は発生源ができたら足す（P5）。停止は製品の overlay のルールにせず、Engine の改善に回す
2. **Retrospective は決定的に束ねる**。supervisor が 1 日 1 回、直近 30 日の信号を signature（`rubric:<id>`、`judgment:<類型>`、`context:<L3 のコンテキスト>`）で束ねる。LLM は使わない。束ねた結果は events の `learn.retrospected` と artifacts の Retrospective に残す
3. **Lesson は eval ケースを必ず同梱する**。束 1 つから learn ジョブ（tier-large）が Lesson を書く。正例（ルールが当たるべき差分。元 PR の指摘の箇所や Golden Set の負例）と負例（当たってはいけない差分。差し戻しの修正 PR や元 PR）を 1 件以上ずつ持たない Lesson は作らない。差分は教師信号の PR から機械的に切り出し、learn ジョブには選ばせるだけにする
4. **3 段階の結晶化**。Stage 1 エピソード（1 件から。似た変更のときだけワーカーに渡す）→ Stage 2 ルーブリック（同じ signature が 30 日で 3 件以上、2 つ以上のチケットから。検査官のルーブリックに足す）→ Stage 3 決定的な規則（Stage 2 が adopted になり、差分の正規表現に書けて同じ eval ケースを全部通したとき。元の Stage 2 には降格注記）
5. **ルールは製品の `.factory-config/overlay/rules/OV-NNN.yaml` に 1 ファイル 1 ルール**。id は base の rubric と混ぜず、再利用しない。`confidentiality: product | client | public` を必須にする（憲章 09 節）。eval ケースの差分は `.factory-config/evals/rules/<id>/` にファイルで置く
6. **ライフサイクルは candidate → shadow → adopted → retired**（`definitions/learning.yaml` の lifecycle）。shadow は判定して記録するが差し戻さない。shadow で 5 回以上発火し、judge と人の裁きに照らした的中率が 0.8 以上で adopted。採用から 7 日はカナリアで、そのルールの範囲の教師信号が増えたか偽陽性率が 0.3 を超えたら shadow に戻す。発火ゼロ 60 日、偽陽性の超過 14 日、矛盾、根拠の Lesson の無効化で retired。ファイルは消さない
7. **ルール変更 PR は工場自身のパイプラインを通る**。決定的な部分は `core-gates/rule_eval.py`（形の検査、正規表現のルールの eval ケース、retired 以外の全ルールの再実行で退行を見る）を製品の CI が Engine の固定した版で走らせる。LLM が要る段（エピソード・ルーブリック）の eval ケースは工場の inspect ジョブが判定し、ルールの digest と head に結びつけて events に残す。supervisor は factory_rule の PR を、両方が緑のときだけマージの対象にする
8. **学習器に自分を採点させない**。ルールの的中率・効き目・カナリアの判定は govern 側（factoryctl report learning と supervisor の表引き）が events から計算し、learn ジョブには計算させない。健康が赤のとき（breaker learning_freeze）は adopted への昇格を止め、shadow は続ける
9. **Exit は件数の比で示す**。そのルールの signature に当たる教師信号の件数を、そのルールの範囲に触れた工場の PR の件数で割り、shadow 前の 30 日と adopted 後の 30 日を比べる。件数が少ないので Wilson の区間を併記する
10. **還流（Engine への昇格）は P6**。public かつ複数の工場で独立に発生したものだけ。Gen1 は工場が 1 つなので、記録だけ残す

## 製品の manifest の factory_rule（憲法。2026-10-30 オーナーが選択）

製品の manifest の factory_rule の行。決定は「採用まで自動」: `pr_create: auto, merge: auto`（7 の検査が緑のときだけ。adopted への昇格も 6 の条件を満たしたら自動、カナリアで自動で戻し、日報で事後に知らせる）。
ほかの案は「採用だけボタン」（shadow までは自動、adopted への昇格はコンソールのボタン）と「今のまま」（ルール変更 PR を作らない）。
オーナーは「採用まで自動」を選んだ（rally の manifest に反映）。supervisor は CI が緑（mergeable_state が clean）の PR だけを表に掛けるので、rule-eval が通らないルール変更 PR はマージされない。

## 選ばなかった案

- **Lesson を LLM に自由に書かせ、eval ケースを後から足す**: 根拠のない規約が溜まる（憲章のリスク「誤ルールの蓄積」）。eval ケースのない Lesson は作らない
- **束ねるのも LLM に任せる**: 同じ材料に同じ束を返さず、効き目の比較ができない。signature で機械的に束ね、LLM は束の中身を言葉にするだけにする
- **ルールを base の rubric.yaml に足す**: Engine の再コンパイルで学習が上書きされ、製品固有の知識が Engine に混ざる（機密区分の前提が崩れる）。overlay に置く
- **採用の判定に learn ジョブの自己申告を使う**: 学習器に自分を採点させることになる（憲章 06 節）。govern が events から計算する
- **P2 の Exit を待ってから P3 に入る**: 残りは人のボタン操作だけで、待つと工場の建設が止まる

## 結果

- 工場の誤りが signature ごとに数えられ、どのルールがどの誤りを減らしたかを Exit の証拠として出せる
- overlay のルールは状態と根拠と eval ケースを必ず持ち、増える一方にならない（廃止の条件と、Learning の指標 rule_inflow / rule_outflow / dead_rule_ratio）
- 段取り: (1) 本 ADR・定義・`rule_eval.py`（この PR）→ (2) 製品の CI に rule_eval を足す → (3) supervisor の Retrospective と learn ジョブ、`factoryctl report learning` → (4) 検査（inspect）が overlay のルールを shadow で当てる → (5) オーナーの選択に沿って manifest の factory_rule を変え、最初のルール変更 PR を流す
- Stage 1 のエピソードと adopted のルールをワーカーに渡す変更は、外部入力（工場が書いたルール）がエージェントのプロンプトにつながる経路なので、その PR はオーナーの承認を得てからマージする
