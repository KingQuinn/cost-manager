-- v1.8: Drinks becomes its own category, split out of Groceries (architecture.md changelog v1.8).
-- Run once in the Supabase SQL Editor. Safe to re-run.
--
-- 1. Adds the Drinks category with its starting keywords.
-- 2. Moves existing line items whose name has a drink keyword at the start of a
--    word (e.g. "AMERICAN COLA", "LIPTON ICE TEA PEACH", "CLAY FURA DRINK") to Drinks.
-- 3. Cleans the keyword lists the bot learned: drops words under 3 characters
--    ("k", "g", "w"), sizes ("500ml", "45cl"), filler ("per", "pack") and
--    drink words that ended up in other categories. Those matched far too much.
--
-- To preview step 2 before running, select this on its own:
--   select name, (select name from categories c where c.id = li.category_id) as now_in
--   from line_items li
--   where li.name ~* '(^|[^a-z0-9])(bottled water|yoghurt drink|energy drink|table water|power horse|tonic water|schweppes|eva water|pure life|milkshake|la casera|lucozade|aquafina|iced tea|smoothie|guinness|heineken|red bull|predator|mirinda|maltina|chapman|ice tea|chivita|sprite|5alive|trophy|whisky|drink|juice|fanta|pepsi|malta|capri|lager|stout|vodka|cola|coke|zobo|kunu|fura|beer|wine|bigi|soda|7up|h2o|h20)';

insert into categories (name, keywords)
values ('Drinks', array['drink', 'juice', 'cola', 'coke', 'fanta', 'sprite', 'pepsi', '7up', 'mirinda', 'maltina', 'malta', 'schweppes', 'chapman', 'zobo', 'kunu', 'fura', 'lucozade', 'aquafina', 'h2o', 'h20', 'table water', 'bottled water', 'eva water', 'pure life', 'ice tea', 'iced tea', '5alive', 'chivita', 'capri', 'smoothie', 'milkshake', 'yoghurt drink', 'beer', 'lager', 'stout', 'guinness', 'heineken', 'trophy', 'wine', 'vodka', 'whisky', 'red bull', 'energy drink', 'predator', 'power horse', 'bigi', 'la casera', 'soda', 'tonic water'])
on conflict (name) do update
  set keywords = array(select distinct unnest(categories.keywords || excluded.keywords));

update line_items li
set category_id = (select id from categories where name = 'Drinks')
where li.name ~* '(^|[^a-z0-9])(bottled water|yoghurt drink|energy drink|table water|power horse|tonic water|schweppes|eva water|pure life|milkshake|la casera|lucozade|aquafina|iced tea|smoothie|guinness|heineken|red bull|predator|mirinda|maltina|chapman|ice tea|chivita|sprite|5alive|trophy|whisky|drink|juice|fanta|pepsi|malta|capri|lager|stout|vodka|cola|coke|zobo|kunu|fura|beer|wine|bigi|soda|7up|h2o|h20)'
  and li.category_id is distinct from (select id from categories where name = 'Drinks');

update categories c
set keywords = coalesce((
  select array_agg(k order by ord)
  from unnest(c.keywords) with ordinality as t(k, ord)
  where length(k) >= 3
    and k !~* '^[0-9]+([.,][0-9]+)?(ml|l|cl|g|kg|mg|oz|pcs|pc|x)?$'
    and k !~* '^#?[0-9]+[a-z]{0,3}$'
    and lower(k) <> all (array['and', 'assorted', 'big', 'black', 'blue', 'bottle', 'can', 'carton', 'each', 'for', 'fresh', 'green', 'kilo', 'kilogram', 'large', 'medium', 'mini', 'new', 'original', 'pack', 'pcs', 'per', 'pet', 'piece', 'pkt', 'plus', 'real', 'red', 'sachet', 'small', 'special', 'the', 'tin', 'var', 'varieties', 'variety', 'white', 'with'])
    and (c.name = 'Drinks' or lower(k) <> all (array['5alive', '7up', 'aquafina', 'beer', 'bigi', 'boost', 'capri', 'chapman', 'chivita', 'clay', 'coke', 'cola', 'drink', 'fanta', 'fura', 'guinness', 'h20', 'h2o', 'heineken', 'ice', 'juice', 'kunu', 'lager', 'lawi', 'lipton', 'lucozade', 'malta', 'maltina', 'milkshake', 'mirinda', 'peach', 'pepsi', 'predator', 'schweppes', 'smoothie', 'soda', 'sprite', 'stout', 'tea', 'trophy', 'vodka', 'whisky', 'wine', 'zer', 'zobo']))
), '{}');

notify pgrst, 'reload schema';
