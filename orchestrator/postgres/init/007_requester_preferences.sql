-- 依頼者ごとの好み（ADR 0040 の決定 6）。特定の依頼者（事業主の担当者）の好みは人に紐づくので git に置かず、制御プレーンに持つ。
-- 委員会には通さず、依頼者が窓口で見て直す・消す（その口は後の順番）。選び手は依頼者が分かる回（窓口の相談・振り分け・単発の実行）に渡す。
-- 足すだけ（expand）。既存の DB にも当てられるように冪等に書く（docker compose exec postgres psql -U factory -f /docker-entrypoint-initdb.d/007_requester_preferences.sql）。

create table if not exists requester_preferences (
  id           bigserial   primary key,                    -- 外に見せる id は P-NNNN（events には中身を書かず id だけ残す）
  product      text        not null,                       -- 製品（owner/repo）
  requester    text        not null,                       -- 依頼者（desk_tokens.name。窓口の会話の opened_by）
  statement    text        not null,                       -- 好み（例「表より箇条書きがよい」）
  work         text[]      not null default '{}',          -- 効く業務（definitions/work.yaml のノード）。空ならその依頼者の作業すべて
  axes         jsonb       not null default '{}',          -- ほかの軸の値（決定 2。例 {"request_form": ["one_off"]}）
  confidence   text        not null default 'hypothesis'
               check (confidence in ('hypothesis','confirmed')),   -- 依頼者が自分で言ったことは confirmed、工場が推し量ったことは hypothesis
  evidence     bigint[]    not null default '{}',          -- 根拠の events の id
  status       text        not null default 'active' check (status in ('active','retired')),
  verified_at  date        not null default current_date,
  created_by   text        not null,                       -- requester（本人）か factory（推し量り）
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);

create index if not exists requester_preferences_who_idx on requester_preferences (product, requester) where status = 'active';
