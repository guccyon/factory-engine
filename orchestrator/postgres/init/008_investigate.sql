-- 工場の中での一次調査の、調査用のロールとビュー（ADR 0048 の決定 2・3、順番 2。definitions/investigate.yaml の sources.db）。
-- expand だけ。既存の表は変えない。既存の DB にも当てられるように冪等に書く
--   docker compose exec postgres psql -U factory -f /docker-entrypoint-initdb.d/008_investigate.sql
--
-- 守り方:
--   - 調査役（ワーカー）には DB の接続情報を渡さない。調査役の SQL は dispatcher が受け取り（dispatcher/investigate_db.py）、
--     この利用者として別に繋いだ接続で、読み取り専用のトランザクションの中で流す
--   - 管理者（factory）の接続で SET ROLE して流す形にはしない。管理者の接続では SELECT の中の set_config('role', ...) で
--     元の権限に戻れてしまうため。この利用者で繋げば、戻る先の権限がない
--   - factory_investigate は最初は NOLOGIN。管理者が合言葉を決めて有効にし、dispatcher に INVESTIGATE_DATABASE_URL で渡す
--       docker compose exec postgres psql -U factory -c "alter role factory_investigate login password '...'"
--     有効にするまで、調査役の SQL の道具は「DB は使えない」と返す（ほかの道具で調べる）
--   - 読めるのは investigate スキーマのビューだけ。ビューは管理者の持ち物で、管理者の権限で元の表を読む（security_invoker は使わない）。
--     この利用者に元の表の権限は渡さない
--   - 入れないもの: restricted スキーマ（利用者の情報）、desk_tokens（窓口のトークンの指紋）、events の restricted.*
--   - 利用者の既定で読み取り専用（default_transaction_read_only）・1 文 10 秒（statement_timeout）にする。
--     行数の上限（500 行）は dispatcher が結果を切って守る

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'factory_investigate') then
    create role factory_investigate nologin nosuperuser nocreatedb nocreaterole noinherit;
  end if;
end $$;

alter role factory_investigate set default_transaction_read_only = on;
alter role factory_investigate set statement_timeout = '10s';
alter role factory_investigate set idle_in_transaction_session_timeout = '30s';

-- 制御プレーンの DB の CONNECT は PUBLIC から外している（dispatcher/jobdb.py）ので、明示して渡す
do $$
begin
  execute format('grant connect on database %I to factory_investigate', current_database());
end $$;

create schema if not exists investigate;
revoke all on schema investigate from public;

-- ジョブ（キュー・状態・結果・費用の元）
create or replace view investigate.jobs as
select id, correlation_id, repo, issue_number, kind, state, attempt, enqueued_at, started_at, finished_at,
       cancel_requested, model_tier, result, error, enqueued_by, ticket_id, priority, preempted_for
  from public.jobs;

-- イベント（制御プレーンの出来事の記録）。restricted.* は中身を書かないが、誰が何を読んだかも見せない
create or replace view investigate.events as
select id, name, occurred_at, correlation_id, job_id, payload
  from public.events
 where name not like 'restricted.%';

-- 停止（stoppage.opened と closed の対。002 のビューをそのまま）
create or replace view investigate.stoppages as
select * from public.stoppages;

-- チケット
create or replace view investigate.tickets as
select id, source, source_ref, repo, title, url, state, priority, priority_set_by, held, depends_on, waiting_on,
       created_at, updated_at, desk_conversation_id
  from public.tickets;

-- 判断（仮決め・judge・採点・人の決定）
create or replace view investigate.judgments as
select id, name, occurred_at, correlation_id, job_id, payload
  from public.events
 where name like 'judgment.%';

-- 窓口の会話（トークンの表は入れない。opened_by は名前だけ）
create or replace view investigate.desk_conversations as
select * from public.desk_conversations;

-- 費用（ジョブが終わったときの記録。種類とモデルは jobs から）
create or replace view investigate.costs as
select e.job_id, e.occurred_at as finished_at, e.correlation_id, j.repo, j.kind, j.model_tier,
       e.payload->>'state'                                   as state,
       nullif(e.payload->>'cost_usd', '')::numeric           as cost_usd,
       nullif(e.payload->>'billable_tokens', '')::bigint     as billable_tokens,
       coalesce((e.payload->>'usage_limited')::boolean, false) as usage_limited
  from public.events e
  left join public.jobs j on j.id = e.job_id
 where e.name = 'job.finished';

-- ラインの流量と予算の宣言（ADR 0029）
create or replace view investigate.line_declarations as
select id, name, occurred_at, correlation_id, payload
  from public.events
 where name in ('line.scaled', 'budget.set');

grant usage on schema investigate to factory_investigate;
grant select on all tables in schema investigate to factory_investigate;
