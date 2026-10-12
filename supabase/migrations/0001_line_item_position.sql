-- Fixes "item N" corrections silently landing on the wrong row: line_items
-- had no explicit ordering column, so SQL's row order wasn't guaranteed to
-- match the numbering shown in chat. Run this once against the project
-- created before this fix (fresh setups get this column from schema.sql).
--
-- Existing rows all default to position=1 since there's no reliable prior
-- order to backfill from — fine for now since only benchmark/test receipts
-- predate this migration. New receipts get correct positions going forward.

alter table line_items add column if not exists position integer not null default 1;

drop index if exists idx_line_items_receipt;
create index idx_line_items_receipt on line_items(receipt_id, position);
