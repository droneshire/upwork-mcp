-- Upwork MCP schema. Run in the Supabase SQL editor (or `supabase db push`).
-- RLS is enabled with no policies, so only the service-role key can read/write.

create table if not exists jobs (
  id                 text primary key,           -- Upwork ciphertext (~01...) or URL hash
  url                text not null,
  title              text,
  posted             text,
  budget             text,
  description        text,
  skills             text[],
  meta               jsonb,                       -- proposals tier, client rating/spend/country
  tile_text          text,
  details            text,                        -- full job page text
  source             text,                        -- best_matches | most_recent | search:<q>
  first_seen         timestamptz not null default now(),
  last_seen          timestamptz not null default now(),
  details_fetched_at timestamptz
);
create index if not exists jobs_first_seen_idx on jobs (first_seen desc);

create table if not exists threads (
  id          text primary key,                   -- room_xxx
  url         text not null,
  title       text,
  preview     text,
  thread_text text,                               -- full conversation text
  last_seen   timestamptz not null default now(),
  fetched_at  timestamptz
);

create table if not exists proposals (
  id        text primary key,
  url       text not null,
  title     text,
  section   text,
  row_text  text,
  last_seen timestamptz not null default now()
);

create table if not exists commands (
  id          bigint generated always as identity primary key,
  type        text not null,
  args        jsonb not null default '{}'::jsonb,
  status      text not null default 'queued'
              check (status in ('queued', 'running', 'done', 'error')),
  result      jsonb,
  error       text,
  created_at  timestamptz not null default now(),
  started_at  timestamptz,
  finished_at timestamptz
);
create index if not exists commands_queue_idx on commands (status, created_at);

create table if not exists worker_status (
  id             text primary key,
  last_heartbeat timestamptz not null default now(),
  logged_in      boolean,
  info           jsonb
);

alter table jobs          enable row level security;
alter table threads       enable row level security;
alter table proposals     enable row level security;
alter table commands      enable row level security;
alter table worker_status enable row level security;

-- Atomically claim the oldest queued command (safe with multiple workers).
create or replace function claim_command()
returns setof commands
language sql
set search_path = public
as $$
  update commands
     set status = 'running', started_at = now()
   where id = (
     select id from commands
      where status = 'queued'
      order by created_at
      limit 1
      for update skip locked
   )
  returning *;
$$;

-- Requeue commands stuck in 'running' (e.g. worker crashed mid-command).
create or replace function requeue_stale_commands(older_than_seconds int default 300)
returns int
language sql
set search_path = public
as $$
  with r as (
    update commands set status = 'queued', started_at = null
     where status = 'running'
       and started_at < now() - make_interval(secs => older_than_seconds)
    returning 1
  )
  select count(*)::int from r;
$$;

-- Only the service role (worker / MCP server) may call these.
revoke execute on function claim_command() from public, anon, authenticated;
revoke execute on function requeue_stale_commands(int) from public, anon, authenticated;
