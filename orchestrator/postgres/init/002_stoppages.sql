-- 操業停止（Stoppage）の記録（ADR 0005）。
-- 状態は events にだけ持つ: stoppage.opened / stoppage.closed を payload.key で対にする。表は作らず、この view で読む。
-- 既存の DB にも当てられるように冪等に書く（docker exec factory-postgres-1 psql -U factory -f ...）。

create or replace view stoppages as
select o.payload->>'key'                                                as key,
       coalesce((o.payload->>'started_at')::timestamptz, o.occurred_at) as started_at,
       coalesce((c.payload->>'ended_at')::timestamptz, c.occurred_at)   as ended_at,
       o.payload->>'category'                                           as category,     -- env | perm | human | factory-bug | ticket | external | budget
       o.payload->>'what'                                               as what,         -- 何が止まったか
       o.payload->>'waiting_for'                                        as waiting_for,  -- 何を待ったか
       o.payload->>'cause'                                              as cause,
       o.payload->>'detected_by'                                        as detected_by,  -- watcher | builder | supervisor
       c.payload->>'remedy'                                             as remedy,       -- その場の手当て
       coalesce(c.payload->>'prevention', o.payload->>'prevention')     as prevention,   -- 再発を防ぐ仕組み（案 / 済）
       o.correlation_id,
       o.job_id
  from events o
  left join events c on c.name = 'stoppage.closed' and c.payload->>'key' = o.payload->>'key'
 where o.name = 'stoppage.opened';

create index if not exists events_name_idx on events (name, occurred_at);
