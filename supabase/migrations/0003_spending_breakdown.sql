-- Spending totals computed in Postgres (architecture.md §7.6, changelog v1.6).
-- The conversation agent answers "how much did we spend on X?" from this
-- function's output only: Gemini phrases the reply, it never adds numbers up.
-- Run once in the Supabase SQL Editor (fresh projects get it from schema.sql).
--
-- Only `confirmed` receipts count. `needs_review` receipts are reported
-- separately (count + receipt total) so the answer can say they're excluded.
-- Dates use purchase_date, falling back to the day the receipt was logged.

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
