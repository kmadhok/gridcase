-- The daily mart loses and invents nothing: its volume and trade counts equal the fact table's.
-- Volume is compared to one part in a trillion: summing millions of fills in a different order
-- moves the last digits (a millionth of a dollar on $57M was seen on real data).
with f as (select day, sum(usd) as v, count(*) as n from {{ ref('fct_trades') }} group by day)
select f.day from f join {{ ref('metrics_daily') }} m using (day)
where abs(f.v - m.volume_usd) > 1e-12 * abs(f.v) or f.n != m.trades
