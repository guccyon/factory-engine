-- 事業部の窓口（ADR 0030 の順番 1）。外の人（とその代理の Claude）が依頼と問い合わせを持ち込む口の、トークンと会話。
-- 会話の中身（相手の言葉・プランナーの問い・下書き・回答）は events の desk.*（correlation_id は desk:<会話の id>）に残し、ここは会話の状態だけを持つ。
-- 既存の DB にも当てられるように冪等に書く（docker compose exec postgres psql -U factory -f /docker-entrypoint-initdb.d/004_desk.sql）。

create table if not exists desk_tokens (
  id          bigserial   primary key,
  name        text        not null unique,                 -- 依頼者の名前（記録の by に残る）
  role        text        not null check (role in ('operator','requester')),   -- 運用主はすべての相談とチケット、依頼者は自分の相談と、自分の依頼から作ったチケットだけ
  products    text[]      not null default '{}',           -- 対象の製品（owner/repo）。運用主で空ならすべて
  token_hash  text        not null unique,                 -- sha256。トークンそのものは発行のときに 1 度だけ見せ、保存しない
  created_at  timestamptz not null default now(),
  revoked_at  timestamptz
);

create table if not exists desk_conversations (
  id          bigserial   primary key,
  kind        text        not null check (kind in ('request','inquiry')),     -- 依頼 / 問い合わせ
  product     text        not null,                        -- 対象の製品（owner/repo）
  opened_by   text        not null,                        -- desk_tokens.name
  state       text        not null default 'open'
              check (state in ('open','waiting_requester','drafted','confirmed','answered','closed')),
  title       text        not null default '',
  wanted      jsonb       not null default '{}'::jsonb,    -- 依頼者の希望（優先度の段・期限）
  rounds      integer     not null default 0,              -- 相手の問いと依頼者の返事の往復の数（上限は壁打ちが入ってから使う）
  cost_usd    numeric     not null default 0,              -- 相談のジョブの費用の合計（窓口の枠。ADR 0030 の 6）
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);
create index if not exists desk_conversations_by_idx on desk_conversations (opened_by, updated_at desc);

-- 窓口から入ったチケットは、依頼 → 確定 → チケット → PR → 出荷と追えるよう、もとの会話を持つ（tickets.source は desk）
alter table tickets add column if not exists desk_conversation_id bigint references desk_conversations(id);
