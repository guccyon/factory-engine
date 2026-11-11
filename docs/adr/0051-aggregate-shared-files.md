# ADR 0051: よく変わる共有ファイルを見つけ、断片にためて取りまとめの PR で更新する（ADR 0027 の 1 の一般化）

- 状態: 承認（2026-11-12 オーナー「その方向で良い」。依頼は「CHANGELOG のように変更をためてそのファイルだけを更新する PR を切り出す。変更頻度の高いファイルを見つける手段も」）
- 関係: ADR 0027（CHANGELOG の断片と取りまとめ、衝突を避ける並べ方、工程の信号）、ADR 0038（衝突の解消の上限、追記 2026-11-11 の衝突しやすいファイルの印 `hot_marks.py`）、ADR 0023（承認の線）、rally #1369（知見の README の手書きの一覧をやめた）、factory-engine #392（衝突の上限で止まった PR を受信トレイに出す）

## 文脈

ADR 0027 では CHANGELOG.md だけを断片（`changelog.d/`）にした。その結果、CHANGELOG の衝突の解消は 1 日 400 件から 0 件近くまで減った。ただ、同じ形の衝突はほかのファイルでも起きている。

- rally #1117 は、担保の PR がみな知見の README の一覧に 1 行足していたため、衝突の解消を 13 回くり返して止まった（11/8〜11/11）。直しは #1369 の 1 件だけで、ほかのファイルには効かない
- 「衝突しやすいファイル」は、ADR 0038 の追記（2026-11-11）の印（`hot_marks.py`、直近 50 PR の変更回数の上位で 3 回以上衝突したファイル）で見つけ、プランナーと指示役が同じファイルに触るタスクを並べて流す。ただ、並べると流量が落ちるだけで、衝突のもと（同じ箇所に足す作り）は残る。印が付くのも衝突に費用を払ったあと

rally の main の直近 2 週間（10/28〜11/12、539 コミット）で、多くのコミットが触ったファイルは次のとおり。

| ファイル | コミット | 形 |
|---|---|---|
| CHANGELOG.md | 191 | 追記（断片化済み。残りは移行前の PR） |
| specs/contracts/api/admin-web/openapi.yaml | 126 | 契約の正本。中身が毎回違う |
| apps/admin-web/src/api/schema.ts | 125 | openapi.yaml から作る生成物 |
| apps/admin-web/screenshots/fixtures.ts・screens.ts | 92・82 | 画面ごとの登録の一覧 |
| apps/admin-web/src/api/headquarters.ts | 80 | 機能ごとに足す API の口 |
| tools/knowledge-enforcement-check/README.md・test_check.py | 73・54 | 知見の id の手書きの一覧（#1369 で直した） |
| apps/backend/README.md・apps/admin-web/README.md | 70・54 | 機能の一覧を足す文書 |
| apps/backend/crates/interface/src/lib.rs・server/src/lib.rs | 53・42 | ルートの登録 |

形は大きく 3 つに分かれ、それぞれ効く手が違う。

1. **追記型**（一覧・機能の表・変更履歴）: 断片にためて取りまとめれば、衝突はなくなる
2. **生成物**（schema.ts など）: 手で書かせず、正本から生成すれば衝突しない。衝突しても生成し直せば解ける
3. **登録の箇所**（screens.ts・lib.rs のルート）: コードなので断片にはしにくい。分割（機能ごとのファイルにして自動で集める）が要る。これは製品の構造の改善になる

## 決定

### 1. よく変わるファイルを見つける（印の見回りを広げる。決定的、LLM を使わない）

ADR 0038 の追記の印（`hot_marks.py`、Learn ループの日次の振り返り）を土台にし、同じ集計に次を足す。新しい見回りは作らない。

