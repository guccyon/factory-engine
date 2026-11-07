-- 単発の依頼のループ（ADR 0039 の順番 1）。窓口の会話に、受付から提出（受取確認まで）の状態と、閉じる理由・依頼どうしの結びを足す。
-- 足すだけ（expand）で、今の状態・列はそのまま使える。会話の中身は今どおり events の desk.*（correlation_id は desk:<会話の id>）。
-- 既存の DB にも当てられるように冪等に書く（docker compose exec postgres psql -U factory -f /docker-entrypoint-initdb.d/006_desk_loop.sql）。

-- 状態（決定 2）: 今の open・waiting_requester・drafted・confirmed・answered・closed に、
--   routed（振り分けた）・awaiting_confirm（確認待ち）・running（実行中）・delivered（提出した。受取確認待ち）・
--   accepted（依頼者が受け取った）・handed_off（チケット化で製品のラインに渡した）を足す
alter table desk_conversations drop constraint if exists desk_conversations_state_check;
alter table desk_conversations add constraint desk_conversations_state_check
  check (state in ('open','waiting_requester','drafted','confirmed','answered','closed',
                   'routed','awaiting_confirm','running','delivered','accepted','handed_off'));

-- 閉じる理由（決定 2・4）。完了にできるのは依頼者だけ（accepted・declined・superseded・withdrawn）。handed_off はチケット化で渡したとき
alter table desk_conversations add column if not exists close_reason text;
alter table desk_conversations drop constraint if exists desk_conversations_close_reason_check;
alter table desk_conversations add constraint desk_conversations_close_reason_check
  check (close_reason is null or close_reason in ('accepted','declined','superseded','withdrawn','handed_off'));
alter table desk_conversations add column if not exists closed_by  text;
alter table desk_conversations add column if not exists closed_at  timestamptz;

-- 対応の種類（ADR 0037 の道。ticket・direct_line・operate・quick・analysis）。振り分けで決まる。まだなら null
alter table desk_conversations add column if not exists path text;
-- 依頼の来た口（決定 1）。GitHub の Issue から来た依頼は Issue の URL。窓口から来た依頼は null
alter table desk_conversations add column if not exists source_ref text;
-- 断りから別案へ進めた依頼の結び（決定 4）。元の会話に superseded_by、新しい会話に derived_from
alter table desk_conversations add column if not exists superseded_by bigint references desk_conversations(id);
alter table desk_conversations add column if not exists derived_from  bigint references desk_conversations(id);
-- 続きの回数（決定 5。上限は仮置き 5）
alter table desk_conversations add column if not exists follow_ups integer not null default 0;

create index if not exists desk_conversations_source_ref_idx on desk_conversations (source_ref) where source_ref is not null;
