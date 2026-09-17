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
  needs_review boolean default false
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
create index idx_line_items_receipt on line_items(receipt_id);
create index idx_line_items_category on line_items(category_id);
