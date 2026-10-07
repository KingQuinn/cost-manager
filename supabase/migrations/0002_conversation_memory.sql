-- 3-layer conversation memory for the WhatsApp agent (architecture.md §7.7, changelog v1.5).
-- Run once in the Supabase SQL Editor on projects created before v1.5.
-- (Fresh projects get these from supabase/schema.sql.)

-- Layer 2 (conversation state): recent chat turns per member.
create table if not exists conversation_turns (
  id uuid primary key default gen_random_uuid(),
  member_id uuid references members(id) on delete cascade,
  role text not null check (role in ('user', 'assistant')),
  content text not null,
  intent text,
  created_at timestamptz default now()
);
create index if not exists idx_conversation_turns_member
  on conversation_turns(member_id, created_at);

-- Layer 2: conversation state — what the bot is waiting on right now
-- (e.g. a typed description after an unreadable photo, a yes/no on a proposed change).
create table if not exists member_state (
  member_id uuid primary key references members(id) on delete cascade,
  state jsonb not null default '{}'::jsonb,
  updated_at timestamptz default now()
);

-- Layer 3: long-term memory — durable facts/preferences + rolling conversation summary.
create table if not exists member_memories (
  id uuid primary key default gen_random_uuid(),
  member_id uuid references members(id) on delete cascade,
  kind text not null default 'fact' check (kind in ('fact', 'summary')),
  content text not null,
  created_at timestamptz default now()
);
create index if not exists idx_member_memories_member
  on member_memories(member_id, created_at);
