-- 工場の制御プレーン（P0）。キュー・成果物メタデータ・テレメトリを 1 つの DB に持つ（ADR 0003）。
-- 用語は definitions/artifacts.yaml と definitions/events.yaml に合わせる。

create table if not exists jobs (
  id              bigserial primary key,
  correlation_id  text        not null,                  -- github:<owner>/<repo>#<issue>
  repo            text        not null,
  issue_number    integer     not null,
  kind            text        not null default 'implement',
  state           text        not null default 'queued'
                  check (state in ('queued','running','succeeded','failed','cancelled')),
  attempt         integer     not null default 1,
  enqueued_at     timestamptz not null default now(),
  started_at      timestamptz,
  finished_at     timestamptz,
  cancel_requested boolean    not null default false,
  container_id    text,
  model_tier      text,
  result          jsonb,                                  -- ワーカーの最終行 JSON（pr_url, usage, ...）
  error           text,
  enqueued_by     text        not null default 'poll'      -- poll | webhook | rerun
);

-- 二重実行の抑止（1 段目）: 同じ repo#issue で queued / running は 1 件だけ
create unique index if not exists jobs_one_active_per_issue
  on jobs (repo, issue_number) where state in ('queued','running');
create index if not exists jobs_state_idx on jobs (state, enqueued_at);

create table if not exists events (
  id              bigserial primary key,
  name            text        not null,                  -- events.yaml のイベント名（job.* は制御プレーン固有）
  occurred_at     timestamptz not null default now(),
  correlation_id  text,
  job_id          bigint references jobs(id),
  payload         jsonb       not null default '{}'::jsonb,
  dedupe_key      text unique
);
create index if not exists events_corr_idx on events (correlation_id, occurred_at);

create table if not exists artifacts (
  id              bigserial primary key,
  type            text        not null,                  -- artifacts.yaml の型名（PR, Ticket, ...）
  correlation_id  text        not null,
  job_id          bigint references jobs(id),
  created_at      timestamptz not null default now(),
  produced_by     text,                                   -- ステージ ID
  confidentiality text        not null default 'product',
  payload         jsonb       not null default '{}'::jsonb,
  blob_key        text                                    -- MinIO 上のオブジェクトキー（ログなど）
);
create index if not exists artifacts_corr_idx on artifacts (correlation_id, created_at);

-- キューの取り出し（SKIP LOCKED）。dispatcher はこの関数でだけ claim する
create or replace function claim_job() returns jobs language plpgsql as $$
declare j jobs;
begin
  select * into j from jobs
   where state = 'queued' and not cancel_requested
   order by enqueued_at
   for update skip locked
   limit 1;
  if j.id is null then return null; end if;
  update jobs set state = 'running', started_at = now() where id = j.id returning * into j;
  return j;
end $$;