- **形**: 直近の PR の、そのファイルの差分の追加行と削除行（`pulls/<n>/files` の additions・deletions。変更ファイルと一緒に `pr.files` に残す）から、追記の割合を出す。8 割以上が追記なら追記型。先頭に「生成物」「generated」と書かれたファイルと、製品が `generate` で宣言したファイルは生成物。どちらでもないコードは登録の箇所
- **先回りの候補**: 衝突の回数が印の閾値（3 回）に届かなくても、変更回数の上位（20 位まで）で、同じ期間に同時に開いていた工場の PR が 3 本以上あった追記型・生成物は「取りまとめの候補」にする（`hotfile.aggregate_candidate`）。衝突が起きる前、PR が重なり始めた時点で気づける
- 印（`hotfile.marked`）にも形を持たせる。印の付いた追記型・生成物は、取りまとめの候補を兼ねる
- 候補と印は、形ごとに分けて開発状況レポートと建設日報に出す。閾値は運用パラメータ（`definitions/workflow.yaml` の `hot_files`）

### 2. 製品が「取りまとめるファイル」を宣言する

製品の `.factory-config/aggregates.yaml` に、断片で更新するファイルを書く。CHANGELOG はその 1 件目にする（今の `changelog.d/` の決まりをここに移す）。

```yaml
aggregates:
  - path: CHANGELOG.md
    fragments: changelog.d/        # 1 PR 1 ファイル（<Issue 番号>.md）
    compile: changelog             # 種類ごとに [Unreleased] に移す（今の changelog_compile.py）
  - path: apps/backend/README.md
    fragments: apps/backend/readme.d/
    compile: append_section        # 断片の中の「## 見出し」ごとに、同じ見出しの節の末尾に足す
    section_order: sorted          # 足す順（sorted か merged）
  - path: apps/admin-web/src/api/schema.ts
    compile: generate              # 断片は使わない。取りまとめで正本から作り直す
    command: npm --prefix apps/admin-web run gen:api
```

- 取りまとめの種類は決まった少数（`changelog`・`append_section`・`sorted_list`・`generate`）にする。中身はどれも決定的で、Engine の `core-gates/aggregate_compile.py`（今の `changelog_compile.py` を広げたもの）が行う
- **取りまとめの PR**: supervisor が、断片がたまった（仮置き 10 件）、いちばん古い断片が一定時間（仮置き 24 時間）たった、または出荷の前、のどれかで取りまとめのチケットを積む（今の `changelog_step` を、宣言されたファイルごとにしたもの）。PR は宣言したファイルの更新と断片の削除だけにする。中身が本当にそれだけかを決定的に確かめ、それだけなら検査と judge を飛ばして CI が通ればマージする
- **読む側のずれ**: 製品の検査やテストが一覧の中身を照らしている場合（#1369 の前の test_check.py のように）は、「ファイル + まだ取りまとめていない断片」を合わせた形で照らす。そのための読み口（`aggregate_compile.py --view <path>`）を用意する

### 3. 宣言したファイルの直書きを止める

- ワーカーの指示と製品の CLAUDE.md に「宣言したファイルは直接書き換えず断片に書く」と足す（CHANGELOG と同じ書き方）
- 製品の CI のテンプレートに決定的な検査を足す。工場の PR が宣言したファイルを直接書き換えていたら落とす（取りまとめの PR は除く）。宣言しただけで直書きが残り、衝突が続く、ということがないようにする

### 4. 候補を宣言に回す道筋と承認の線

- 1 の候補のうち **追記型** は、工場が `aggregates.yaml` への追加と断片の置き場の用意を製品の改善チケットとして起票する。製品の書き方の変更なので、ADR 0023 の線どおり製品の改善チケットとしてラインで作る（rally は自動マージの範囲）
- **生成物** は、`compile: generate` の宣言と、CI での生成し直しの確かめを製品の改善チケットにする
- **登録の箇所** は、プランナーが分割の改善チケットを提案する（ADR 0027 の 2 の続き）。製品のアーキテクチャの変更なので、人の判断の扱いは既存の規則（ADR 0019 の改善チケットの上限）に従う。分割ができるまでは、ADR 0027 の 2 の並べ方（同じファイルに触るタスクを同時に流さない）の対象にする
- 起票は 1 日 1 件までにし、同じファイルの起票が開いていれば足さない（`process_issue_step` と同じ）

## 順番

