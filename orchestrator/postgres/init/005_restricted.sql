-- 知見の制限付きの保管先（ADR 0024 の段取り 5、決定 8）。利用者の情報をどうしても含む知見（障害対応など）の中身を置く。
-- git の知見（.factory-config/knowledge/K-NNNN.yaml）からは evidence の restricted: R-NNNN で指すだけにする。
-- 中身は管理者だけが持つ鍵（RESTRICTED_STORE_KEY。.env にもサービスの環境にも置かない）で pgcrypto の対称鍵暗号にして持つ。
-- ワーカーのジョブの DB は同じ Postgres に作るので、DB の権限だけでは管理者に限れない。鍵がなければ読めない形にする。
-- 中身を読んだ記録は restricted_reads と events の restricted.read に残す（誰が・いつ・何のために）。events には中身を書かない。
-- 既存の DB にも当てられるように冪等に書く（docker compose exec postgres psql -U factory -f /docker-entrypoint-initdb.d/005_restricted.sql）。

create extension if not exists pgcrypto;

create table if not exists restricted_records (
  id             bigserial   primary key,                 -- R-NNNN の番号（再利用しない。消さずに retired にする）
  product        text        not null,                    -- 製品（owner/repo）
  body           bytea       not null,                    -- 中身（pgp_sym_encrypt）。見出しも含めて平文では持たない
  knowledge_ids  text[]      not null default '{}',       -- この記録を指している知見（K-NNNN）。中身の手がかりにならない id だけ
  created_by     text        not null,
  created_at     timestamptz not null default now(),
  retired_at     timestamptz,                             -- 要らなくなった（保管の期限が来た・知見が廃止された）。中身は消す
  retired_by     text
);
create index if not exists restricted_records_product_idx on restricted_records (product, id);

create table if not exists restricted_reads (
  id          bigserial   primary key,
  record_id   bigint      not null references restricted_records(id),
  read_by     text        not null,
  purpose     text        not null,                       -- 何のために読んだか（必須）
  read_at     timestamptz not null default now()
);
create index if not exists restricted_reads_record_idx on restricted_reads (record_id, read_at desc);
