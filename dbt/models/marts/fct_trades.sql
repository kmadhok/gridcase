-- One row per fill, with the semantic layer's dimensions resolved (ontology/kpis.yaml).
{{ config(materialized='incremental', unique_key='trade_id', incremental_strategy='delete+insert') }}

with resolved as (
select
    t.trade_id,
    t.ts,
    cast(t.ts as date) as day,
    date_trunc('hour', t.ts) as hour,
    t.wallet,
    t.condition_id,
    t.asset,
    t.outcome,
    lower(t.side) as side,
    t.size,
    t.price,
    t.usd,
    m.category,
    m.market_name,
    m.market_kind,
    case when t.usd < 10 then 'under $10' when t.usd < 100 then '$10-100'
         when t.usd < 1000 then '$100-1k' else '$1k+' end as size_band,
    case when t.price < 0.2 then 'longshot' when t.price > 0.8 then 'favourite'
         else 'toss-up' end as odds_band,
    w.first_day = cast(t.ts as date) as is_new_trader,
    t.collected_at
from {{ ref('stg_polymarket__trades') }} t
join {{ ref('dim_wallets') }} w using (wallet)
join {{ ref('dim_markets') }} m using (condition_id)
)

select * from resolved
{% if is_incremental() %}
-- Compare all resolved values, including NULLs (EXCEPT treats two NULLs as equal).
-- Old trades can arrive late and dimension corrections can affect any historical fact,
-- so scan the source and target rather than filtering on event/collection timestamps.
-- Only missing or changed rows reach delete+insert; unchanged runs write no fact rows.
except
select
    trade_id, ts, day, hour, wallet, condition_id, asset, outcome, side, size, price, usd,
    category, market_name, market_kind, size_band, odds_band, is_new_trader, collected_at
from {{ this }}
{% endif %}
