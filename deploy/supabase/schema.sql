-- Physics Witness: Supabase setup. Run once in the Supabase dashboard:
-- SQL Editor -> New query -> paste this file -> Run. Safe to run again.

-- One row per claim. `meta` holds status, progress and the card stats the home
-- page shows; `report` holds the full evidence report (verdicts, frames,
-- observations, rounds). status/title/created_at are copied out for querying.
create table if not exists public.claims (
  id          text primary key,
  created_at  timestamptz default now(),
  updated_at  timestamptz default now(),
  status      text,
  title       text,
  meta        jsonb not null default '{}'::jsonb,
  report      jsonb
);
create index if not exists claims_created_at on public.claims (created_at desc);
create index if not exists claims_status on public.claims (status);

-- One row per statement verdict, rewritten whenever a claim is (re)judged.
create table if not exists public.verdicts (
  id          bigint generated always as identity primary key,
  claim_id    text not null references public.claims (id) on delete cascade,
  claim_key   text,
  statement   text,
  verdict     text check (verdict in ('supported', 'contradicted', 'cant_tell')),
  round       integer default 0,
  reasoning   text,
  evidence    jsonb default '[]'::jsonb,
  created_at  timestamptz default now()
);
create index if not exists verdicts_claim on public.verdicts (claim_id);
create index if not exists verdicts_verdict on public.verdicts (verdict);

-- Shared cache of model replies, keyed by a hash of the exact request (model,
-- prompt, frames). Same request -> same reply, no tokens spent.
create table if not exists public.model_cache (
  namespace   text not null,
  key         text not null,
  value       jsonb not null,
  created_at  timestamptz default now(),
  primary key (namespace, key)
);

-- Lock the tables: the server uses the service_role key, which bypasses Row
-- Level Security; with RLS on and no policies, the public anon key gets nothing.
alter table public.claims enable row level security;
alter table public.verdicts enable row level security;
alter table public.model_cache enable row level security;

-- Private bucket for videos and evidence frames (served through signed URLs).
-- 200 MB per file to match the upload limit; your project's global limit may be
-- lower (Settings -> Storage), so keep clips short.
insert into storage.buckets (id, name, public, file_size_limit)
values ('claims', 'claims', false, 209715200)
on conflict (id) do nothing;

-- Handy for the demo: verdict counts per claim.
create or replace view public.claim_verdict_summary
with (security_invoker = true) as
select c.id, c.title, c.status, c.created_at,
       count(*) filter (where v.verdict = 'supported')    as supported,
       count(*) filter (where v.verdict = 'contradicted') as contradicted,
       count(*) filter (where v.verdict = 'cant_tell')    as cant_tell
from public.claims c
left join public.verdicts v on v.claim_id = c.id
group by c.id;
