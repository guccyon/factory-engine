# ADR 0004: 工場長の分離と supervisor

- 状態: 承認（2026-10-27 戦略会議 第 1 回の決定 D1・D2。実装の細部と閾値は委任の範囲で決め、運用で調整する）
- 関係: 戦略会議の議事録（建設者の手元の 50_build-work/strategy/minutes/2026-10-27_strategy-meeting.md の 3 節・11 節・12 節）、ADR 0003（制御プレーン）、ADR 0005（番人）、ADR 0006（judge。未作成）、製品の `.factory-config/manifest.yaml`（autonomy）

## 文脈

P1 入口までは、ジョブの投入・マージ・検査の起動・イメージの更新を、建設者の手元の Claude が手で回していた。
その Claude が使用量の上限や許可待ちで止まると、工場全体が止まる（操業停止の記録 S01・S13・S15・S16）。
ラインの中の human in the loop を減らし、判断が要るものは工程の最初か最後に寄せるのが戦略会議の方針で、
「工場長」の役割を 3 つに分けることにした: 状態を見張る番人（ADR 0005）、工程を起動する supervisor（本 ADR）、判断する judge（ADR 0006）。

## 決定

1. **supervisor は判断を持たない**。状態（制御プレーンと GitHub）と表（製品の manifest、Engine の `definitions/`）を見て、決まった工程を起動するだけ。意味の解釈が要るものは judge に回す
2. **常駐プロセスとして Compose に足す**（`supervisor`。dispatcher と同じイメージで別プロセス）。dispatcher と分けるのは、落ちたことを番人が heartbeat の途絶えで見られるようにするため。60 秒ごとに `supervisor.heartbeat` を events に出す
3. **最小形で起動する工程**:
   - **マージ**: 工場の PR が GitHub で `mergeable_state=clean` になったら、変更ファイルを `definitions/change_type_rules.yaml` で manifest の変更種別に写し（パスと差分の正規表現だけ）、manifest の matrix と always_human_approve を引く。表の値と、工場の権限（`phase_grants[phase].merge`）を分けて `supervisor.merge_decided` に記録する。両方 auto のときだけ squash でマージし、表が承認を求めれば PR に `factory:needs-approval` を付ける。表にない種別は承認に倒す
   - **結果の記録**: 判定した PR が閉じたら、誰がマージしたか（`pr.merged`）か、閉じただけか（`pr.closed_unmerged`）を記録する。判定と人の判断の一致率が、工場のマージを解放する根拠になる（manifest の release_conditions）
   - **依存順の投入**: `factory:planned` の Issue の本文の「依存: #N」がすべて閉じたら `factory:implement` に替える。同時に流すのは、実行中・投入済みのジョブと開いている工場の PR の合計で 1 本まで（仮置き。同じファイルの衝突を避ける）
4. **Issue と PR の本文は正規表現で番号を拾うだけで、シェルにもプロンプトにも渡さない**。このため本 ADR の範囲は承認不要（事後報告）
5. **マージのトークンは分けられるようにする**（`SUPERVISOR_GH_TOKEN`。空ならワーカーと同じ）。ワーカーは PR 作成まで（ADR 0003 の 6）という約束を、トークンの権限でも守れるようにするため
6. **置き場所は Mac で試す**（D1）。EC2 などの常時稼働の基盤は予算が付いてから。Compose に閉じているので移すときは同じ定義を持っていく

## 後に回したもの

- **検証ジョブの起動**（工場の PR ごとに L4 の shadow 判定を走らせる）: PR の本文と差分がエージェントのプロンプトにつながる経路なので、マージ基準で承認が要る。別の PR で足す
- **イメージの更新**（CI でビルドしてレジストリから取る）: ワーカーの PAT にワークフローの権限がなく、レジストリの置き場所も決めていない。いまは手元で既存のイメージに重ねて作り直す
- **工場のマージの解放**: 2026-10-27 時点の rally の manifest は `phase: P0`（工場のマージは human_approve）で、P1 でも shadow。supervisor は判定を記録するだけで、実際のマージは建設者が行う。解放は manifest の変更（憲法）なので、一致率の実績を添えて承認付き PR で行う

## 選ばなかった案

- **supervisor を dispatcher の中のスレッドにする**: プロセスが 1 つなら heartbeat の途絶えで落ちたことが分からない。番人と supervisor が同時に止まる
- **supervisor を LLM のエージェントにする**: 表を引くだけの仕事にトークンを使う理由がなく、判断が混ざると記録の 3 層（ADR 0006）と分けられない
- **変更種別を LLM に分類させる**: 判断は judge に寄せる。機械的な写像で外れたもの（例: rally #73 の identity の文脈の email 検証のテストは、認証・権限ではない）は、写像の表を直して吸収する
- **CI の結果を check-runs から自前で集計する**: 必須チェックは ruleset が持っている。`mergeable_state=clean` は必須チェックの通過と衝突のなさを GitHub が判定した値なので、それに従う

## 結果

- 建設者の手元の Claude が止まっても、heartbeat・依存順の投入・判定の記録は続く
- マージの判定と人の判断の一致率が events から出せる。週次レポートに載せ、工場のマージを解放する根拠にする
- 変更種別の写像の表は運用で直す。直すたびに、外れた PR を表のコメントに残す
