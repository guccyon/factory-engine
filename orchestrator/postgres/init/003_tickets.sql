-- 工場のチケットと優先度（ADR 0009）。
-- チケットは入口のサービス（GitHub Issue、将来は Jira・Redmine・Backlog・操業コンソール）から写した工場側の記録。
-- 優先度・保留・順番の正本はここ。入口のラベルは「最初の値」を渡すだけで、コンソールや factoryctl で変えた値を上書きしない。
-- 既存の DB にも当てられるように冪等に書く（docker exec factory-postgres-1 psql -U factory -f ...）。

create table if not exists tickets (
  id              bigserial   primary key,
  source          text        not null,                  -- github | jira | redmine | backlog | console
  source_ref      text        not null,                  -- 入口のサービスでの識別子（github は owner/repo#123）
  repo            text        not null,                  -- 作る対象の製品リポジトリ
  title           text        not null default '',
  url             text,
  state           text        not null default 'active'
                  check (state in ('planned','active','closed')),   -- planned: 依存・上限で投入待ち / active: ジョブに渡した / closed: 済み
  priority        smallint    not null default 50 check (priority between 0 and 100),   -- 大きいほど先。段は ops.LEVELS
  priority_set_by text        not null default 'default',          -- default | source（入口のラベル）| console | factoryctl
  held            boolean     not null default false,              -- 保留（順番待ちから一時的に外す）
  depends_on      text[]      not null default '{}',               -- 依存チケットの source_ref
  waiting_on      text[]      not null default '{}',               -- そのうちまだ閉じていないもの（supervisor が更新）
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  unique (source, source_ref)
);

alter table jobs add column if not exists ticket_id     bigint references tickets(id);
alter table jobs add column if not exists priority      smallint not null default 50;
alter table jobs add column if not exists preempted_for bigint;   -- 割り込みで止めたとき、先に流したジョブ
create index if not exists jobs_claim_idx on jobs (state, priority desc, enqueued_at);

-- キューの取り出し: 優先度の高い順、同じなら受け付けた順。保留中のチケットのジョブは取らない
create or replace function claim_job() returns jobs language plpgsql as $$
declare j jobs;
begin
  select * into j from jobs q
   where q.state = 'queued' and not q.cancel_requested
     and not exists (select 1 from tickets t where t.id = q.ticket_id and t.held)
   order by q.priority desc, q.enqueued_at
   for update skip locked
   limit 1;
  if j.id is null then return null; end if;
  update jobs set state = 'running', started_at = now() where id = j.id returning * into j;
  return j;
end $$;
