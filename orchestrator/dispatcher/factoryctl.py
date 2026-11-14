#!/usr/bin/env python3
"""工場の手動操作: 一覧・停止・再実行（ADR 0003）。工場を動かすマシンで docker exec factory-dispatcher-1 factoryctl … か、compose のディレクトリで docker compose exec dispatcher factoryctl … と打つ（下では factoryctl … と略す）:

  factoryctl list [--all]   # ジョブの一覧（順番待ち・作業中と直近 1 日。--all はすべて、新しい順に 50 件）
  factoryctl stop <job_id>   # ジョブを止める（作業中ならコンテナは dispatcher が止める）
  factoryctl rerun <job_id> [--continue]   # 続きのジョブは続きとして積み直す。--continue: 途中終了のドラフト PR のブランチから続ける（1 回まで。2 回目は plan resplit か --force）
  factoryctl enqueue <repo> <issue> [--tier tier-small]   # Issue を手で積む（試験運転や、ラベルを使わない再投入）。--tier でモデルの tier を変える
  factoryctl events <job_id>   # ジョブの出来事（events）を時刻の順に

チケットの順番（ADR 0009）。操業コンソールと同じ操作。<ticket> はチケットの id か owner/repo#123:
  factoryctl tickets [--all]                      # 投入待ち・作業中のチケット（優先度の高い順）
  factoryctl priority <ticket> p0|p1|p2|p3|<0-100> [--reason ..]   # p0 は作業中のものを止めてでも先に流す
  factoryctl hold <ticket> [--reason ..]          # 順番待ちから一時的に外す
  factoryctl resume <ticket>                      # 保留を外して順番待ちに戻す

工場の更新（ADR 0020）。ラインの一時停止の間は順番待ちを取り出さない（実行中のジョブは最後まで流す）。操業コンソールと同じ操作:
  factoryctl drain [--reason ..] [--wait [秒]]     # 一時停止にする。--wait は更新してよくなる（実行中 0・出荷の処理なし・一時停止でも流れる順番待ちなし）まで待つ（秒を省くと 1800）。なれば終了コード 0、待ち切れなければ 3
  factoryctl drain status                          # 一時停止か、更新してよいか、何を待っているか。更新してよければ終了コード 0
  factoryctl line [status]                         # ラインの流量の宣言と今の数（ワーカー・同時に流すチケット・委員会の枠。ADR 0028）
  factoryctl line scale [--workers N] [--tickets N] [--governance N] [--desk N] [--reason ..]   # 宣言を変える。上げ直さずに次の周回から効く
  factoryctl budget [status]                       # 使用量の予算（毎日同じ額か曜日ごと。7:00 JST 区切り）と今日の予算
  factoryctl budget set [--daily USD] [--mon USD .. --sun USD] [--mode daily|weekday] [--reason ..]   # 予算を宣言する。上げ直さずに次の周回から効く
  factoryctl drain allow <PR か Issue の URL>... [--reason ..]   # 一時停止のまま、その PR のジョブ（衝突の解消・検査・judge・委員会）か、その Issue のチケットのジョブだけ流す。再開で消える
  factoryctl drain allow --priority p0 [--reason ..]    # 一時停止のまま、優先度がそのレベル以上のチケットのジョブ（着手判定・実装を含む）も流す。再開で消える
  factoryctl runner check                          # Mac ランナー（ADR 0033）に届くか: 窓口・ペアリング済みの台数・道具・端末。届かなければ終了コード 1
  factoryctl distribute [--channel firebase_android] [--app <owner/repo>:<パス>] [--ref main] [--notes ..] [--define K=V].. [--timeout 秒]
      # モバイルアプリを審査前の配布先に 1 本配る（ADR 0047）。Mac の画面で人が「配布する」を押すまで待つ。配れなければ終了コード 1
  factoryctl distribute list                       # 直近の配布の依頼と結果
  factoryctl version [--expect <コミット>]          # サービスごと・ジョブごとに動いている版（イメージのラベル。ADR 0031）。ずれたサービスか止まっているサービスがあれば終了コード 1
  factoryctl resume [--reason ..]                  # チケットを渡さないときはラインを再開する

工場の更新の自律化（ADR 0031）。dispatcher が使い捨ての更新役（コンテナ factory-swap-<id>）を起動し、事前の確かめ → 一時停止（control_plane のとき）→
すべてのサービスの上げ直し → 確かめ → 再開を行う。確かめに通らなければ前の版に戻す。DB の変更・秘密情報と権限を含む版は人に回す:
  factoryctl swap run <main|コミット> [--kind code|control_plane|db] [--note ..] [--by ..]   # いま更新する（時刻の合意なし。順番 2）。種類は差分から判定し直し、重い方を採る
  factoryctl swap status [<id>]                    # 直近の更新の一覧か、1 件のステップごとの記録。進み具合はホストで docker logs -f factory-swap-<id> でも見られる
  factoryctl swap cancel <id> [--reason ..]        # 更新役を起動する前だけ取り消せる
  （クラウドの建設者は factory-engine の Issue に factory:swap を付けて依頼する。dispatcher が拾い、結果を同じ Issue に返す。orchestrator/README.md）

操業停止（ADR 0005）。番人が見えない停止（builder の端末の許可待ちなど）は手で記録する:
  factoryctl stoppage open <分類> <何が止まったか> [--key K] [--waiting-for ..] [--cause ..] [--prevention ..] [--at ISO] [--by builder] [--quiet]
      # 停止を記録する（番人が見つけられない止まり）
  factoryctl stoppage close <key> [--remedy ..] [--prevention ..] [--at ISO] [--woken-by pr_event|watcher|schedule|human] [--quiet]   # --quiet は通知しない（過去分の取り込み）。始まりを通知した停止は --quiet でも終わりを通知する（#181）。--woken-by は builder を起こしたもの（D8）
  factoryctl stoppage list [--open]               # 停止の記録の一覧（--open はまだ閉じていないものだけ）
  factoryctl report stoppages [--from YYYY-MM-DD] [--to YYYY-MM-DD]   # Markdown。既定は直近 7 日。週次レポートの stoppages.md はこの出力

来歴とロット（ADR 0007）。Release は後付けで、前の Release 以降にマージされた工場の PR をまとめて含める:
  factoryctl release create <repo> <app_version> [--prs URL,URL]   # Release を作る（--prs を省くと前の Release 以降にマージされた工場の PR）

出荷と Rollback（ADR 0012）。依頼を events に書くだけで、手を動かすのは常駐の shipper:
  factoryctl release ship <release_id> [--by ..]            # イメージを作って staging に出す（staging は自動）
  factoryctl release approve <release_id> [--by ..] [--skip-qa --reason ..]   # 承認ボタンと同じ。staging で確かめた版を prod に出す
      # QA の関門（ADR 0049）の報告の前に出すときだけ --skip-qa と理由を付ける
  factoryctl release rollback <repo> [--env prod] [--by ..] # その環境を 1 つ前の版に戻す（DB は戻さない）
  factoryctl release status <repo>                          # 環境ごとのいまの版と前の版、出荷の記録

定期便（ADR 0043 の O6）。1 日 1 便は supervisor が出す。承認は便ごとに 1 回。S1 の修正だけ緊急便で待たずに出す:
  factoryctl release trains [--limit 10]                    # 最近の便（定期・緊急）と載った Release、承認待ち
  factoryctl release train <repo> --emergency --reason ".." [--by ..]   # 緊急便。まだ Release に入っていないマージ済みの PR を今すぐ出す
  factoryctl release train-approve <train_id> [--by ..] [--skip-qa --reason ..]   # 便に載った Release をまとめて prod へ承認する（本番を立てない段階の製品は飛ばす）
  factoryctl report provenance [--from YYYY-MM-DD] [--to YYYY-MM-DD]   # 来歴の付与率と Release への紐付け率

週次の KPI の Report ライン（ADR 0043 の O2・O3）。supervisor が毎週月曜 7 時（JST）に staging で測る。今すぐ 1 回測るには --measure:
  factoryctl report kpi [<repo>] [--measure] [--env staging] [--timeout 300]   # いちばん新しい週次の KPI。--measure は測る依頼を書き、結果を待って Report・Opportunity の下書き・見込みの採点まで回す

触れる環境（ADR 0028 の touchable_env。Gen1 は staging と兼ねる）。Release 候補を 1 コマンドで立て、シードを入れ、URL と QR を出す:
  factoryctl env up <repo> [--release <id>] [--fresh] [--no-wait] [--timeout 3600] [--by ..]   # 版がなければ未 Release の PR から切る。--fresh は DB を空にしてシードから入れ直す
  factoryctl env show <repo> [--env staging]                 # いまの版の入口（URL・QR・ログインの手がかり）
  factoryctl env restart <repo> [--advance-hours N | --reset-clock] [--env staging]   # 再起動して時計を進める（DB は残す。時間をまたぐシナリオ）

PR ごとの使い捨て環境（ADR 0043 の O7）。PR の head を <製品>-pr-<番号> の compose で立て、シードを入れる。PR が閉じたら supervisor が壊す:
  factoryctl env pr-up <repo> <PR 番号> [--no-wait] [--timeout 3600] [--by ..]   # 同時に EPHEMERAL_MAX（既定 2）まで。fork の PR は立てない
  factoryctl env pr-show [<repo>]                            # 立っている環境の入口（URL・QR・ログインの手がかり）と触った印
  factoryctl env pr-touch <repo> <PR 番号> [--note ..] [--by ..]   # 人が触った印を残す（QA 役をこの環境に向けるのは Gen1.5）
  factoryctl env pr-down <repo> <PR 番号> [--by ..]          # PR が開いたままでも壊す

QA 役（ADR 0028 の決定 2）。触れる環境にユーザーストーリー（specs/stories/）を流し、ストーリーごとに Verdict を出す:
  factoryctl qa run <repo> [--stories id,id] [--env staging] [--priority p0|p1|p2|p3] [--through-pause] [--by ..]
      # 製品の固定 Issue（factory:qa）に積む。--stories を省けば active な全部（上限 6）。--through-pause はラインの一時停止の間もこれだけ流す
  factoryctl qa show <repo> [--limit 10]                     # 最近の判定と夜間の探索
  factoryctl qa gate <repo> [--limit 5]                      # 出荷の QA の関門（ADR 0049）。版ごとの判定と探索のジョブ、ブロッカーの候補、承認者の見送り
  factoryctl qa explore <repo> [--through-pause] [--by ..]   # 夜間の探索（nightly_explore）を今すぐ 1 回積む（その夜のチャーター。人が積んだものは無人の夜に数えない）
  factoryctl qa synthetic <repo> [--friction example|none|<JSON>] [--through-pause] [--by ..]
      # 合成の利用者（ADR 0043 の O4）を今すぐ 1 回積む。仕込みは --friction か工場の SYNTH_FRICTION（staging だけ）。提案は受信トレイに出る
  factoryctl qa learn <repo>                                  # 品質の Learn（ADR 0043 の O9）を今すぐ回す（その週にまだ回っていなければ）

障害の重要度とエラーバジェット（ADR 0043 の O5）。QA 役が S1 の Incident を開くとラインを差し止める（一時停止とは別の札）。解除は人だけ:
  factoryctl line release --reason "..." [--by 名前]   # QA 役の差し止めを解除する（直した・演習を終えた）。理由は必須
  factoryctl incident list [<repo>] [--limit 20]       # 最近の Incident（重要度・演習か・閉じたか）と差し止めの状態
  factoryctl incident budget <repo>                    # エラーバジェット（製品の quality_policy.yaml の error_budget）と期間の中の件数（演習を除く）
  factoryctl incident close <incident_id> --reason "..."   # Incident を閉じる（直った・演習を終えた）
  factoryctl incident drill <repo> [--plant s1_data_leak] [--charter other-user] [--wait 300]
      # S1 の演習: staging に仕込み（ship.yaml の touch.drills）、仕込めたら QA 役の探索を 1 回積む（一時停止中も流す）。終えるまでの Incident は演習の印
  factoryctl incident drill-end <repo> [--reason ..]   # 演習を終える: 演習の Incident を閉じ、staging を合成シードから入れ直して仕込みを消す

工場の事故の台帳（ADR 0046。事業主にお金がかかった工場の失敗。製品の障害 incident とは別）:
  factoryctl incident-record list [--all]              # 台帳（開いているもの。--all で閉じたものも）
  factoryctl incident-record show <IC-...>             # 台帳の 1 件（生の事実と、足した記録）
  factoryctl incident-record open --title "..." --resource <資源> --severity <帯> [--mechanism <仕組み>] [--note "..."]   # 人が気づいた事故を台帳に開く（low もここで）。資源・仕組み・帯は definitions/incident.yaml
  factoryctl incident-record note <IC-...> [--mechanism ..] [--severity ..] [--engine-issue URL] [--fir FIR-YYYY-NNNN] [--note "..."]   # 調べた結果を台帳に足す
  factoryctl incident-record close <IC-...> --reason "..."   # 台帳を閉じる
  factoryctl incident-record patrol                    # 日次の見回りを今の記録で 1 回試す（何も残さない）
  factoryctl incident-record weekly                    # 週次の見回りの最新の結果（所見・台帳・送り出し口・落ちた下書き）
  factoryctl incident-record outbox [--all]            # 送り出し口の FIR の下書き（検査と査読を通ったもの。--all で取り出し済みも）
  factoryctl incident-record outbox show <FIR-...>     # 下書きの YAML の全文（Engine の incidents/ に PR で入れる。公開はオーナーの承認）
  factoryctl incident-record outbox take <FIR-...>     # 送り出し口から取り出した印を付ける
  factoryctl incident-record checks                    # 専用のチェックの棚卸し（場所・関係する記録・見直しの期日。definitions/incident.yaml）

工場の中での一次調査（ADR 0048。窓口の依頼から。特定できなかった件は引き継ぎ資料 INV-YYYY-NNNN にして外の調査に回す）:
  factoryctl investigation list                       # 引き継ぎ資料の一覧（止めた理由・起票した Issue）
  factoryctl investigation show <INV-...>             # 引き継ぎ資料の本体（二次の調査はここから始める。依頼者の問いはそのまま入っている）
  factoryctl investigation bench [--only id,..] < eval-corpus/investigate/cases.yaml   # 評価セットを再生する（1 件 $3 まで。docker exec -i で標準入力に渡す）
  factoryctl investigation bench-status [<回>]        # 評価の回の進みと採点（外れた理由）。特定率が合格線に届くまで窓口の報告に「試行中」と添える

オンボーディングの判定の記録（ADR 0045 の追記。操業コンソールの製品のページ・受信トレイの準備のお願いと稼働 ready の承認が読む）:
  factoryctl readiness record <file.yaml|-> [--by ..]   # 製品の今の粒度と根拠・満たした条件・技術スタックと外部サービス・取り込み元を記録し、判定を出す（標準入力は docker exec -i で）
  factoryctl readiness show [<owner/repo>]              # 製品ごとのいちばん新しい判定（レベル・仕事ごとの判定・お願いの数）

事業部の窓口（ADR 0030）。依頼者のトークンと相談の一覧:
  factoryctl desk token create <名前> --role operator|requester [--product owner/repo,...]   # トークンはこのときだけ出る
  factoryctl desk token list                               # トークンの一覧（名前・役割・対象の製品。トークンそのものは出ない）
  factoryctl desk token revoke <名前>                       # トークンを取り消す
  factoryctl desk list                                     # 相談の一覧（新しい順）
  factoryctl desk show <相談の id>                          # 会話（発言・最新の下書き・作ったチケット）
  factoryctl desk reply <相談の id> <本文>                   # 運用主として返事・言い足しを書く（相談役が次の周回で動く）
  factoryctl desk confirm <相談の id> <下書きの id> [--level p0..p3]   # 下書きを確認して投入する（依頼者の確認 1 回。ADR 0030 の 3）
  factoryctl desk from-issue <owner/repo#番号>              # clarify で確定した Issue などを窓口の依頼として開き直す（相談役が下書きを出す）

モードと判断（ADR 0006）:
  factoryctl mode show                                          # いまのモード（並走モード attended・全自動モード unattended）と決め手
  factoryctl mode set attended|unattended|schedule [--until ISO]  # 期限まで（なければ次に切り替えるまで）時間帯と既定より優先。schedule で切り替えを外す
  factoryctl judgment list [--ungraded] [--limit N]             # 工場の判断の記録（--ungraded は人がまだ採点していないもの）
  factoryctl judgment recategorize <remap.yaml>                 # 類型の振り直し（definitions/judgment-remap-*.yaml。元の類型は category_v1 に残す）
  factoryctl judgment grade <artifact_id> agree|disagree [--note ..]   # 人の採点（教師データ）。類型ごとの一致率になる
  factoryctl report judgments [--from YYYY-MM-DD] [--to YYYY-MM-DD]   # 類型・モード別の件数、確信度、一致率、差し戻し、類型の見直しの候補

デザインの線（ADR 0025）。画面を含むタスク（factory:design）の案を人が選ぶ。操業コンソールの選択の画面（P4-7）と同じ操作:
  factoryctl design list                                   # 人の選択を待っているタスク（検査役の推す案と、人へ回した理由）
  factoryctl design show <owner/repo#123>                  # そのタスクの最新の UXSpec と検査役の判断（3 層）
  factoryctl design pick <owner/repo#123> <案> [--by ..] [--note ..]   # 案を選ぶ。dispatcher が次の周回で Issue に UXSpec を足して投入待ちに戻す
  docker compose exec -T dispatcher factoryctl design export <owner/repo#123> > design.tar   # 案のファイル（HTML・PNG）を tar で取り出す

プランナー（ADR 0025・0026）。判定の根拠を後で見返す:
  factoryctl plan list [--days 7]                          # 最近の判定（経路・当てた決まり・印・確信度・費用）
  factoryctl plan show <owner/repo#123>                    # そのチケットの判定の履歴（役ごとの理由、とらなかった経路、根拠・解釈、入力の版）
  factoryctl plan outcomes [--repo owner/repo] [--days 14] [--backfill]   # 判定と結果の突き合わせ（ADR 0036。--backfill は過去の PR も GitHub から引いて残す）
  factoryctl plan sizes [--repo owner/repo] [--days 60]    # 実績の表（層の組ごとのファイル数・ターン・途中終了。プランナーの見積もりの材料）
  factoryctl plan pass [--repo owner/repo]                 # 合格線（実績の 4 つ。直近 14 日）を満たしているか
  factoryctl plan resplit <owner/repo#123>                 # 途中終了を繰り返したチケットの残りを、プランナーに見積もり直させて切る（ADR 0036 の 6）
  factoryctl plan cap <owner/repo#123> [--add-usd 10]      # 費用の残り（上限・実績）を見る。--add-usd で上限に上乗せし、費用の上限で止めたチケットを再開する（ADR 0038 の追記）

知見の制限付きの保管先（ADR 0024 の 5）。利用者の情報をどうしても含む知見の根拠を専用スキーマ restricted に置き、git の知見からは restricted: R-NNNN で指す。
保管先の専用の利用者（factory_restricted）で繋ぐ。合言葉は管理者だけが持ち（.env に置かない）、使うときに RESTRICTED_DATABASE_URL で渡す。読むたびに誰が・何のために読んだかを残す:
  export RESTRICTED_DATABASE_URL=postgresql://factory_restricted:<合言葉>@postgres:5432/factory   # 管理者の手元でだけ渡す
  docker compose exec -T -e RESTRICTED_DATABASE_URL dispatcher factoryctl restricted add <owner/repo> [--knowledge K-0001,..] [--by ..] < 中身.md   # R-NNNN を出す
  docker compose exec -e RESTRICTED_DATABASE_URL dispatcher factoryctl restricted list [<owner/repo>]   # id・製品・指している知見・読んだ回数だけ（中身は出さない）
  docker compose exec -e RESTRICTED_DATABASE_URL dispatcher factoryctl restricted show <R-NNNN> --purpose "何のために" [--by ..]   # 中身を読む（誰が・何のために読んだかを残す）
  docker compose exec -e RESTRICTED_DATABASE_URL dispatcher factoryctl restricted retire <R-NNNN> [--reason ..] [--by ..]   # 中身を消し、id と記録は残す

Learn ループ（ADR 0013）:
  factoryctl report learning [--days 30]          # 教師信号の束（signature ごとの件数・チケット・段階の候補・eval の元）
  factoryctl report usage [--days 14] [--json]    # 使用量（トークンの消費と費用、日ごと・役割・モデル・契約の週、アカウントを足すかの判断の材料）。コンソールの /usage と同じ
  factoryctl report send <report.factory_health|report.product|all> [--date YYYY-MM-DD] [--to 宛先] [--html 出力先]
                                                  # 定期レポート（ADR 0032）を今のコードで組み立て、通知の設定の宛先に今すぐ送る（試し送り。既定は前日の分）。--html は送らずに報告書を書き出す
  factoryctl report process [--days 7]            # 工程の信号の束（ADR 0027 の 3。衝突の解消・やり直し・費用の外れ値・待ち・停止）。前の窓との比較つき
  factoryctl committee reopen <PR の URL>...      # 人に回した知見の PR を委員会にかけ直す（承認待ちのラベルを外す）
  factoryctl learn backfill-conflicts [--days 7]  # 衝突の解消のジョブのログ（オブジェクトストア）から、衝突したファイルの記録（conflict.files）を埋める
  docker exec factory-dispatcher-1 factoryctl export learning [--since YYYY-MM-DD] [--repo owner/repo] > learning.json
                                                  # 学習の観測データ（知見・委員会・ルールの成績と移り変わり・教師信号・流量）を JSON で書き出す。読むだけ。次の世代の工場で観測を続ける材料
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import psycopg
import yaml
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import choice
import desk
import design_line
import distribution
import drain
import ephemeral
import incident
import factory_incident
import desk_investigate
import investigate_bench
import line
import usage
import ghapp
import judgment
import learn
import learn_export
import ops
import plan_outcome
import planner
import process_learn
import rerun
import restricted
import serialize
import workflow_guard
import workflow_shadow
import ship
import stoppage
import touch
import train
import swap


from rerun import INCOMPLETE_CONTINUE_MAX, PIPED_KINDS, carried_input, incomplete_count  # noqa: E402,F401  積み直し（rerun.py。コンソールのボタンと共通）


def split_ref(ref: str) -> tuple[str, int]:
    repo, _, n = ref.partition("#")
    if not repo or not n.isdigit():
        raise ValueError("owner/repo#123 の形で渡す")
    return repo, int(n)


INCOMPLETE_CONTINUE_MAX = 1   # 途中終了の続きは 1 回まで（ADR 0036 の 6）


def incomplete_count(cur, corr: str) -> int:
    """そのチケットの実装役のジョブが途中終了した回数"""
    cur.execute("select count(*) as n from jobs where correlation_id=%s and kind='implement' and coalesce((result->>'incomplete')::boolean, false)", (corr,))
    return int(cur.fetchone()["n"])


def plan_resplit(cur, argv) -> int:
    """途中終了を繰り返したチケットの残りを、実装の手前の判定としてプランナーに切らせる（ADR 0036 の 6）"""
    repo, n = split_ref(argv[3])
    corr = f"github:{repo}#{n}"
    cur.execute("select result->>'pr_url' as pr from jobs where correlation_id=%s and kind='implement' and result->>'pr_url' is not null order by id desc limit 1", (corr,))
    pr = (cur.fetchone() or {}).get("pr")
    jid = planner.enqueue_pre_implement(cur, repo, n, corr, "incomplete_repeated", {"continuing_pr": pr} if pr else {})
    if not jid:
        print("an active job already exists for this issue"); return 1
    print(f"enqueued plan job {jid}（実装の手前の判定。残りを見積もり、大きければ切る）")
    return 0


def plan_outcome_cmd(cur, argv, sub: str) -> int:
    """判定と結果の突き合わせ・実績の表・合格線（ADR 0036 の 4）"""
    repo = opt(argv, "--repo", None)
    if sub == "outcomes":
        if "--backfill" in argv or "--rescore" in argv:
            import ghapi

            def gh(method, path, body=None):
                return ghapi.request(method, path, body, user_agent="factoryctl", token=ghapp.token())
            log = lambda *a: print(*a, flush=True)  # noqa: E731
            if "--rescore" in argv:   # 数え方を変えたあと、残した突き合わせを組み立て直す
                n = plan_outcome.rescore(cur, gh, log=log)
                print(f"{n} 本の突き合わせを組み立て直しました")
            else:
                n = plan_outcome.step(cur, gh, days=None, limit=None, log=log)
                print(f"{n} 本の PR を突き合わせました")
        for o in plan_outcome.outcomes(cur, repo, int(opt(argv, "--days", "14"))):
            rec = "-" if o.get("expected_recall") is None else o["expected_recall"]
            print(f"{o['at']:%m-%d %H:%M} {o.get('repo')}#{o.get('issue')} {o.get('judgment'):<10} {','.join(o.get('reasons') or []):<22} "
                  f"plan={'y' if o.get('planned') else 'n'} jobs={o.get('jobs')} turns={o.get('turns')} files={o.get('files')} "
                  f"layers={o.get('layers_key')} recall={rec} ${o.get('cost_usd') or 0}")
        return 0
    rows = plan_outcome.outcomes(cur, repo, int(opt(argv, "--days", "60" if sub == "sizes" else str(plan_outcome.PASS_LINES["window_days"]))))
    if sub == "sizes":
        print(f"{'製品':<20} {'層の組':<28} {'件数':>4} {'ファイル':>8} {'ターン':>6} {'p90':>5} {'途中終了':>8} {'費用':>7}")
        for t in plan_outcome.size_table(rows):
            print(f"{t['repo']:<20} {t['layers']:<28} {t['n']:>4} {t['files_median']:>8} {t['turns_median']:>6} {t['turns_p90']:>5} {t['incomplete']:>8} ${t['cost_median']:>6}")
        return 0
    st = plan_outcome.pass_status(rows)
    ja = {"incomplete_rate": "実装の途中終了の率", "turns_median": "1 チケットのターン（中央値）", "turns_p90": "1 チケットのターン（90 パーセンタイル）",
          "recall_median": "見込みのファイルの再現率（中央値）", "misrouted_rate": "経路の覆りの率"}
    print(f"直近 {plan_outcome.PASS_LINES['window_days']} 日: 実装のチケット {st['tickets']} 件（プランナーを通った {st['planned']} 件）"
          + ("" if st["measured"] else f"。{plan_outcome.PASS_LINES['min_tickets']} 件に満たないので参考の値"))
    for k, m in st["metrics"].items():
        line_ = f"{m['max']} 以下" if "max" in m else f"{m['min']} 以上"
        mark = "-" if m["ok"] is None else ("ok" if m["ok"] else "NG")
        print(f"  {mark:<3} {ja[k]}: {m['value']}（合格線 {line_}）")
    print("判定の内訳: " + "、".join(f"{k} {v}" for k, v in st["judgments"].items()))
    return 0 if st["ok"] or not st["measured"] else 1


def plan_cap(cur, argv) -> int:
    """費用の残りを見る。--add-usd で上限に上乗せし、費用の上限で止めたチケットを再開する（ADR 0038 の追記、2026-11-11）"""
    repo, n = split_ref(argv[3])
    corr = f"github:{repo}#{n}"
    add = opt(argv, "--add-usd")
    if add is not None:
        if float(add) <= 0:
            print("--add-usd は正の数"); return 2
        rnd = workflow_guard.release_cost(cur, corr, float(add), "factoryctl")
        for key in stoppage.open_keys(cur, f"cost:{corr}#"):
            stoppage.close_stoppage(cur, key, remedy=f"上限に ${add} を上乗せ", closed_by="factoryctl", quiet=True)
        cur.execute("select id from tickets where source='github' and source_ref=%s and held", (f"{repo}#{n}",))
        for t in cur.fetchall():
            ops.set_held(cur, t["id"], False, "factoryctl", f"費用の上限に ${add} を上乗せ")
        print(f"上乗せ {rnd} 回目: ${add}")
    for k, v in workflow_guard.cost_left(cur, corr).items():
        print(f"{workflow_shadow.CAP_JA.get(k, k)}: 上限 ${v['cap']:.2f}  実績 ${v['spent']:.2f}  残り ${v['left']:.2f}")
    return 0


def plan_cmd(cur, argv) -> int:
    sub = argv[2] if len(argv) > 2 else "list"
    if sub == "show" and len(argv) > 3:
        repo, n = split_ref(argv[3])
        rows = planner.plans(cur, f"github:{repo}#{n}")
        if not rows and not serialize.serialized(cur, f"github:{repo}#{n}") and not workflow_shadow.shadows(cur, f"github:{repo}#{n}"):
            print("no plan"); return 1
        for p in rows:
            print(f"## Plan {p['id']}（job {p['job_id']}、{p['created_at']:%Y-%m-%d %H:%M}、{p.get('round')} 回目）: {p.get('decision')}  {planner.path_ja(p.get('steps') or [])}")
            print(f"確信度 {p.get('confidence')}  大きさ {p.get('design_scale')}  決まり {p.get('guards') or []}  印 {p.get('advisories') or []}  費用 ${p.get('cost_usd') or 0}")
            print(f"要約: {p.get('summary') or ''}")
            for line in planner.rationale(p, p.get("steps") or []):
                print(line)
            print("根拠:")
            for e in p.get("evidence") or []:
                print(f"- {e}")
            print(f"解釈: {p.get('interpretation') or ''}")
            for q in p.get("questions") or []:
                print(f"問い: {q.get('question')} {q.get('options')}")
            if p.get("expected_files"):
                print("変わる見込みのファイル: " + ", ".join(p["expected_files"]))
            for line in planner.workflow_lines(p.get("workflow"), p.get("workflow_marks")):
                print(line)
            print(f"入力の版: {json.dumps(p.get('versions') or {}, ensure_ascii=False)}\n")
        for s in serialize.serialized(cur, f"github:{repo}#{n}"):
            print(f"並べた（{s['at']:%m-%d %H:%M}）: {s.get('why')}")
        for s in workflow_shadow.shadows(cur, f"github:{repo}#{n}"):
            what = (f"{workflow_shadow.CAP_JA.get(s.get('cap'), s.get('cap'))} の上限 {s.get('declared')} を超えた（実績 {s.get('actual')}。守らせていたら"
                    f"{workflow_shadow.ACTION_JA.get(s.get('action'), s.get('action'))}）") if s.get("kind") == "cap_hit" else \
                   (f"飛ばす宣言の {s.get('step')} が動いた（ジョブ {s.get('job_id')}）" if s.get("kind") == "would_skip" else
                    f"start: after（{' '.join(f'#{x}' for x in s.get('after') or [])}）の前に投入した")
            print(f"宣言との差（{s['at']:%m-%d %H:%M}、記録だけ）: {what}")
        cur.execute("select occurred_at, payload from events where name='workflow.enforced' and correlation_id=%s order by id", (f"github:{repo}#{n}",))
        ja = {"conflict_wait": "同じファイルに触るチケットを待たせた", "conflict_retry": "待ちの後に 1 回だけ解いた", "conflict_human": "もう解かず人に回した"}
        for r in cur.fetchall():
            e = r["payload"]
            if e.get("kind") == "cost_human":
                print(f"守らせた（{r['occurred_at']:%m-%d %H:%M}）: 費用の上限に当たり、チケットを止めて人に回した（{', '.join(workflow_shadow.CAP_JA.get(k, k) for k in e.get('hit') or [])}）")
                continue
            if e.get("kind") == "cost_released":
                print(f"上乗せ（{r['occurred_at']:%m-%d %H:%M}）: 費用の上限に ${e.get('add_usd')} を足して再開した（{e.get('by')}）")
                continue
            if e.get("kind") == "skipped":
                print(f"飛ばした（{r['occurred_at']:%m-%d %H:%M}）: {e.get('step')}（{e.get('why') or ''}。決まり: {e.get('rule')}）")
                continue
            print(f"守らせた（{r['occurred_at']:%m-%d %H:%M}）: 衝突の解消 {e.get('conflicts')} 回で上限 {e.get('cap')} に当たり、{ja.get(e.get('kind'), e.get('kind'))}"
                  + (f"（{' '.join(e.get('behind') or [])}）" if e.get("behind") else ""))
        return 0
    if sub in ("outcomes", "sizes", "pass"):
        return plan_outcome_cmd(cur, argv, sub)
    if sub == "resplit" and len(argv) > 3:
        return plan_resplit(cur, argv)
    if sub == "cap" and len(argv) > 3:
        return plan_cap(cur, argv)
    days = int(opt(argv, "--days", "7"))
    cur.execute("select correlation_id, payload, created_at from artifacts where type='Plan' and created_at > now() - make_interval(days => %s) "
                "order by id desc limit 100", (days,))
    for r in cur.fetchall():
        p = r["payload"]
        print(f"{r['created_at']:%m-%d %H:%M} {p.get('repo')}#{p.get('issue')} {p.get('decision'):<8} {planner.path_ja(p.get('steps') or [])}"
              f"  conf={p.get('confidence')} guards={p.get('guards') or []} marks={p.get('advisories') or []} ${p.get('cost_usd') or 0}")
    return 0


def design_cmd(cur, argv) -> int:
    sub = argv[2] if len(argv) > 2 else "list"
    if sub == "list":
        cur.execute("select w.occurred_at, w.payload from events w where w.name='ui_design.awaiting_choice' and not exists ("
                    "select 1 from events c where c.name='ui_design.chosen' and c.payload->>'design_artifact_id' = w.payload->>'design_artifact_id') "
                    "order by w.id")
        for r in cur.fetchall():
            p = r["payload"]
            why = "、".join(design_line.REASON_JA.get(x, x) for x in p.get("reasons") or [])
            print(f"{p['repo']}#{p['issue']} 案 {p.get('design_artifact_id')} 推す={p.get('recommended')} {r['occurred_at']:%m-%d %H:%M} {why}")
        return 0
    try:
        repo, issue = split_ref(argv[3] if len(argv) > 3 else "")
    except ValueError as e:
        print(e); return 2
    if sub == "pick":
        try:
            c = design_line.choose(cur, repo, issue, argv[4], opt(argv, "--by", "builder"), opt(argv, "--note", ""))
        except (ValueError, IndexError) as e:
            print(e); return 1
        aid = choice.find_open(cur, "design_variants", repo, str(issue))   # 操業コンソールの依頼も閉じる
        if aid:
            choice.choose(cur, aid, c["variant"], opt(argv, "--by", "builder"), opt(argv, "--note", ""))
        print(f"選んだ: {repo}#{issue} 案 {c['variant']}（検査役の推す案と{'一致' if c['agree'] else '不一致'}）。dispatcher が次の周回で Issue に反映する")
        return 0
    cur.execute("select id from artifacts where type='UXSpec' and payload->>'repo'=%s and (payload->>'issue')::int=%s order by id desc limit 1", (repo, issue))
    r = cur.fetchone()
    if not r:
        print("このタスクの案はない"); return 1
    d = design_line.load_design(cur, r["id"])
    if sub == "export":   # 標準出力に tar（docker compose exec -T で取り出す）
        import io
        import tarfile
        from minio import Minio
        e = os.environ
        st = Minio(e["OBJECTSTORE_ENDPOINT"], access_key=e["OBJECTSTORE_ACCESS_KEY"], secret_key=e["OBJECTSTORE_SECRET_KEY"], secure=False)
        with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as tar:
            for name, key in (d.get("files") or {}).items():
                obj = st.get_object(e.get("OBJECTSTORE_BUCKET", "factory-artifacts"), key)
                try:
                    data = obj.read()
                finally:
                    obj.close()
                    obj.release_conn()
                info = tarfile.TarInfo(f"{repo.replace('/', '_')}-{issue}-{d['id']}/{name}")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return 0
    import yaml
    print(yaml.safe_dump(d.get("uxspec") or {}, allow_unicode=True, sort_keys=False))
    cur.execute("select payload from artifacts where type='DesignVerdict' and (payload->>'design_artifact_id')::int=%s order by id desc limit 1", (d["id"],))
    v = (cur.fetchone() or {}).get("payload")
    if v:
        print(f"# 検査役: {v.get('decision')}（検査役の答え {v.get('inspector_decision')}、推す案 {v.get('chosen_variant')}、確信度 {(v.get('score') or {}).get('self_reported')}）")
        print(f"# 解釈: {v.get('interpretation')}")
        for x in v.get("evidence") or []:
            print(f"# 根拠: {x}")
        for f in v.get("findings") or []:
            print(f"# 指摘 [{f.get('severity')}] {f.get('viewpoint')} / {f.get('variant')}: {f.get('evidence')} → {f.get('fix')}")
    return 0


DRAIN_WAIT_DEFAULT_SEC = 1800


def opt(argv, name, default=None):
    return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else default


def report_kpi_cmd(cur, argv):
    """週次の KPI（ADR 0043 の O2）。--measure は shipper に測る依頼を書き、結果を待って supervisor と同じ組み立てを今すぐ回す"""
    import design
    import report_kpi
    repo = argv[3] if len(argv) > 3 and not argv[3].startswith("--") else (report_kpi_repos() or ["guccyon/rally"])[0]
    if "--measure" in argv:
        ok, msg = ship.request_kpi(cur, repo, opt(argv, "--env", report_kpi.ENV), design.week_of(), opt(argv, "--by", "builder"), "factoryctl")
        print(msg)
        if not ok:
            return 1
        cur.execute("select max(id) as id from events where name='release.kpi_requested'")
        rid, t0 = cur.fetchone()["id"], time.monotonic()
        while time.monotonic() - t0 < float(opt(argv, "--timeout", "300")):
            cur.execute("select payload from events where name='release.kpi_measured' and (payload->>'request_id')::bigint = %s", (rid,))
            got = cur.fetchone()
            if got:
                if not got["payload"].get("ok"):
                    print(f"測れなかった: {got['payload'].get('error')}")
                    return 1
                report_kpi.report_step(cur)
                break
            time.sleep(2)
        else:
            print("待ち切れなかった（shipper が動いているか factoryctl release status で見る。結果が出れば supervisor が Report にする）")
            return 1
    print("\n".join(report_kpi.lines(report_kpi.latest(cur, repo))))
    return 0


def report_kpi_repos() -> list[str]:
    return [r.strip() for r in os.environ.get("KPI_WEEKLY", os.environ.get("RELEASE_TRAIN", "")).split(",") if r.strip()]


def report_send(cur, argv):
    import report as rpt
    import report_page
    kinds = list(rpt.builders_for()) if len(argv) < 4 or argv[3] in ("all", "") or argv[3].startswith("--") else [argv[3]]
    unknown = [k for k in kinds if k not in rpt.builders_for()]
    if unknown:
        print(f"種類 {unknown[0]} はない（{', '.join(rpt.builders_for())}）")
        return 2
    date = opt(argv, "--date")
    if date:
        start = datetime.combine(datetime.strptime(date, "%Y-%m-%d").date(), datetime.min.time(), tzinfo=rpt.TZ)
        end = start + timedelta(days=1)
    else:
        start, end = rpt.previous_day(datetime.now(timezone.utc))
    if opt(argv, "--html"):
        for kind in kinds:
            rep = rpt.builders_for()[kind](cur, start, end)
            path = os.path.join(opt(argv, "--html"), report_page.filename(rep)) if os.path.isdir(opt(argv, "--html")) else opt(argv, "--html")
            with open(path, "w") as f:
                f.write(report_page.render_html(rep, "provider", os.environ.get("FACTORY_NAME", "")))
            print(f"{kind}: {path}")
            print(report_page.comment(rep, "provider", os.environ.get("FACTORY_NAME", "")))
        return 0
    code = 0
    for kind in kinds:
        r = rpt.send_now(cur, kind, start, end, opt(argv, "--to"))
        print(f"{kind}（{start:%Y-%m-%d}）: 送れた {', '.join(r['sent']) or 'なし'} / 送れなかった {', '.join(r['failed']) or 'なし'}")
        code = code or (1 if r["failed"] or not r["sent"] else 0)
    return code


def report_stoppages(cur, argv):
    today = datetime.now(timezone.utc).date()
    to = opt(argv, "--to", (today + timedelta(days=1)).isoformat())
    frm = opt(argv, "--from", (today - timedelta(days=6)).isoformat())
    # 期間に重なる停止（開いたままのものを含む）
    cur.execute("select *, coalesce(ended_at, now()) - started_at as dur from stoppages "
                "where started_at < %s::date and coalesce(ended_at, now()) >= %s::date order by started_at", (to, frm))
    rows = cur.fetchall()
    out = [f"# 操業停止の記録（{frm} 〜 {to}、{os.environ.get('REPORT_TZ', 'Asia/Tokyo')}、制御プレーンから生成）", "",
           "生成: `factoryctl report stoppages --from --to`。記録は events の stoppage.opened / closed（ADR 0005）", ""]
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(r["category"], []).append(r)
    out += ["## 内訳", "", "| 分類 | 件数 | 合計時間 | 開いたまま |", "|---|---|---|---|"]
    for cat in stoppage.CATEGORIES:
        rs = by.get(cat, [])
        if rs:
            mins = sum(int(r["dur"].total_seconds() // 60) for r in rs)
            out.append(f"| {cat} | {len(rs)} | {mins // 60} 時間 {mins % 60} 分 | {sum(1 for r in rs if r['ended_at'] is None)} |")
    # builder を起こしたもの（D8）。builder が閉じた停止のうち、人が起こした割合を自律の度合いとして見る
    cur.execute("select coalesce(payload->>'woken_by', '未記録') as w, count(*) as n from events where name='stoppage.closed' "
                "and payload->>'closed_by'='builder' and occurred_at >= %s::date and occurred_at < %s::date group by 1", (frm, to))
    woke = {r["w"]: r["n"] for r in cur.fetchall()}
    known = sum(n for w, n in woke.items() if w != "未記録")
    out += ["", "## builder を起こしたもの（D8）", "", "| 起こしたもの | 件数 |", "|---|---|"] + [f"| {w} | {n} |" for w, n in sorted(woke.items())]
    out += ["", f"- 人が起こした割合: {woke.get('human', 0)}/{known}" + (f"（{woke.get('human', 0) / known:.0%}）" if known else "（記録なし）")]
    out += ["", "## 一覧", "", "| key | 開始 | 終了 | 分類 | 検知 | 何が止まったか | 原因 | 手当て | 再発を防ぐ仕組み |", "|---|---|---|---|---|---|---|---|---|"]

    def cell(v):
        return (v or "").replace("|", "／").replace("\n", " ")
    for r in rows:
        end = f"{r['ended_at']:%m-%d %H:%M}" if r["ended_at"] else "**開いたまま**"
        out.append(f"| {r['key']} | {r['started_at']:%m-%d %H:%M} | {end} | {r['category']} | {r['detected_by']} | {cell(r['what'])} | "
                   f"{cell(r['cause'])} | {cell(r['remedy'])} | {cell(r['prevention'])} |")
    print("\n".join(out))


def window(argv):
    # 比較は REPORT_TZ（既定 Asia/Tokyo）の日付で行うので、既定の期間も同じ時刻帯の今日から作る。
    # UTC の今日だと、日本の朝（UTC の前日）に当日分が --to から外れていた
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo(os.environ.get("REPORT_TZ", "Asia/Tokyo"))).date()
    return opt(argv, "--from", (today - timedelta(days=6)).isoformat()), opt(argv, "--to", (today + timedelta(days=1)).isoformat())


def release_create(cur, argv):
    repo, version = argv[3], argv[4]
    prs = [u.strip() for u in opt(argv, "--prs").split(",") if u.strip()] if opt(argv, "--prs") else None
    rid, prs = ship.create_release(cur, repo, version, prs)
    if rid is None:
        print(f"{repo}@{version} already exists"); return 1
    print(f"created {rid} with {len(prs)} PRs")
    return 0


def env_pr_show(cur, repo: str | None) -> int:
    envs = [a for a in ephemeral.active(cur) if not repo or a["repo"] == repo]
    if not envs:
        print("立っている使い捨て環境はない")
    for a in envs:
        cur.execute("select count(*) as n from events where name='env.ephemeral_touched' and correlation_id=%s and occurred_at >= %s",
                    (ephemeral.corr(a["repo"], int(a["pr"])), a["at"]))
        print(touch.render({"release_id": f"{a['repo']}#{a['pr']}（{a['project']}・{str(a.get('head_sha') or '')[:8]}）", "env": f"枠 {a['slot']}",
                            "reachable_from_phone": any(not e["url"].startswith("http://127.0.0.1") for e in a.get("entries") or []),
                            "entries": a.get("entries"), "accounts": a.get("accounts"),
                            "note": f"立てた {a['at']:%m-%d %H:%M}・触った印 {cur.fetchone()['n']} 件"}))
        print()
    return 0


def env_pr_wait(cur, repo: str, pr: int, timeout: float) -> int:
    t0 = time.monotonic()
    cur.execute("select max(id) as id from events where name='env.ephemeral_up_requested' and correlation_id=%s", (ephemeral.corr(repo, pr),))
    req = cur.fetchone()["id"]
    while time.monotonic() - t0 < timeout:
        cur.execute("select name, payload from events where name = any(%s) and (payload->>'request_id')::bigint = %s",
                    (list(ephemeral.RESULTS), req))
        r = cur.fetchone()
        if r and r["name"] == "env.ephemeral_failed":
            print(f"失敗: {r['payload'].get('error')}")
            return 1
        if r:
            print(f"立った（{r['payload'].get('seconds')} 秒）")
            return env_pr_show(cur, repo)
        time.sleep(3)
    print(f"{int(timeout)} 秒待っても立たなかった（shipper が動いているか見る）")
    return 1


def release_trains(cur, limit: int) -> int:
    rows = train.recent(cur, limit)
    if not rows:
        print("まだ便がない")
    for t in rows:
        a = t.get("approval") or {}
        mark = ("承認は要らない（本番を立てない段階）" if not a.get("required") else
                f"承認済み（{t['approved'].get('by')}）" if t.get("approved") else "承認待ち " + "・".join(a.get("pending") or []))
        print(f"{t['at']:%m-%d %H:%M} {t['train_id']}  {'緊急' if t['kind'] == 'emergency' else '定期'}  {mark}" +
              (f"  理由: {t['reason']}" if t.get("reason") else ""))
        for r in t.get("releases") or []:
            print(f"    {r['release_id']}（PR {r['prs']} 件）")
    return 0


def release_status(cur, repo):
    st = ship.status(cur, repo)
    for env, v in sorted(st["environments"].items()):
        print(f"{env:<8} いま {v['release_id']}  前 {v['previous'] or '-'}")
    cur.execute("select id, occurred_at, name, payload from events where name like 'release.%%' and correlation_id like %s "
                "and name not in ('release.included') order by id desc limit 20", (f"release:{repo}@%",))
    for r in reversed(cur.fetchall()):
        p = r["payload"]
        extra = " ".join(f"{k}={p[k]}" for k in ("env", "seconds", "ok", "from", "by", "via", "error") if k in p)
        print(f"{r['id']:>6} {r['occurred_at']:%m-%d %H:%M:%S} {r['name']:<28} {p.get('release_id', '')} {extra[:160]}")
    return 0


def report_provenance(cur, argv):
    frm, to = window(argv)
    cur.execute("""select a.payload->>'pr_url' as pr_url, a.payload->'provenance' as prov,
                          (select r.payload->>'release_id' from events r where r.name='release.included' and r.payload->>'pr_url'=a.payload->>'pr_url' limit 1) as release_id,
                          (select count(*) from artifacts j where j.type='Judgment' and j.payload->>'pr_url'=a.payload->>'pr_url') as judgments
                     from artifacts a where a.type='PR' and a.created_at >= %s::date and a.created_at < %s::date""", (frm, to))
    rows = cur.fetchall()
    n = len(rows)

    def rate(k):
        return f"{k}/{n}（{k / n:.0%}）" if n else "0/0"
    with_prov = sum(1 for r in rows if r["prov"])
    mode_known = sum(1 for r in rows if (r["prov"] or {}).get("mode") not in (None, "unknown"))
    released = sum(1 for r in rows if r["release_id"])
    out = [f"# 来歴の紐付け（{frm} 〜 {to}、制御プレーンから生成）", "", "生成: `factoryctl report provenance --from --to`（ADR 0007）。属性はすべて optional なので、統計には必ずこの紐付け率を併記する", "",
           "| 項目 | 紐付け率 |", "|---|---|", f"| 工場の PR | {n} |", f"| 製造の来歴あり | {rate(with_prov)} |", f"| モードが分かる | {rate(mode_known)} |",
           f"| Release に入った | {rate(released)} |", f"| 仮決めを含む | {rate(sum(1 for r in rows if r['judgments']))} |"]
    # 出荷の来歴（ADR 0012）: Release ごとの作り・環境・承認・戻し
    cur.execute("""select payload->>'release_id' as rid,
                          (select count(*) from events i where i.name='release.included' and i.payload->>'release_id'=c.payload->>'release_id') as prs,
                          (select max((b.payload->>'seconds')::numeric) from events b where b.name='release.built' and b.payload->>'release_id'=c.payload->>'release_id') as built_s,
                          (select string_agg(distinct d.payload->>'env', '・') from events d where d.name='release.deployed' and d.payload->>'release_id'=c.payload->>'release_id') as envs,
                          (select string_agg(distinct a.payload->>'via', '・') from events a where a.name='release.approved' and a.payload->>'release_id'=c.payload->>'release_id') as approved_via,
                          (select max((r.payload->>'seconds')::numeric) from events r where r.name='release.rolled_back' and r.payload->>'from'=c.payload->>'release_id') as rollback_s,
                          (select count(*) from events b, jsonb_array_elements_text(coalesce(b.payload->'contract_migrations','[]'::jsonb)) x
                            where b.name='release.built' and b.payload->>'release_id'=c.payload->>'release_id') as contracts
                     from events c where c.name='release.created' and c.occurred_at >= %s::date and c.occurred_at < %s::date order by c.id""", (frm, to))
    rels = cur.fetchall()
    if rels:
        out += ["", "## 出荷（ADR 0012）", "", "| Release | PR | イメージ作り（秒） | 出した環境 | 承認の入口 | この版から戻した秒数 | contract |", "|---|---|---|---|---|---|---|"]
        out += [f"| {r['rid']} | {r['prs']} | {r['built_s'] or '-'} | {r['envs'] or '-'} | {r['approved_via'] or '-'} | {r['rollback_s'] or '-'} | {r['contracts']} |" for r in rels]
    print("\n".join(out))


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 1.0)
    p, d = k / n, 1 + z * z / n
    c, h = (p + z * z / (2 * n)) / d, z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (max(0.0, c - h), min(1.0, c + h))


def false_positive_lines(jd):
    """検査器の指摘 1 件ごとの judge の裁き（inspection 類型）から偽陽性率を出す（health.yaml の false_positive_rate）。
    judge の推定: 不採用 / （採用＋不採用）。人へ回したものは分母から除く。
    人の確認: 人が採点した指摘のうち、偽陽性だったもの（judge の不採用に人が同意、または judge の採用に人が不同意）"""
    adopt = sum(1 for r in jd if r["v"] == "adopt")
    reject = sum(1 for r in jd if r["v"] == "reject")
    n = adopt + reject
    lines = ["| 指標 | 値 | 件数 | Wilson 95% |", "|---|---|---|---|"]
    if n:
        lo, hi = wilson(reject, n)
        lines.append(f"| 偽陽性率（judge の推定） | {reject / n:.0%} | {reject}/{n} | {lo:.2f}〜{hi:.2f} |")
    else:
        lines.append("| 偽陽性率（judge の推定） | — | 0/0 | — |")
    graded = [r for r in jd if r["g"] in ("agree", "disagree") and r["v"] in ("adopt", "reject")]
    fp = sum(1 for r in graded if (r["v"] == "reject") == (r["g"] == "agree"))
    if graded:
        lo, hi = wilson(fp, len(graded))
        lines.append(f"| 偽陽性率（人の確認） | {fp / len(graded):.0%} | {fp}/{len(graded)} | {lo:.2f}〜{hi:.2f} |")
    else:
        lines.append("| 偽陽性率（人の確認） | — | 0/0（採点待ち） | — |")
    lines += ["", f"- 人へ回した指摘 {sum(1 for r in jd if r['v'] == 'escalate')} 件は分母から除く。見逃し（miss_rate）はまだ測っていない"]
    return lines


def report_judgments(cur, argv):
    frm, to = window(argv)
    cur.execute("""select payload->>'category' as c, payload->>'stage' as st, payload->>'mode' as m, payload->>'verdict' as v,
                          (payload->'score'->>'self_reported')::float as conf, payload->'human_grade'->>'grade' as g
                     from artifacts where type='Judgment' and created_at >= %s::date and created_at < %s::date""", (frm, to))
    rows = cur.fetchall()
    out = [f"# 判断の記録（{frm} 〜 {to}、制御プレーンから生成）", "", "生成: `factoryctl report judgments --from --to`（ADR 0006）。類型は definitions/judgments.yaml " + judgment.TAXONOMY_VERSION, ""]
    out += ["## 類型 × 段階", "", "| 類型 | 仮決め | judge | うち差し替え・不採用 | 人へ | 確信度の平均 | 採点済み | 一致率 |", "|---|---|---|---|---|---|---|---|"]
    cats = sorted({r["c"] for r in rows})
    for c in cats:
        rs = [r for r in rows if r["c"] == c]
        prov = [r for r in rs if r["st"] == "provisional"]
        jd = [r for r in rs if r["st"] == "judged"]
        confs = [r["conf"] for r in jd if r["conf"] is not None]
        cur.execute("select count(*) as n, avg(case when e.payload->>'grade'='agree' then 1.0 else 0.0 end) as a from events e "
                    "join artifacts a on a.id = (e.payload->>'artifact_id')::bigint "
                    "where e.name='judgment.graded' and a.payload->>'category'=%s and e.occurred_at >= %s::date and e.occurred_at < %s::date", (c, frm, to))
        g = cur.fetchone()
        avg = f"{sum(confs) / len(confs):.2f}" if confs else "—"
        agree = f"{float(g['a']):.0%}" if g["n"] else "—"
        changed = sum(1 for r in jd if r["v"] in ("replace", "reject"))
        escal = sum(1 for r in jd if r["v"] == "escalate")
        out.append(f"| {c} | {len(prov)} | {len(jd)} | {changed} | {escal} | {avg} | {g['n']} | {agree} |")
    cur.execute("select count(*) as n from artifacts where type='Judgment' and payload->>'stage'='provisional' and (payload->>'ticket_interpretation')::boolean "
                "and created_at >= %s::date and created_at < %s::date", (frm, to))
    out += ["", f"- うちチケットの受け入れ条件の解釈で決めた仮決め（印 ticket_interpretation）: {cur.fetchone()['n']} 件。多ければチケットの書き方を直す"]
    out += ["", "## 検査の偽陽性率", ""] + false_positive_lines([r for r in rows if r["c"] == "inspection" and r["st"] == "judged"])
    out += ["", "## モード別（judge）", "", "| モード | 件数 | 人へ |", "|---|---|---|"]
    for m in sorted({r["m"] for r in rows if r["st"] == "judged"}):
        rs = [r for r in rows if r["st"] == "judged" and r["m"] == m]
        out.append(f"| {m} | {len(rs)} | {sum(1 for r in rs if r['v'] == 'escalate')} |")
    cur.execute("select name, count(*) as n from events where name like 'sendback.%%' and occurred_at >= %s::date and occurred_at < %s::date group by 1", (frm, to))
    sb = {r["name"]: r["n"] for r in cur.fetchall()}
    cur.execute("select count(*) as n from events where name='sendback.filed'")
    total_sb = cur.fetchone()["n"]
    out += ["", "## 差し戻し", "", f"- 起票 {sb.get('sendback.filed', 0)} 件、上限で流さず {sb.get('sendback.skipped', 0)} 件、再検査で指摘が残った {sb.get('sendback.residual', 0)} 件",
            f"- 累計の起票 {total_sb} 件（20 件で「1 PR 1 回」の上限を見直す）"]
    # 類型の見直しの候補（データ量で起こす。分け直しは人）
    # 仮決め全体に占める割合で見る（definitions/judgments.yaml の split_candidate。分け直しは全件の振り直しで、人が決める）
    cur.execute("select payload->>'category' as c, count(*) as n from artifacts where type='Judgment' and payload->>'stage'='provisional' group by 1")
    counts = {r["c"]: r["n"] for r in cur.fetchall()}
    total = sum(counts.values())
    hints = [f"- {c}: {n} 件（仮決めの {n / total:.0%}）。全件の振り直しの候補" for c, n in sorted(counts.items()) if total and n / total > 0.4]
    out += ["", "## 類型の見直しの候補", ""] + (hints or ["- なし"])
    print("\n".join(out))


def find_ticket(cur, ref: str):
    if ref.isdigit():
        cur.execute("select * from tickets where id=%s", (int(ref),))
    else:
        cur.execute("select * from tickets where source_ref=%s order by id limit 1", (ref,))
    return cur.fetchone()


def drain_report(cur) -> int:
    st = drain.status(cur)
    if not st["draining"]:
        print("一時停止していない（ラインは流れている）")
        return 1
    print(f"一時停止中（{st['since']} から、{st['by']}: {st['reason'] or '理由なし'}）")
    for j in st["running"]:
        print(f"  実行中のジョブ {j['id']} {j['kind']} {j['repo']}#{j['issue_number']}（{j['elapsed_sec'] // 60} 分）")
    for e in st["shipping"]:
        print(f"  処理中の出荷 {e['name']} {e['release_id']}")
    for j in st.get("queued") or []:
        print(f"  一時停止の間も流れる順番待ち {j['id']} {j['kind']} {j['repo']}#{j['issue_number']}")
    if st["allowed"]:
        print(f"  一時停止のまま流すもの: {' '.join(st['allowed'])}")
    if st.get("allowed_min_priority") is not None:
        print(f"  一時停止のまま流す優先度: {ops.level_of(st['allowed_min_priority'])} 以上のチケット")
    print("更新してよい" if st["ready"] else "まだ更新しない（上の終わりを待つ）")
    return 0 if st["ready"] else 3


def backfill_conflicts(cur, days: int) -> int:
    """衝突の解消のジョブ（continue.enqueued の reason=conflict）のうち、衝突したファイルの記録がないものを、ジョブのログから埋める"""
    from minio import Minio
    e = os.environ
    st = Minio(e["OBJECTSTORE_ENDPOINT"], access_key=e["OBJECTSTORE_ACCESS_KEY"], secret_key=e["OBJECTSTORE_SECRET_KEY"], secure=False)
    cur.execute("""select c.job_id, c.correlation_id as corr, f.payload->>'blob_key' as k from events c
                     join events f on f.name='job.finished' and f.job_id=c.job_id
                    where c.name='continue.enqueued' and c.payload->>'reason'='conflict' and c.occurred_at >= now() - make_interval(days => %s)
                      and not exists (select 1 from events x where x.name='conflict.files' and x.job_id=c.job_id) order by c.id""", (days,))
    filled = missing = 0
    for r in cur.fetchall():
        if not r["k"]:
            missing += 1
            continue
        try:
            obj = st.get_object(e.get("OBJECTSTORE_BUCKET", "factory-artifacts"), r["k"])
            try:
                log = obj.read().decode("utf-8", "replace")
            finally:
                obj.close()
                obj.release_conn()
        except Exception:  # noqa: BLE001  ログが消えたジョブは飛ばす
            missing += 1
            continue
        files = process_learn.parse_conflict_files(log)
        if files is None:
            missing += 1
            continue
        filled += process_learn.record_conflict_files(cur, r["job_id"], r["corr"], files, source="backfill")
    print(f"conflict.files: filled {filled}, no log or no conflict line {missing}")
    return 0


OPERATOR = {"name": "factoryctl", "role": "operator", "products": []}   # factoryctl は運用主として窓口を使う


def desk_cmd(cur, argv):
    sub = argv[2:4]
    try:
        if sub[:2] == ["token", "create"] and len(argv) > 4:
            products = [p for p in (opt(argv, "--product", "") or "").split(",") if p]
            token = desk.create_token(cur, argv[4], opt(argv, "--role", "requester"), products, opt(argv, "--by", "factoryctl"))
            print(token)
            print("このトークンは今しか出ません。依頼者の手元（MCP の設定など）に渡してください", file=sys.stderr)
            return 0
        if sub[:2] == ["token", "list"]:
            for t in desk.list_tokens(cur):
                print(f"{t['name']:<20} {t['role']:<9} {','.join(t['products']) or '（すべて）'} {t['created_at']:%m-%d %H:%M}"
                      f"{' 取り消し ' + format(t['revoked_at'], '%m-%d %H:%M') if t['revoked_at'] else ''}")
            return 0
        if sub[:2] == ["token", "revoke"] and len(argv) > 4:
            print("取り消しました" if desk.revoke_token(cur, argv[4], opt(argv, "--by", "factoryctl")) else "有効なトークンがない")
            return 0
        if sub[:1] == ["list"]:
            for c in desk.list_conversations(cur, OPERATOR):
                print(f"{c['id']:>4} {c['kind']:<8} {c['state']:<17} {c['product']} {c['opened_by']} r{c['rounds']} ${c['cost_usd']:.2f} {c['title'][:50]}")
            return 0
        if sub[:1] == ["show"] and len(argv) > 3:
            out = desk.read_conversation(cur, OPERATOR, int(argv[3]))
            c = out["conversation"]
            print(f"相談 {c['id']}（{c['kind']}・{c['state']}・{c['product']}・{c['opened_by']}・往復 {c['rounds']}・${c['cost_usd']:.2f}）")
            for m in out["messages"]:
                print(f"\n--- {m['at'][:16]} {m['event']} {m.get('role') or ''} {m.get('kind') or ''}\n{m.get('body') or ''}".rstrip())
            for t in out["tickets"]:
                print(f"チケット {t['id']} {t['source_ref']} {t['state']} {t['level']} {t['title'][:50]}")
            return 0
        if sub[:1] == ["reply"] and len(argv) > 4:
            desk.post_message(cur, OPERATOR, int(argv[3]), " ".join(argv[4:]))
            print("書きました。相談役が次の周回で動きます")
            return 0
        if sub[:1] == ["confirm"] and len(argv) > 4:
            desk.confirm_draft(cur, OPERATOR, int(argv[3]), int(argv[4]), level=opt(argv, "--level"))
            print("確認しました。dispatcher が次の周回でチケットを起票します（factoryctl desk show で確かめる）")
            return 0
        if sub[:1] == ["from-issue"] and len(argv) > 3:
            m = re.match(r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)$", argv[3])
            if not m:
                print("owner/repo#番号 で渡す")
                return 2
            import desk_line
            import ghapi

            def gh(method, path, body=None):
                return ghapi.request(method, path, body, user_agent="factoryctl", token=ghapp.token())
            c = desk_line.open_from_issue(cur, gh, OPERATOR, m[1], int(m[2]))
            print(f"相談 {c['id']} として開きました。相談役が次の周回で問いか下書きを出します（factoryctl desk show {c['id']}）")
            return 0
    except desk.DeskError as e:
        print(e)
        return 2
    print(__doc__)
    return 2


def swap_cmd(cur, argv) -> int:
    sub = argv[2] if len(argv) > 2 else "status"
    if sub == "run" and len(argv) > 3:
        try:
            sid = swap.request(cur, argv[3], opt(argv, "--by", "builder"), kind=opt(argv, "--kind", "code"), note=opt(argv, "--note", ""))
        except swap.SwapError as e:
            print(e)
            return 1
        print(f"更新 {sid} を受けた（{argv[3]}）。dispatcher が {swap.ENV.get('SWAP_INTERVAL_SEC', '15')} 秒ほどで更新役を起動する")
        print(f"進み具合: factoryctl swap status {sid}（dispatcher を上げ直す間は答えないことがある）。ホストでは docker logs -f {swap.container(sid)}")
        return 0
    if sub == "cancel" and len(argv) > 3 and argv[3].isdigit():
        ok = swap.cancel(cur, int(argv[3]), opt(argv, "--by", "builder"), opt(argv, "--reason", ""))
        print("取り消した" if ok else "取り消せない（更新役がもう動いているか、終わっている）")
        return 0 if ok else 1
    if sub == "status" and len(argv) > 3 and argv[3].isdigit():
        s = swap.get(cur, int(argv[3]))
        if not s:
            print("no such swap"); return 1
        print(swap.summary(s))
        print("\n".join(swap.event_lines(s)))
        return 0 if s["status"] == "completed" else 1
    if sub == "status":
        rows = swap.recent(cur)
        print("\n".join(swap.summary(s) for s in rows) if rows else "更新の記録はまだない")
        return 0
    print(__doc__)
    return 2


def restricted_conn():
    """保管先の専用の利用者で繋ぐ。合言葉は管理者が使うときに RESTRICTED_DATABASE_URL で渡す"""
    url = os.environ.get("RESTRICTED_DATABASE_URL", "")
    if not url:
        raise restricted.RestrictedError("RESTRICTED_DATABASE_URL がない（保管先の利用者 factory_restricted の接続。docker compose exec -e RESTRICTED_DATABASE_URL で渡す）")
    try:
        return psycopg.connect(url, row_factory=dict_row, autocommit=True)
    except psycopg.Error as e:
        raise restricted.RestrictedError(f"保管先に繋げない（{str(e).splitlines()[0] if str(e) else e.__class__.__name__}）") from e


def restricted_cmd(cur, argv):
    sub = argv[2] if len(argv) > 2 else "list"
    by = opt(argv, "--by", os.environ.get("FACTORY_ADMIN", "builder"))
    if sub not in ("add", "list", "show", "retire") or (sub != "list" and len(argv) < 4):
        print(__doc__)
        return 2
    try:
        with restricted_conn() as rconn:
            rcur = rconn.cursor()
            rcur.execute("select set_config('timezone', %s, false)", (os.environ.get("REPORT_TZ", "Asia/Tokyo"),))
            if sub == "add":
                ids = [k for k in (opt(argv, "--knowledge", "") or "").split(",") if k]
                print(restricted.add(rcur, cur, argv[3], sys.stdin.read(), ids, by))
                print("git の知見からは evidence に「- restricted: <この id>」と書いて指す（中身は書かない）", file=sys.stderr)
                return 0
            if sub == "list":
                product = argv[3] if len(argv) > 3 and not argv[3].startswith("--") else None
                for r in restricted.list_records(rcur, product):
                    state = f"retired {r['retired_at']:%m-%d}" if r["retired_at"] else "active"
                    last = f" 最後 {r['last_read_at']:%m-%d %H:%M}" if r["last_read_at"] else ""
                    print(f"{r['rid']:<8} {state:<14} {r['product']} {','.join(r['knowledge_ids']) or '-'} 作成 {r['created_by']} {r['created_at']:%m-%d %H:%M} 読んだ {r['reads']} 回{last}")
                return 0
            if sub == "show":
                print(restricted.read(rcur, cur, argv[3], by, opt(argv, "--purpose", "")))
                return 0
            ok = restricted.retire(rcur, cur, argv[3], by, opt(argv, "--reason", ""))
            print("retired（中身を消しました）" if ok else "active な記録がない")
            return 0 if ok else 1
    except restricted.RestrictedError as e:
        print(e)
        return 2


def hold_report(cur) -> None:
    st = incident.hold_status(cur)
    if not st:
        print("QA 役の差し止めはない")
        return
    print(f"QA 役がラインを差し止め中（{st['since']} から {st['minutes']} 分・{st['reason']}）。解除は人: factoryctl line release --reason ...")
    for i in st["incidents"]:
        print(f"  {i.get('severity', '').upper()} {i.get('incident_id')}{'（演習）' if i.get('drill') else ''}: {i.get('title')} {i.get('issue') or ''}")
    if st["allowed"]:
        print(f"  差し止めの間も流すもの: {' '.join(st['allowed'])} と p0 のチケット")


def investigation_cmd(cur, argv) -> int:
    """factoryctl investigation list|show（ADR 0048、dispatcher/desk_investigate.py）"""
    sub = argv[2] if len(argv) > 2 else "list"
    if sub == "list":
        hs = desk_investigate.handoffs(cur)
        if not hs:
            print("引き継ぎ資料はない"); return 0
        for h in hs:
            print(f"{h['id']} {h['at'][:16]} 会話 {h['conversation_id']} 止めた理由 {h.get('stop_reason')} {h.get('issue') or '（未起票）'}")
        return 0
    if sub == "show" and len(argv) > 3:
        h = desk_investigate.get_handoff(cur, argv[3])
        if not h:
            print(f"{argv[3]} はない"); return 1
        print(json.dumps(h, ensure_ascii=False, indent=2, default=str)); return 0
    if sub == "bench":
        try:
            cases = investigate_bench.load_cases(sys.stdin.read())
            only = [x for x in (opt(argv, "--only", "") or "").split(",") if x]
            got = investigate_bench.enqueue(cur, cases, only=only)
        except ValueError as e:
            print(f"積めなかった: {e}"); return 1
        print(f"評価の回 {got['run']} に {len(got['jobs'])} 件を積んだ（governance の枠で順に動く。費用の上限は 1 件 $3、合わせて ${3 * len(got['jobs'])} まで）")
        for k, v in got["jobs"].items():
            print(f"  {k}: job {v}")
        return 0
    if sub == "bench-status":
        st = investigate_bench.status(cur, argv[3] if len(argv) > 3 else None)
        if not st["run"]:
            print("評価の回はまだない"); return 0
        rate = "—" if st["rate"] is None else f"{st['rate']:.0%}"
        print(f"回 {st['run']}: {st['done']}/{st['of']} 件済み、合格 {st['passed']} 件、特定率 {rate}（合格線 {investigate_bench.PASS_RATE:.0%}）、費用 ${st['cost_usd']}")
        for c in st["cases"]:
            mark = "…" if c.get("pass") is None else ("○" if c["pass"] else "×")
            print(f"  {mark} {c['case']}" + (f": {'／'.join(c.get('reasons') or [])}" if c.get("pass") is False else ""))
        return 0
    print("factoryctl investigation list|show <INV-...>|bench|bench-status"); return 2


def incident_record_cmd(cur, argv) -> int:
    """factoryctl incident-record list|show|open|note|close|patrol|weekly|outbox|checks（ADR 0046、dispatcher/factory_incident.py）"""
    sub = argv[2] if len(argv) > 2 else "list"
    arg = argv[3] if len(argv) > 3 and not argv[3].startswith("--") else None
    by = f"factoryctl:{opt(argv, '--by', 'human')}"
    try:
        if sub == "list":
            cases = factory_incident.list_cases(cur, "all" if "--all" in argv else "open")
            if not cases:
                print("台帳に開いているものはない"); return 0
            for c in cases:
                print(f"{c['id']} [{c['status']}] {c['severity']} {c['resource']}{'×' + c['mechanism'] if c.get('mechanism') else ''}: {c['title']}")
            return 0
        if sub == "show" and arg:
            c = factory_incident.get_case(cur, arg)
            if not c:
                print(f"{arg} は台帳にない"); return 1
            print(json.dumps(c, ensure_ascii=False, indent=2, default=str)); return 0
        if sub == "open":
            title, resource, severity = opt(argv, "--title"), opt(argv, "--resource"), opt(argv, "--severity")
            if not (title and resource and severity):
                print("--title・--resource・--severity が要る"); return 2
            cid = factory_incident.open_case(cur, by, title, resource, severity, mechanism=opt(argv, "--mechanism"), note=opt(argv, "--note", "") or "")
            print(f"台帳に開いた: {cid}"); return 0
        if sub == "note" and arg:
            ok = factory_incident.update_case(cur, arg, by, mechanism=opt(argv, "--mechanism"), severity=opt(argv, "--severity"),
                                              engine_issue=opt(argv, "--engine-issue"), fir=opt(argv, "--fir"), note=opt(argv, "--note"))
            print("足した" if ok else f"{arg} は台帳にない"); return 0 if ok else 1
        if sub == "close" and arg:
            reason = opt(argv, "--reason")
            if not reason:
                print("--reason が要る"); return 2
            ok = factory_incident.close_case(cur, arg, by, reason)
            print("閉じた" if ok else f"{arg} は台帳にないか、もう閉じている"); return 0 if ok else 1
        if sub == "patrol":
            snap = factory_incident.patrol(cur)
            print(json.dumps(snap, ensure_ascii=False, indent=2, default=str)); return 0
        if sub == "weekly":
            cur.execute("select payload from events where name='fincident.reviewed' order by id desc limit 1")
            r = cur.fetchone()
            print(json.dumps(r["payload"], ensure_ascii=False, indent=2) if r else "週次の見回りの結果はまだない"); return 0
        if sub == "checks":
            items = factory_incident.load_defs().get("dedicated_checks") or []
            if not items:
                print("専用のチェックは載っていない"); return 0
            today = datetime.now(factory_incident.JST).date()
            for d in items:
                rb = str(d.get("review_by"))
                print(f"{rb}{' [期日を過ぎた]' if rb <= today.isoformat() else ''} {d.get('name')}（{', '.join(d.get('where') or [])}、記録 {d.get('fir') or 'なし'}）")
            return 0
        if sub == "outbox":
            act = arg or "list"
            fid = argv[4] if len(argv) > 4 and not argv[4].startswith("--") else None
            if act == "list":
                items = factory_incident.outbox(cur, "all" if "--all" in argv else "pending")
                if not items:
                    print("送り出し口に下書きはない"); return 0
                for x in items:
                    print(f"{x['id']}{' [取り出し済み]' if x['taken'] else ''} {x['severity']} {x['resource']}×{x['mechanism']}: {x['title']}（{x['week']}）")
                return 0
            if act in ("show", "take") and fid:
                x = next((x for x in factory_incident.outbox(cur, "all") if x["id"] == fid), None)
                if not x:
                    print(f"{fid} は送り出し口にない"); return 1
                if act == "show":
                    print(x["yaml"], end=""); return 0
                print("取り出した印を付けた" if factory_incident.take(cur, fid, by) else "もう取り出してある"); return 0
    except ValueError as e:
        print(str(e)); return 2
    print("factoryctl incident-record list|show|open|note|close|patrol|weekly|outbox|checks ..."); return 2


def incident_cmd(cur, argv) -> int:
    """factoryctl incident list|budget|close|drill|drill-end（ADR 0043 の O5）"""
    sub = argv[2] if len(argv) > 2 else "list"
    arg = argv[3] if len(argv) > 3 and not argv[3].startswith("--") else None
    by = opt(argv, "--by", "factoryctl")
    if sub == "list":
        for r in incident.recent(cur, arg, int(opt(argv, "--limit", "20"))):
            print(f"{r['at']:%m-%d %H:%M} {str(r.get('severity')).upper()} {r['incident_id']}{'（演習）' if r.get('drill') else ''}"
                  f"{'（閉じた）' if r.get('closed') else ''}: {r.get('title')} {r.get('issue') or ''}")
        hold_report(cur)
        return 0
    if sub == "close" and arg:
        reason = opt(argv, "--reason", "")
        if not reason:
            print("--reason が要る"); return 2
        ok = incident.close_incident(cur, arg, by, reason)
        print("閉じた" if ok else "開いている Incident ではない")
        return 0 if ok else 1
    if not arg or "/" not in arg:
        print("factoryctl incident list|budget|close|drill|drill-end ..."); return 2
    repo = arg
    import ghapi

    def gh(method, path, body=None):
        return ghapi.request(method, path, body, user_agent="factoryctl", token=ghapp.token())
    if sub == "budget":
        b = incident.budget(cur, repo, incident.fetch_policy(gh, repo))
        if not b:
            print(f"{repo} の quality_policy.yaml に error_budget がない"); return 1
        print(f"{repo} のエラーバジェット（{b['period_days']} 日・{b['since'][:10]} から・演習を除く{'・仮置きの値' if b['provisional'] else ''}）")
        for s_, v in b["severities"].items():
            mark = " ← 超えた" if s_ in b["over"] else ""
            print(f"  {s_.upper()}: {v['count']} 件 / 許容 {v['allowed'] if v['allowed'] is not None else '-'} 件{mark}")
        return 1 if b["over"] else 0
    if sub == "drill":
        plant = opt(argv, "--plant", "s1_data_leak")
        if incident.drill_active(cur, repo):
            print("この製品は演習の最中（factoryctl incident drill-end で終える）"); return 1
        cur.execute("select id, state from jobs where kind='qa' and repo=%s and state in ('queued','running') order by id limit 1", (repo,))
        busy = cur.fetchone()
        if busy:   # QA のジョブは製品ごとに 1 本。仕込んだのに探索を積めない、を避けるため先に確かめる
            print(f"この製品の QA のジョブ {busy['id']} が {busy['state']}。終わってから演習を始める（仕込みはしていない）"); return 1
        ok, msg = ship.request_drill(cur, repo, "staging", plant, by, "factoryctl")
        print(msg)
        if not ok:
            return 1
        cur.execute("select max(id) as id from events where name='release.drill_requested'")
        req = cur.fetchone()["id"]
        until = time.time() + int(opt(argv, "--wait", "300"))
        got = None
        while time.time() < until:
            cur.execute("select name, payload from events where name in ('release.drill_planted','release.ship_failed') "
                        "and (payload->>'request_id')::bigint = %s", (req,))
            got = cur.fetchone()
            if got:
                break
            time.sleep(3)
        if not got or got["name"] != "release.drill_planted":
            print("仕込めなかった: " + (str(got["payload"].get("error"))[-300:] if got else "shipper が時間内に応えなかった")); return 1
        cur.execute("insert into events(name, payload) values ('incident.drill_started', %s)",
                    (Jsonb({"repo": repo, "drill": plant, "by": by, "env": "staging", "request_id": req}),))
        import qa_line
        night = qa_line.night_of(datetime.now(timezone.utc))
        try:
            q = qa_line.enqueue(cur, gh, repo, by=by, priority="p1",
                                extra={"mode": "explore", "charter": opt(argv, "--charter", "other-user"), "night": night, "unattended": False})
        except psycopg.errors.UniqueViolation:
            print("仕込んだが、この製品の QA のジョブがもう動いているので積めなかった（終わってから factoryctl qa explore で積む）"); return 1
        if drain.active(cur):
            drain.allow(cur, [q["corr"]], by, "S1 の演習（factoryctl incident drill）")
        print(f"仕込んだ。QA 役の探索を積んだ（ジョブ {q['job_id']}・{repo}#{q['issue']}）。S1 が見つかればラインを差し止める")
        return 0
    if sub == "drill-end":
        d = incident.drill_active(cur, repo)
        if not d:
            print("この製品は演習の最中ではない"); return 1
        reason = opt(argv, "--reason", "演習を終えた")
        n = sum(incident.close_incident(cur, i["incident_id"], by, reason) for i in incident.open_incidents(cur, repo)
                if i.get("drill") and i.get("severity") in incident.HOLD_SEVERITIES)   # 演習が起こした S1 だけ。ほかは本物の不具合として残す
        cur.execute("insert into events(name, payload) values ('incident.drill_ended', %s)",
                    (Jsonb({"repo": repo, "drill": d.get("drill"), "by": by, "reason": reason, "closed_incidents": n}),))
        ok, msg = ship.request_reseed(cur, repo, "staging", by, "factoryctl")
        print(f"演習を終えた（演習の Incident {n} 件を閉じた）。{msg}")
        if incident.held(cur):
            print("※ ラインの差し止めはまだ残っている。解除は factoryctl line release --reason ... か操業コンソールで")
        return 0 if ok else 1
    print("factoryctl incident list|budget|close|drill|drill-end ..."); return 2


def qa_cmd(cur, argv) -> int:
    """factoryctl qa run|show（ADR 0028 の決定 2）"""
    import qa_line
    sub = argv[2] if len(argv) > 2 else ""
    repo = argv[3] if len(argv) > 3 else ""
    if sub not in ("run", "show", "explore", "synthetic", "learn", "gate") or "/" not in repo:
        print("factoryctl qa run|show|explore|synthetic|learn|gate <owner/repo> ..."); return 2
    if sub == "gate":   # 出荷の QA の関門（ADR 0049）
        import qa_gate
        gates = qa_gate.recent(cur, repo, int(opt(argv, "--limit", "5")))
        state_ja = {"open": "結果を待っている", "decided": "報告済み", "superseded": "次の版に替わった"}
        for g in gates:
            names = {"verdict": "判定", "explore": "探索", qa_gate.TIME: "時刻の判定"}
            jobs = "・".join(f"{names[w]} " + ",".join(str(j["job_id"]) for j in g["jobs"][w]) for w in qa_gate.ALL if g["jobs"][w])
            print(f"{g['opened_at']:%m-%d %H:%M} {g['release_id']}: {state_ja.get(g['state'], g['state'])}（ジョブ {jobs or 'まだ'}）")
            if g["decided"]:
                for r in g["decided"].get("report") or []:
                    print(f"    {r['label']}: {r['text']}")
            if g["blocked"]:
                print(f"    見送り（{g['blocked'].get('by')}）: {g['blocked'].get('note')}")
        if not gates:
            print("関門の記録がない（staging に配備すると supervisor が開く。QA_GATE の製品だけ）")
        return 0 if gates else 1
    if sub == "learn":
        import quality_learn
        from datetime import datetime, timezone
        got = quality_learn.step(cur, repo, datetime.now(timezone.utc), force=True)
        if not got:
            print("この週の品質の Learn はもう回っている（supervisor の週 1 回か、前の factoryctl qa learn）"); return 1
        f = got["facts"]
        print(f"{got['week']}: Defect {f['defects']} 件・Incident {f['incidents']} 件・流出防止率 {f['escape_prevention']}・発見工程 {f['by_found_in']}")
        print(f"  重点 {got['focus']}・足したシナリオ {got['scenarios'] or 'なし'}・基準の変更案 {got['proposal'] or 'なし'}"
              "（案は supervisor が委員会の順番を見て知見の Issue にする）")
        return 0
    if sub == "show":
        rows = qa_line.latest(cur, repo, int(opt(argv, "--limit", "10")))
        for r in rows:
            print(f"{r['at']:%m-%d %H:%M} job {r['job_id']} {r.get('release_id') or '-'} {r['story_id']}: {r['decision']}"
                  f"（確信度 {r.get('confidence')}・Defect {r.get('defects')}・印 {r.get('flags')}）")
        if not rows:
            print("判定の記録がない（factoryctl qa run で流す）")
        nights = qa_line.explore_nights(cur, repo, 5)
        for r in nights:
            filed = [d["issue"] for d in r.get("defects") or [] if d.get("issue")]
            what = f"出荷の関門 {r['gate']}" if r.get("gate") else f"{r.get('night')} の夜" + ("" if r.get("unattended") else "（人が積んだ）")
            print(f"{r['at']:%m-%d %H:%M} job {r['job_id']} 探索 {what} {r.get('charter')}: "
                  f"試した {r.get('tried')}・見つけた {r.get('findings')}・印 {len(r.get('flags') or [])}" + (f"（{' '.join(filed)}）" if filed else ""))
        return 0 if rows or nights else 1
    level = opt(argv, "--priority", "p1")
    if level not in ops.LEVELS:
        print(f"--priority は {'・'.join(ops.LEVELS)} のどれか"); return 2
    import ghapi

    def gh(method, path, body=None):
        return ghapi.request(method, path, body, user_agent="factoryctl", token=ghapp.token())
    stories = [x for x in (opt(argv, "--stories", "") or "").split(",") if x]
    if sub == "synthetic":
        fr = opt(argv, "--friction", None)
        friction = None if fr is None else qa_line.friction_from(fr)
        try:
            got = qa_line.enqueue_synthetic(cur, gh, repo, by=opt(argv, "--by", "factoryctl"), env=opt(argv, "--env", "staging"),
                                            friction=friction, unattended=False)
        except psycopg.errors.UniqueViolation:
            print("この製品の QA のジョブがもう動いている（factoryctl list）"); return 1
        print(f"enqueued job {got['job_id']}（合成の利用者・{repo}#{got['issue']}・版 {got['release_id'] or '不明'}・仕込み {sorted(got['friction']) or 'なし'}）")
        if drain.active(cur):
            if "--through-pause" in argv:
                drain.allow(cur, [got["corr"]], opt(argv, "--by", "factoryctl"), "合成の利用者（factoryctl qa synthetic --through-pause）")
                print("  ラインは一時停止中。このジョブだけ流す（drain allow）")
            else:
                print("  ラインは一時停止中なので、再開するまで流れない（--through-pause を付けて積み直す）")
        return 0
    if sub == "explore":
        from datetime import datetime, timezone
        night = qa_line.night_of(datetime.now(timezone.utc))
        try:
            got = qa_line.enqueue_explore(cur, gh, repo, night, by=opt(argv, "--by", "factoryctl"), unattended=False)
        except psycopg.errors.UniqueViolation:
            print("この製品の QA のジョブがもう動いている（factoryctl list）"); return 1
        print(f"enqueued job {got['job_id']}（夜間の探索・{repo}#{got['issue']}・チャーター {got['charter']}・版 {got['release_id'] or '不明'}）")
        if drain.active(cur):
            if "--through-pause" in argv:
                drain.allow(cur, [got["corr"]], opt(argv, "--by", "factoryctl"), "夜間の探索（factoryctl qa explore --through-pause）")
                print("  ラインは一時停止中。このジョブだけ流す（drain allow）")
            else:
                print("  ラインは一時停止中なので、再開するまで流れない（--through-pause を付けて積み直す）")
        return 0
    exclusive = False
    try:   # 時刻をまたぐストーリーを含むなら、同じ製品のほかの QA と重ねずに流す（ADR 0055 の決定 3）
        import qa_gate
        with_time, _ = qa_gate.time_stories(gh, repo)
        exclusive = bool(set(with_time) & set(stories)) if stories else bool(with_time)
    except Exception as e:  # noqa: BLE001  読めなければ重ねてよいものとして積む（時計の道具は重なると断る）
        print(f"  ※ ストーリーの time を読めなかった（{str(e)[:120]}）。ほかの QA と重なると時計を進められない")
    try:
        got = qa_line.enqueue(cur, gh, repo, env=opt(argv, "--env", "staging"), stories=stories, by=opt(argv, "--by", "factoryctl"), priority=level,
                              exclusive_env=exclusive)
    except psycopg.errors.UniqueViolation:
        print("この製品の QA のジョブがもう動いている（factoryctl list）"); return 1
    print(f"enqueued job {got['job_id']}（QA 役・{repo}#{got['issue']}・版 {got['release_id'] or '不明'}・{level}）")
    if exclusive:
        print("  時刻をまたぐストーリーを含むので、この製品のほかの QA のジョブが終わってから始まる（ADR 0055）")
    if not got["release_id"]:
        print("  ※ 環境に出ている版の記録がない。先に factoryctl env up で触れる環境を立てる")
    if drain.active(cur):
        if "--through-pause" in argv:
            drain.allow(cur, [got["corr"]], opt(argv, "--by", "factoryctl"), "QA の判定（factoryctl qa run --through-pause）")
            print("  ラインは一時停止中。このジョブだけ流す（drain allow）")
        else:
            print("  ラインは一時停止中なので、再開するまで流れない（このジョブだけ流すなら --through-pause を付けて積み直す）")
    return 0


def readiness_cmd(cur, argv) -> int:
    """オンボーディングの判定の記録（onboarding.py）"""
    import onboarding
    import readiness
    sub = argv[2] if len(argv) > 2 else "show"
    defs = readiness.load()
    if sub == "record" and len(argv) > 3:
        text = sys.stdin.read() if argv[3] == "-" else open(argv[3], encoding="utf-8").read()
        doc = yaml.safe_load(text)
        rid, probs = onboarding.record(cur, defs, doc, opt(argv, "--by", (doc or {}).get("by") or "factoryctl") if isinstance(doc, dict) else "factoryctl")
        if probs:
            print("記録しなかった:\n" + "\n".join(f"- {p}" for p in probs))
            return 1
        sm = onboarding.summaries(cur, defs)
        s = next(x for x in sm if x["product"] == doc["product"])
        print(f"記録した（events #{rid}）: {s['product']} L{s['reached']} {s['reached_ja']}・いちばん弱い仕事 {s['weakest']['target_ja']} "
              f"{s['weakest']['verdict_ja']}・お願い {s['asks']} 件" + ("・稼働 ready の承認を受信トレイに出す" if s["ready_ok"] and not s["past_ready"] else ""))
        return 0
    if sub == "show":
        product = argv[3] if len(argv) > 3 and not argv[3].startswith("--") else None
        rows = [r for r in onboarding.summaries(cur, defs) if not product or r["product"] == product]
        if not rows:
            print("判定の記録がない（factoryctl readiness record で残す）")
            return 1
        for r in rows:
            print(f"{r['product']}  L{r['reached']} {r['reached_ja']}" + (f" → L{r['next']} {r['next_ja']}" if r["next"] else "")
                  + ("  稼働 ready 承認済み" if r["approved"] else "  稼働 ready を越えた" if r["past_ready"] else "  承認待ち" if r["ready_ok"] else "")
                  + f"  判定 {r['at'][:16]}")
            rec = onboarding.latest(cur, r["product"])[0]
            for t, tj in defs["targets"].items():
                j = rec["result"]["targets"][t]
                print(f"  {tj['ja']:<10} {readiness.band_ja(defs, j['verdict'])}（{round(j['ratio'] * 100)}%）お願い {len(j['asks'])} 件")
        return 0
    print(__doc__)
    return 2


def main(argv):
    if len(argv) > 1 and argv[1] == "version":   # サービスごとの版（ADR 0031）。DB を使わない。--expect <コミット> で、ずれたら終了コード 1
        import version
        text, code = version.report(version.list_running(os.environ.get("COMPOSE_PROJECT", "factory")), opt(argv, "--expect"))
        print(text)
        return code
    if len(argv) > 2 and argv[1] == "runner" and argv[2] == "check":   # Mac ランナー（ADR 0028）に届くか。DB を使わない
        import macrunner_client
        lines, code = macrunner_client.check()
        print("\n".join(lines))
        return code
    conn = psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row, autocommit=True)
    cur = conn.cursor()
    cur.execute("select set_config('timezone', %s, false)", (os.environ.get("REPORT_TZ", "Asia/Tokyo"),))   # 表示は建設者の時刻で
    cmd = argv[1] if len(argv) > 1 else "list"
    if cmd == "list":
        where = "" if "--all" in argv else "where state in ('queued','running') or finished_at > now() - interval '1 day'"
        cur.execute(f"select id, state, repo, issue_number, attempt, enqueued_at, started_at, finished_at, error, result->>'pr_url' as pr_url from jobs {where} order by id desc limit 50")
        for r in cur.fetchall():
            print(f"{r['id']:>4} {r['state']:<9} {r['repo']}#{r['issue_number']} a{r['attempt']} {r['enqueued_at']:%m-%d %H:%M} {r['error'] or ''} {r['pr_url'] or ''}")
    elif cmd == "stop":
        print("cancel requested" if ops.stop_job(cur, int(argv[2]), "factoryctl", opt(argv, "--reason", "")) else "job is not active")
    elif cmd == "rerun":   # 続きのジョブは続きとして積み直す。中身は rerun.py（操業コンソールの「やり直す」と共通）
        new_id, msg = rerun.rerun(cur, int(argv[2]), continue_draft="--continue" in argv, force="--force" in argv)
        print(msg)
        if new_id is None:
            return 1
    elif cmd == "enqueue":   # 手動投入（試験運転や、ラベルを使わない再投入）。--tier tier-small でモデル tier を変えられる（ペア比較の材料）
        repo, issue = argv[2], int(argv[3])
        tier = argv[argv.index("--tier") + 1] if "--tier" in argv else os.environ.get("MODEL_TIER", "tier-large")
        t = ops.upsert_ticket(cur, "github", f"{repo}#{issue}", repo, state="active")
        try:
            cur.execute("insert into jobs(correlation_id, repo, issue_number, enqueued_by, model_tier, ticket_id, priority) values (%s,%s,%s,'factoryctl',%s,%s,%s) returning id",
                        (f"github:{repo}#{issue}", repo, issue, tier, t["id"], t["priority"]))
        except psycopg.errors.UniqueViolation:
            print("an active job already exists for this issue"); return 1
        new_id = cur.fetchone()["id"]
        cur.execute("insert into events(name, correlation_id, job_id, payload) values ('job.enqueued', %s, %s, %s)",
                    (f"github:{repo}#{issue}", new_id, Jsonb({"by": "factoryctl"})))
        print(f"enqueued job {new_id}")
    elif cmd == "tickets":
        where = "" if "--all" in argv else "where state <> 'closed'"
        cur.execute(f"select t.*, (select string_agg(j.id || ':' || j.state, ' ') from jobs j where j.ticket_id=t.id and j.state in ('queued','running')) as jobs "
                    f"from tickets t {where} order by priority desc, created_at limit 100")
        for t in cur.fetchall():
            hold = " 保留" if t["held"] else ""
            wait = f" 依存待ち {' '.join(t['waiting_on'])}" if t["waiting_on"] else ""
            print(f"{t['id']:>4} {ops.level_of(t['priority'])}({t['priority']}) {t['state']:<7} {t['source_ref']} {t['title'][:40]}{hold}{wait} {t['jobs'] or ''}")
    elif cmd == "line" and len(argv) > 2 and argv[2] == "release":   # QA 役の差し止めの解除（ADR 0043 の O5）。人だけ
        try:
            ok = incident.release(cur, f"factoryctl:{opt(argv, '--by', 'human')}", opt(argv, "--reason", "") or "")
        except ValueError as e:
            print(e); return 2
        print("差し止めを解除した。ラインは次の周回から流れる" if ok else "差し止めていない")
        return 0 if ok else 1
    elif cmd == "line":
        if len(argv) > 2 and argv[2] == "scale":
            vals = {k: int(opt(argv, f"--{k}")) for k in line.FIELDS if opt(argv, f"--{k}") is not None}
            if not vals:
                print("--workers・--tickets・--governance・--desk のどれかを渡す")
                return 2
            try:
                line.scale(cur, "factoryctl", opt(argv, "--reason", ""), **vals)
            except ValueError as e:
                print(e)
                return 2
        st = line.status(cur)
        d, r = st["desired"], st["running"]
        print(f"ラインのワーカー: {r['workers']} / {d['workers']}　委員会の枠: {r['governance']} / {d['governance']}　窓口の枠: {r['desk']} / {d['desk']}　同時に流すチケット: {'?' if r.get('tickets') is None else r['tickets']} / {d['tickets']}")
        print(f"宣言: {st['by']}{'（' + st['reason'] + '）' if st['reason'] else ''}{' ' + st['at'] if st['at'] else '（環境変数の値）'}")
        if drain.active(cur):
            print("ラインは一時停止中（宣言の数に関わらず、ラインの枠は取り出さない）")
        hold_report(cur)
    elif cmd == "budget":
        if len(argv) > 2 and argv[2] == "set":
            try:
                daily = float(opt(argv, "--daily")) if opt(argv, "--daily") is not None else None
                week = {k: float(opt(argv, f"--{k}")) for k in usage.WEEKDAYS if opt(argv, f"--{k}") is not None}
            except ValueError:
                print("額はドルの数で渡す")
                return 2
            mode = opt(argv, "--mode") or ("weekday" if week and daily is None else ("daily" if daily is not None and not week else None))
            if daily is None and not week and mode is None:
                print("--daily か --mon..--sun（と --mode）のどれかを渡す")
                return 2
            try:
                usage.set_budget(cur, "factoryctl", opt(argv, "--reason", ""), mode=mode, daily_usd=daily, weekday_usd=week)
            except ValueError as e:
                print(e)
                return 2
        b = usage.budget_spec(cur)
        print(f"予算: {usage.BUDGET_MODE_JA[b['mode']]} — {usage.budget_text(b)}")
        print(f"今日の予算: {'なし（予算で止めない）' if b['today_usd'] is None else '$' + format(b['today_usd'], ',.2f')}")
        print(f"宣言: {b['by']}{'（' + b['reason'] + '）' if b['reason'] else ''}{' ' + b['at'] if b['at'] else '（環境変数の値）'}")
    elif cmd == "drain":
        if len(argv) > 2 and argv[2] == "status":
            return drain_report(cur)
        if len(argv) > 2 and argv[2] == "allow":
            urls = [a for a in argv[3:] if a.startswith("https://")]
            level = opt(argv, "--priority")
            if level is not None and level not in ops.LEVELS:
                print(f"--priority は {'・'.join(ops.LEVELS)} のどれか")
                return 2
            minp = ops.LEVELS[level][0] if level else None
            corrs = []
            for u in urls:
                issue = drain.issue_corr(u)
                if issue:   # Issue はチケットの correlation_id。デザインの線から実装・検査・judge まで同じ流れで通る（p0 の検証チケット）
                    corrs.append(issue)
                    continue
                cur.execute("select correlation_id from artifacts where type='PR' and payload->>'pr_url'=%s order by id desc limit 1", (u,))
                r = cur.fetchone()
                if not r:
                    print(f"工場の PR でも Issue でもない: {u}")
                    return 2
                corrs += [r["correlation_id"], f"committee:{u}"]   # 衝突の解消・検査・judge はチケットの、委員会は PR の correlation_id
            if not (urls or level) or not drain.allow(cur, corrs, "factoryctl", opt(argv, "--reason", ""), minp):
                print("一時停止していない（流してよいものを足すのは一時停止の間だけ）" if urls or level else "PR か Issue の URL か --priority を渡す")
                return 1
            if urls:
                print(f"一時停止のまま流す: {len(urls)} 本（{' '.join(urls)}）")
            if level:
                print(f"一時停止のまま流す: 優先度 {level} 以上のチケットのジョブ")
            return drain_report(cur)
        changed = drain.set_drain(cur, True, "factoryctl", opt(argv, "--reason", ""))
        print("ラインを一時停止にした（順番待ちを取り出さない）" if changed else "すでに一時停止中")
        if "--wait" not in argv:
            return drain_report(cur)
        wait = opt(argv, "--wait")
        # 秒を省いた --wait（後ろに何もない・次がオプション）は既定の秒数だけ待つ。前は待たずに終わっていた（#269）
        until = time.time() + (int(wait) if wait and wait.isdigit() else DRAIN_WAIT_DEFAULT_SEC)
        while not drain.status(cur)["ready"] and time.time() < until:
            time.sleep(5)
        return drain_report(cur)
    elif cmd == "resume" and (len(argv) < 3 or argv[2].startswith("--")):
        changed = drain.set_drain(cur, False, "factoryctl", opt(argv, "--reason", ""))
        print("ラインを再開した" if changed else "一時停止していない")
    elif cmd in ("priority", "hold", "resume"):
        t = find_ticket(cur, argv[2]) if len(argv) > 2 else None
        if not t:
            print("no such ticket"); return 1
        if cmd == "priority":
            v = argv[3]
            value = ops.LEVELS[v][0] if v in ops.LEVELS else int(v)
            t = ops.set_priority(cur, t["id"], value, "factoryctl", opt(argv, "--reason", ""))
            print(f"ticket {t['id']} priority {ops.level_of(t['priority'])}({t['priority']})")
        else:
            t = ops.set_held(cur, t["id"], cmd == "hold", "factoryctl", opt(argv, "--reason", ""))
            print(f"ticket {t['id']} {'held' if t['held'] else 'resumed'}")
    elif cmd == "events":
        job_id = int(argv[2])
        cur.execute("select name, occurred_at, payload from events where job_id=%s order by id", (job_id,))
        for r in cur.fetchall():
            print(f"{r['occurred_at']:%H:%M:%S} {r['name']:<22} {json.dumps(r['payload'], ensure_ascii=False)}")
    elif cmd == "stoppage":
        sub = argv[2] if len(argv) > 2 else "list"
        if sub == "open":
            category, what = argv[3], argv[4]
            key = opt(argv, "--key", f"manual:{datetime.now(timezone.utc):%Y%m%dT%H%M%S}")
            ok = stoppage.open_stoppage(cur, key, category, what, waiting_for=opt(argv, "--waiting-for", ""), cause=opt(argv, "--cause", ""),
                                        prevention=opt(argv, "--prevention", ""), started_at=opt(argv, "--at"), detected_by=opt(argv, "--by", "builder"), quiet="--quiet" in argv)
            print(f"opened {key}" if ok else f"{key} already exists")
        elif sub == "close":
            key = argv[3]
            ok = stoppage.close_stoppage(cur, key, remedy=opt(argv, "--remedy", ""), prevention=opt(argv, "--prevention", ""),
                                         ended_at=opt(argv, "--at"), closed_by="builder", quiet="--quiet" in argv, woken_by=opt(argv, "--woken-by"))
            print(f"closed {key}" if ok else f"{key} is not open")
        else:
            where = "where ended_at is null" if "--open" in argv else ""
            cur.execute(f"select * from stoppages {where} order by started_at")
            for r in cur.fetchall():
                end = f"{r['ended_at']:%m-%d %H:%M}" if r["ended_at"] else "open"
                print(f"{r['key']:<26} {r['category']:<11} {r['started_at']:%m-%d %H:%M} → {end:<11} {r['what']}")
    elif cmd == "committee" and len(argv) > 3 and argv[2] == "reopen":
        # 人に回した知見の PR を、委員会にかけ直す（2026-11-04 オーナー: 知見の PR は委員会の可決でマージする。決める前に人へ回ったものの移行用）
        for url in argv[3:]:
            cur.execute("insert into events(name, payload) values ('committee.reopened',%s)", (Jsonb({"pr_url": url, "by": "builder"}),))
            print(f"reopened {url}（次の周回で委員会に積み直し、承認待ちのラベルを外す）")
    elif cmd == "report" and len(argv) > 2 and argv[2] == "stoppages":
        report_stoppages(cur, argv)
    elif cmd == "report" and len(argv) > 2 and argv[2] == "provenance":
        report_provenance(cur, argv)
    elif cmd == "report" and len(argv) > 2 and argv[2] == "judgments":
        report_judgments(cur, argv)
    elif cmd == "report" and len(argv) > 2 and argv[2] == "usage":
        v = usage.view(cur, days=int(opt(argv, "--days", "14")))
        if "--json" in argv:
            print(json.dumps(v, ensure_ascii=False, indent=2))
        else:
            print("\n".join([f"# 工場の使用量（{v['generated_at'][:16]} 時点、制御プレーンから生成）", "",
                             "生成: `factoryctl report usage`。費用は API 単価に換算した消費の物差しで、定額の契約の実請求ではない", ""] + usage.report_lines(v)))
    elif cmd == "report" and len(argv) > 2 and argv[2] == "kpi":
        return report_kpi_cmd(cur, argv)
    elif cmd == "report" and len(argv) > 2 and argv[2] == "send":
        return report_send(cur, argv)
    elif cmd == "report" and len(argv) > 2 and argv[2] == "process":
        retro = process_learn.retrospect(cur, window_days=int(opt(argv, "--days", str(process_learn.WINDOW_DAYS))))
        print("\n".join([f"# 工程の信号（{retro['from'][:10]} 〜 {retro['to'][:10]}、制御プレーンから生成）", "",
                         "生成: `factoryctl report process`（ADR 0027 の 3）", ""] + process_learn.report_lines(retro)))
    elif cmd == "export" and len(argv) > 2 and argv[2] == "learning":
        since = datetime.fromisoformat(opt(argv, "--since", "2026-10-01")).replace(tzinfo=timezone.utc)
        print(json.dumps(learn_export.export(cur, since, opt(argv, "--repo")), ensure_ascii=False, indent=1, default=str))
    elif cmd == "learn" and len(argv) > 2 and argv[2] == "backfill-conflicts":
        return backfill_conflicts(cur, int(opt(argv, "--days", str(process_learn.WINDOW_DAYS))))
    elif cmd == "report" and len(argv) > 2 and argv[2] == "learning":
        retro = learn.retrospect(cur, window_days=int(opt(argv, "--days", str(learn.WINDOW_DAYS))))
        print("\n".join([f"# Learn ループ（{retro['from'][:10]} 〜 {retro['to'][:10]}、制御プレーンから生成）", "",
                         "生成: `factoryctl report learning`（ADR 0013）", ""] + learn.report_lines(retro) + [""]
                        + learn.rule_lines(learn.rule_stats(cur, datetime.fromisoformat(retro["from"]), datetime.fromisoformat(retro["to"])))))
    elif cmd == "mode":
        if len(argv) > 3 and argv[2] == "set":
            try:
                judgment.set_mode(cur, argv[3], "factoryctl", opt(argv, "--until"), opt(argv, "--reason", ""))
            except ValueError as e:
                print(e)
                return 2
        mode, basis = judgment.current_mode(cur, os.environ.get("FACTORY_MODE") or "attended", os.environ.get("MODE_SCHEDULE", ""))
        print(f"mode={mode}（決め手: {basis}）")
    elif cmd == "judgment":
        sub = argv[2] if len(argv) > 2 else "list"
        if sub == "recategorize":   # 類型の振り直し（人が決めた分け直しを当てる。冪等）
            import yaml
            with open(argv[3]) as f:
                print(judgment.recategorize(cur, yaml.safe_load(f)))
        elif sub == "grade":
            aid, grade = int(argv[3]), argv[4]
            if grade not in ("agree", "disagree"):
                print("grade は agree か disagree"); return 2
            cur.execute("select payload from artifacts where id=%s and type='Judgment'", (aid,))
            r = cur.fetchone()
            if not r:
                print("no such judgment"); return 1
            cur.execute("update artifacts set payload = jsonb_set(payload, '{human_grade}', %s) where id=%s", (Jsonb({"grade": grade, "note": opt(argv, "--note")}), aid))
            cur.execute("insert into events(name, payload) values ('judgment.graded', %s)",
                        (Jsonb({"artifact_id": aid, "category": r["payload"].get("category"), "stage": r["payload"].get("stage"), "mode": r["payload"].get("mode"),
                                "grade": grade, "note": opt(argv, "--note")}),))
            print(f"graded {aid} {grade}")
        else:
            where = "and payload->'human_grade' = 'null'::jsonb" if "--ungraded" in argv else ""
            cur.execute(f"select id, created_at, payload from artifacts where type='Judgment' {where} order by id desc limit %s", (int(opt(argv, "--limit", "30")),))
            for r in cur.fetchall():
                p = r["payload"]
                conf = (p.get("score") or {}).get("self_reported")
                print(f"{r['id']:>5} {p.get('stage', ''):<11} {p.get('category', ''):<13} {p.get('verdict') or '':<9} conf={conf} {p.get('mode', '')} {(p.get('question') or '')[:60]}")
    elif cmd == "design":
        return design_cmd(cur, argv)
    elif cmd == "plan":
        return plan_cmd(cur, argv)
    elif cmd == "release" and len(argv) > 4 and argv[2] == "create":
        return release_create(cur, argv)
    elif cmd == "release" and len(argv) > 3 and argv[2] in ("ship", "approve", "rollback"):
        by = opt(argv, "--by", "builder")
        if argv[2] == "ship":
            ok, msg = ship.request_ship(cur, argv[3], by, "factoryctl")
        elif argv[2] == "approve":
            ok, msg = ship.request_approve(cur, argv[3], by, "factoryctl", skip_qa=opt(argv, "--reason", "") if "--skip-qa" in argv else None)
        else:
            ok, msg = ship.request_rollback(cur, argv[3], opt(argv, "--env", "prod"), by, "factoryctl")
        print(msg)
        return 0 if ok else 1
    elif cmd == "release" and len(argv) > 2 and argv[2] == "trains":
        return release_trains(cur, int(opt(argv, "--limit", "10")))
    elif cmd == "release" and len(argv) > 3 and argv[2] == "train":
        if "--emergency" not in argv:
            print("定期便は supervisor が出す。手で出すのは緊急便だけ（--emergency --reason ..）")
            return 2
        ok, msg = train.emergency(cur, argv[3], opt(argv, "--by", "builder"), opt(argv, "--reason", ""))
        print(msg)
        return 0 if ok else 1
    elif cmd == "release" and len(argv) > 3 and argv[2] == "train-approve":
        ok, msg = train.approve(cur, argv[3], opt(argv, "--by", "builder"), "factoryctl",
                                skip_qa=opt(argv, "--reason", "") if "--skip-qa" in argv else None)
        print(msg)
        return 0 if ok else 1
    elif cmd == "release" and len(argv) > 3 and argv[2] == "status":
        return release_status(cur, argv[3])
    elif cmd == "env" and len(argv) > 4 and argv[2] in ("pr-up", "pr-touch", "pr-down"):
        repo, pr, by = argv[3], int(argv[4].lstrip("#")), opt(argv, "--by", "builder")
        if argv[2] == "pr-up":
            ok, msg = ephemeral.request_up(cur, repo, pr, by, "factoryctl")
            print(msg)
            if not ok or "--no-wait" in argv:
                return 0 if ok else 1
            return env_pr_wait(cur, repo, pr, float(opt(argv, "--timeout", "3600")))
        ok, msg = (ephemeral.touch(cur, repo, pr, by, "human", opt(argv, "--note", "")) if argv[2] == "pr-touch"
                   else ephemeral.request_down(cur, repo, pr, by, "factoryctl"))
        print(msg)
        return 0 if ok else 1
    elif cmd == "distribute" and len(argv) > 2 and argv[2] == "list":
        for r in distribution.recent(cur):
            res = r["res"] or {}
            what = ("配った " + str(res.get("build_number")) if r["result"] == "ship.preview_distributed"
                    else f"失敗（{res.get('stage')}）{str(res.get('error') or '')[:80]}" if r["result"] else "結果なし")
            print(f"{r['id']:>6} {r['occurred_at']:%m-%d %H:%M} {r['req'].get('channel')} {r['req'].get('app')}@{r['req'].get('ref')} {what}")
    elif cmd == "distribute":
        defines = dict(argv[i + 1].split("=", 1) for i, a in enumerate(argv) if a == "--define" and i + 1 < len(argv) and "=" in argv[i + 1])
        ok, p = distribution.distribute(cur, app=opt(argv, "--app", distribution.DEFAULT_APP), channel=opt(argv, "--channel", "firebase_android"),
                                        ref=opt(argv, "--ref", "main"), notes=opt(argv, "--notes", ""), defines=defines,
                                        by=opt(argv, "--by", "builder"), timeout=float(opt(argv, "--timeout", "3600")),
                                        log=lambda m: print(m, flush=True))
        print(("配った: " if ok else "配れなかった: ") + json.dumps(p, ensure_ascii=False, default=str))
        return 0 if ok else 1
    elif cmd == "env" and len(argv) > 2 and argv[2] == "pr-show":
        return env_pr_show(cur, argv[3] if len(argv) > 3 and not argv[3].startswith("--") else None)
    elif cmd == "env" and len(argv) > 3 and argv[2] == "up":
        return touch.up(cur, argv[3], release_id=opt(argv, "--release"), fresh="--fresh" in argv, by=opt(argv, "--by", "builder"),
                        timeout=float(opt(argv, "--timeout", "3600")), do_wait="--no-wait" not in argv,
                        log=lambda m: print(m, flush=True))
    elif cmd == "env" and len(argv) > 3 and argv[2] == "restart":
        ok, msg, _ = touch.restart(cur, argv[3], opt(argv, "--env", "staging"), advance_hours=float(opt(argv, "--advance-hours", "0")),
                                   reset_clock="--reset-clock" in argv, by=opt(argv, "--by", "builder"),
                                   do_wait="--no-wait" not in argv, log=lambda m: print(m, flush=True))
        print(msg)
        return 0 if ok else 1
    elif cmd == "env" and len(argv) > 3 and argv[2] == "show":
        t = touch.latest(cur, argv[3], opt(argv, "--env", "staging"))
        print(touch.render(t) if t else "いまの版の入口の記録がない（factoryctl env up で立てる）")
        return 0 if t else 1
    elif cmd == "readiness":
        return readiness_cmd(cur, argv)
    elif cmd == "qa":
        return qa_cmd(cur, argv)
    elif cmd == "incident":
        return incident_cmd(cur, argv)
    elif cmd == "incident-record":
        return incident_record_cmd(cur, argv)
    elif cmd == "investigation":
        return investigation_cmd(cur, argv)
    elif cmd == "swap":
        return swap_cmd(cur, argv)
    elif cmd == "desk":
        return desk_cmd(cur, argv)
    elif cmd == "restricted":
        return restricted_cmd(cur, argv)
    else:
        print(__doc__); return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
