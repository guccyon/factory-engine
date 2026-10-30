# ADR 0012: 出荷（Ship）と Rollback を建設者の端末の Docker で行う

- 状態: 承認（2026-10-30 オーナーが出荷先の案 A を選択。細部は builder に委任、事後報告。操業コンソールから出荷を動かす経路は外部入力からコマンドへの経路なので、その PR はオーナーの承認を得てからマージする）
- 関係: ADR 0007（来歴と Release）、ADR 0009（操業コンソール）、製品の ADR（rally の docs/adr/0004 expand/contract）、憲章 07 節の P2

## 文脈

P2 の Exit 条件のうち 2 つは出荷の実績を求める。「人間の操作が承認ボタンだけでリリースが完了した」「Rollback が 1 分以内に完了した」。
Gen1 の製品（rally）には出荷先がない。予算の都合で常時稼働のサーバーは後回し（戦略会議 D1）。
出荷先の候補は 3 つだった。A: 工場と同じ端末の Docker に検証と本番相当の 2 環境、B: クラウドの無料枠、C: 予算が付くまで出荷を作らない。オーナーが A を選んだ。

## 決定

1. **環境は 2 つ: staging と prod**。どちらも工場と同じ端末の Docker Compose の別のプロジェクト（`<製品>-staging` / `<製品>-prod`）で、DB を別に持つ。prod は「本番相当」で、合成でない利用者には出さない（運用開始 D4 は発動しない）。置き場所を変えるとき（EC2 など）は環境の定義だけを差し替える
2. **製品が出荷の形を持ち、工場は手順を持つ**。製品はイメージの作り方（Dockerfile）と環境の組み立て（compose のファイル。イメージの名前と版は環境変数で受ける）と、起動を確かめる URL を持つ。工場（shipper）はどの製品でも同じ手順で動かす: イメージを作る → staging に出す → 確かめる → 承認を待つ → prod に出す → 確かめる。製品ごとの値は manifest の `ship` に書く
3. **版ごとにイメージを残す**。イメージの名前は `<製品>-<部品>:<app_version>`。Release（ADR 0007）を切ると、その Release の先頭のコミットからイメージを作る。同じ版を作り直さない
4. **staging へは自動、prod へは承認ボタン**（manifest の phase_grants の P2: deploy_test は auto、deploy_prod は canary_human_approve）。承認ボタンは操業コンソールの「本番に出す」（`factoryctl release approve` でも同じ）。承認は events の `release.approved`（誰が・どこから）に残る。押されなければ prod は前の版のまま（未応答は進めない）
5. **Rollback は前の版のイメージに戻すだけ**。操業コンソールの「前の版に戻す」（`factoryctl release rollback`）。DB は戻さない。押してから起動を確かめ終わるまでの秒数を `release.rolled_back` に残し、目標は 60 秒（manifest の `rollback.target_seconds`）
6. **DB は前進だけで、Rollback できることを expand/contract が支える**。新しい版は起動時に自分のマイグレーションを当てる。expand と移行は古い版のアプリがそのまま動くので、前の版に戻しても DB はそのままでよい。前の版のアプリは自分の知らないマイグレーション（新しい版が当てたもの）を無視して起動する。contract を含む Release は、その列を使わなくなった版がすでに prod に出ているときだけ出せる。contract を含む Release の Rollback 先は、その直前の版まで（それより前は列を使っていた）
7. **出荷の来歴は Release に付ける**。`release.built`（イメージの ID）、`release.deployed`（環境、イメージ、所要秒数、確かめた結果）、`release.approved`、`release.rolled_back`。ADR 0007 の「出荷の経路ができたら environment と rollback_target を足す」をここで満たす
8. **出荷が失敗したら停止として開く**（分類 factory-bug か ticket。staging で起動を確かめられない版は prod に出さない）

## 選ばなかった案

- **クラウドの無料枠（B）**: 本番に近いが、アカウントと外部への書き込みが増え、無料枠の制約（休止・起動の遅さ）が Rollback の秒数に混ざる
- **出荷を後回しにする（C）**: P2 の Exit 条件のうち 2 つが測れないまま P3 に進めない
- **DB も Rollback で戻す**: データを失う。expand/contract で前の版が動く形にしておく方が、戻す操作が速く安全
- **承認を GitHub の Environment の承認にする**: 端末の Docker に出すには、Actions の実行環境を端末に置く（self-hosted runner）必要があり、権限の面が広がる。コンソールのボタンで足りる

## 結果

- 出荷・承認・Rollback の 3 つが events に残り、Exit 条件の 2 つを実績で示せる
- 端末が眠っていると出荷も止まる（keep-awake.sh、ADR 0011 の 7）
- **iOS のビルドと配布はオプション**（2026-10-30 オーナー。憲章 P2 の「iOS ビルドは標準かオプションか」の判断）。Engine の標準の出荷（この ADR）には入れず、工場の個別のケイパビリティの 1 つとして別のプロジェクトで作る。Gen1 のモバイル（Flutter の student-app）は出荷の対象外