1. 印の見回りに形と先回りの候補を足す（1）。読むだけで、日報に候補が出る。今の rally で上の表の追記型（知見の README 類・apps の README）が候補に出ることを受け入れの条件にする
2. `aggregates.yaml` と `aggregate_compile.py`、取りまとめの PR の一般化（2）。CHANGELOG を宣言に移し、今の動きが変わらないことを確かめる
3. 直書きを止める CI の検査とワーカーの指示（3）
4. 候補を改善チケットにする道筋（4）

## 選ばなかった案

- **`.gitattributes` の `merge=union`**: 追記どうしの衝突は消える。ただ、GitHub の PR の衝突の判定とマージはこの設定を使わない（手元の git だけで効く）ので、工場の PR の衝突は減らない。行の順と重複も保証されない
- **衝突をくり返した PR を優先してマージする**: ほかの PR を止めることになり、流量のつくりの変更が大きい。共有の箇所をなくす方が効く
- **頻度だけで候補を出す**: openapi.yaml のように、中身が毎回違う正本も上位に来る。形を見ないと、断片にできないものまで候補になる
- **候補を LLM に選ばせる**: 同じ記録に同じ答えを返さず、効き目を比べられない（ADR 0027 と同じ）

## 結果

- 追記型の共有ファイルの衝突が、CHANGELOG と同じように構造からなくなる見込み
- 衝突が起きる前、PR が重なり始めた時点で候補に気づける。並べて流す（ADR 0038 の追記）のは、取りまとめにできない登録の箇所が主になり、流量が戻る
- 製品の書き方が増える（宣言したファイルは断片に書く）。取りまとめの PR が増えるが、検査と judge を飛ばすので費用はほぼ CI だけ
- 登録の箇所（コード）は、この ADR だけではなくならない。分割の改善チケットとして別に進める

## 実施の記録

- 2026-11-12 順番 1（#396）: `hot_marks.py` が `pr.files` に差分の行数を残し、形（`append`・`generated`・`registry`・`content`）と同時に開いた PR の数を数える。取りまとめの候補は `hotfile.aggregate_candidate`。建設日報に「共有ファイル」の節
- 2026-11-12 順番 2〜4:
  - 2: `core-gates/aggregate_compile.py`（`changelog`・`append_section`・`sorted_list`・`generate`、`--view`・`--list`）。宣言がなければ CHANGELOG.md だけで、今の動きは変わらない。ワーカーは `factory:changelog` のラベルのチケットでこれを呼ぶ。supervisor の `changelog_step` は宣言したファイルの断片を合わせて数え、製品ごとに 1 本の取りまとめの Issue を起こす（題は CHANGELOG だけなら今までどおり）。断片がいちばん古いものの経過時間での取りまとめは、今までの「出荷の前」と件数で足りるので入れていない
  - 3: `core-gates/aggregate_guard.py` と CI のテンプレート（`templates/ci/README.md`）。ワーカーの指示に、宣言したファイルは断片に書く（生成物は作り直す）旨を宣言から作って足す。検査のルーブリック DOC-002 も宣言に広げた。製品のワークフローに足すのは特権のライン
  - 4: supervisor の `aggregate_issue_step`。直近の候補のうち宣言していないファイルを、1 日 1 件、プランナーの入口（`factory:plan`、`factory:aggregate`）の改善チケットにする。一度起票したファイルは足し直さない（閉じたら人の判断を尊重）。`AGGREGATE_ISSUES=0` で止める。登録の箇所（コード）は候補にしないので、ここからは起票しない（ADR 0027 の 2 の並べ方とプランナーの分割の提案に任せる）
- 2026-11-12 印とのつなぎ目: 取りまとめに宣言したファイルは、`hot_marks.py` が印も候補も付けず、付いている印はその日のうちに外す（`hotfile.unmarked` の why「取りまとめに宣言した」）。7 日待たずに並べ方（ADR 0038 の追記）から外れる。宣言の読み口は `hot_marks.declarations` の 1 つにし、supervisor の取りまとめ（`aggregate_fragments`）と起票（`aggregate_declared`）もこれを使う。`CHANGELOG.md` の除外も `hot_marks.EXEMPT` の 1 つにした
