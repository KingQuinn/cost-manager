-- Family expense tracker schema — mirrors architecture.md §8.2.
-- Run this once in the Supabase SQL Editor (Project > SQL Editor > New query)
-- against a fresh project. Category seeding is handled automatically by the
-- app on first connect (app/db/__init__.py:get_store), not here.

create table families (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  created_at timestamptz default now()
);

create table members (
  id uuid primary key default gen_random_uuid(),
  family_id uuid references families(id) on delete cascade,
  name text not null,
  phone_number text unique not null,
  created_at timestamptz default now()
);

create table categories (
  id uuid primary key default gen_random_uuid(),
  name text not null unique,
  keywords text[] default '{}'
);

create table receipts (
  id uuid primary key default gen_random_uuid(),
  member_id uuid references members(id) on delete cascade,
  vendor text,
  purchase_date date,
  total numeric(12,2),
  image_url text,
  status text not null default 'pending'
    check (status in ('pending', 'confirmed', 'needs_review')),
  raw_extraction jsonb,
  created_at timestamptz default now()
);

create table line_items (
  id uuid primary key default gen_random_uuid(),
  receipt_id uuid references receipts(id) on delete cascade,
  name text not null,
  quantity numeric default 1,
  price numeric(12,2) not null,
  category_id uuid references categories(id),
  confidence numeric(3,2),
  needs_review boolean default false,
  -- 1-based position within the receipt. The chat UX's "item N" numbering
  -- (confirmation message, corrections) refers to this column, not row/
  -- insertion order, which SQL doesn't guarantee without an ORDER BY.
  position integer not null default 1
);

create table clarifications (
  id uuid primary key default gen_random_uuid(),
  receipt_id uuid references receipts(id) on delete cascade,
  line_item_id uuid references line_items(id),
  question text not null,
  resolved boolean default false,
  answer text,
  created_at timestamptz default now()
);

create table weekly_summaries (
  id uuid primary key default gen_random_uuid(),
  family_id uuid references families(id),
  week_start date,
  week_end date,
  summary_text text,
  total_spend numeric(12,2),
  created_at timestamptz default now()
);

create index idx_members_phone on members(phone_number);
create index idx_receipts_member on receipts(member_id, created_at);
create index idx_line_items_receipt on line_items(receipt_id, position);
create index idx_line_items_category on line_items(category_id);

-- Conversation memory (architecture.md §7.7). Same as migrations/0002.

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

-- Spending totals for chat questions (architecture.md §7.6). Same as migrations/0003.
create or replace function spending_breakdown(
  p_family_id uuid,
  p_member_id uuid default null,    -- null = whole family
  p_start date default null,
  p_end date default null,
  p_category_id uuid default null,
  p_item text default null          -- case-insensitive substring of the item name
) returns jsonb
language sql
stable
as $$
  with dated as (
    select r.id, r.member_id, r.vendor, r.status, r.total,
           coalesce(r.purchase_date, (r.created_at at time zone 'utc')::date) as spend_date
    from receipts r
    join members m on m.id = r.member_id
    where m.family_id = p_family_id
      and (p_member_id is null or r.member_id = p_member_id)
  ),
  in_range as (
    select * from dated
    where (p_start is null or spend_date >= p_start)
      and (p_end is null or spend_date <= p_end)
  ),
  items as (
    select li.price,
           li.name as item,
           coalesce(c.name, 'Other') as category,
           coalesce(mb.name, 'Unknown') as member,
           coalesce(d.vendor, 'Unknown vendor') as vendor,
           d.id as receipt_id,
           d.spend_date
    from in_range d
    join line_items li on li.receipt_id = d.id
    left join categories c on c.id = li.category_id
    left join members mb on mb.id = d.member_id
    where d.status = 'confirmed'
      and (p_category_id is null or li.category_id = p_category_id)
      and (p_item is null or position(lower(p_item) in lower(li.name)) > 0)
  )
  select jsonb_build_object(
    'total', coalesce((select sum(price) from items), 0),
    'confirmed_receipts', (select count(distinct receipt_id) from items),
    'by_category', coalesce((
      select jsonb_agg(jsonb_build_object('name', category, 'amount', amt) order by amt desc, category)
      from (select category, sum(price) as amt from items group by category) s), '[]'::jsonb),
    'by_member', coalesce((
      select jsonb_agg(jsonb_build_object('name', member, 'amount', amt) order by amt desc, member)
      from (select member, sum(price) as amt from items group by member) s), '[]'::jsonb),
    'top_vendors', coalesce((
      select jsonb_agg(jsonb_build_object('name', vendor, 'amount', amt, 'receipts', n) order by amt desc, vendor)
      from (select vendor, sum(price) as amt, count(distinct receipt_id) as n
            from items group by vendor order by amt desc, vendor limit 3) s), '[]'::jsonb),
    'top_items', coalesce((
      select jsonb_agg(jsonb_build_object('item', item, 'amount', price, 'category', category,
                                          'vendor', vendor, 'date', spend_date) order by price desc, item)
      from (select * from items order by price desc, item limit 5) s), '[]'::jsonb),
    'pending_receipts', (select count(*) from in_range where status = 'needs_review'),
    'pending_amount', coalesce((select sum(total) from in_range where status = 'needs_review'), 0)
  );
$$;
