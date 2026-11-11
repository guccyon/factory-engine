-- 知見の制限付きの保管先（ADR 0024 の段取り 5、決定 8）。利用者の情報をどうしても含む知見（障害対応など）の根拠の中身を置く。
-- git の知見（.factory-config/knowledge/K-NNNN.yaml）からは evidence の restricted: R-NNNN で指すだけにする。
-- 守り方は利用者を分けること（2026-11-05 オーナー: テーブル単位の細かい制御より、スキーマと利用者で分ける）。
--   - 専用のスキーマ restricted を、保管先の専用の利用者 factory_restricted の持ち物にし、PUBLIC からは何もできなくする
--   - factory_restricted は最初は NOLOGIN。管理者が合言葉を決めて有効にし、合言葉は管理者だけが持つ（.env に置かない）
--       docker compose exec postgres psql -U factory -c "alter role factory_restricted login password '...'"
--   - ワーカーのジョブの DB は管理者でない別の利用者（factory_job）の持ち物で、制御プレーンの DB には繋げない（dispatcher/jobdb.py）
--   - 制御プレーンのサービスは今も Postgres の管理者（factory）で動くので、そこからは見える。サービスを分けるのは別の作業で
-- 読んだ記録は restricted.reads と events の restricted.read に残す（誰が・いつ・何のために）。events には中身を書かない。
-- 既存の DB にも当てられるように冪等に書く（docker compose exec postgres psql -U factory -f /docker-entrypoint-initdb.d/005_restricted.sql）。

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'factory_restricted') then
    create role factory_restricted nologin nosuperuser nocreatedb nocreaterole;
  end if;
end $$;

-- 制御プレーンの DB の CONNECT は PUBLIC から外す（dispatcher/jobdb.py）ので、保管先の利用者には明示して渡す
do $$
begin
  execute format('grant connect on database %I to factory_restricted', current_database());
end $$;

create schema if not exists restricted authorization factory_restricted;
revoke all on schema restricted from public;

-- 前の形（public の表で中身を pgcrypto の暗号に）を当てていて、まだ空なら片付ける
do $$
begin
  if to_regclass('public.restricted_records') is not null then
    if not exists (select 1 from public.restricted_records) then   -- 表が無いときに読まないよう入れ子にする
      drop table if exists public.restricted_reads;
      drop table public.restricted_records;
    end if;
  end if;
end $$;

set role factory_restricted;

create table if not exists restricted.records (
  id             bigserial   primary key,                 -- R-NNNN の番号（再利用しない。消さずに retired にする）
  product        text        not null,                    -- 製品（owner/repo）
  body           text        not null,                    -- 中身。retired にしたら空にする
  knowledge_ids  text[]      not null default '{}',       -- この記録を指している知見（K-NNNN）
  created_by     text        not null,
  created_at     timestamptz not null default now(),
  retired_at     timestamptz,                             -- 要らなくなった（保管の期限・知見の廃止）
  retired_by     text
);
create index if not exists records_product_idx on restricted.records (product, id);

create table if not exists restricted.reads (
  id          bigserial   primary key,
  record_id   bigint      not null references restricted.records(id),
  read_by     text        not null,
  purpose     text        not null,                       -- 何のために読んだか（必須）
  read_at     timestamptz not null default now()
);
create index if not exists reads_record_idx on restricted.reads (record_id, read_at desc);

reset role;
