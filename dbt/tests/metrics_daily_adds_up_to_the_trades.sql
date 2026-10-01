-- The daily mart loses and invents nothing: its volume and trade counts equal the fact table's.
with f as (select day, sum(usd) as v, count(*) as n from {{ ref('fct_trades') }} group by day)
select f.day from f join {{ ref('metrics_daily') }} m using (day)
where abs(f.v - m.volume_usd) > 1e-6 or f.n != m.trades
