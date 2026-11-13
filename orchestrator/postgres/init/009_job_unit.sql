-- ジョブの単位（ADR 0054 の決定 3）。同じ Issue に、単位の違うジョブを並べて積めるようにする。
-- 版ごとの QA のチケット（factory:qa-release）に、シナリオごとのジョブ（verdict:<story_id>・explore:<charter>）を積む。
-- QA 以外のジョブは unit が空文字のままなので、「同じ repo#issue で queued / running は 1 件だけ」は今までどおり守られる。
-- 動いている DB には人が当てる（ADR 0031。何度流しても同じ結果になる）。当てるまで、関門は今までの形（固定 Issue に判定と探索の 2 本）で動く。
alter table jobs add column if not exists unit text not null default '';

begin;
drop index if exists jobs_one_active_per_issue;
create unique index jobs_one_active_per_issue
  on jobs (repo, issue_number, unit) where state in ('queued','running');
commit;
