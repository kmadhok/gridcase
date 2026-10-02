-- Existing KPIs at the UTC hour/category grain; formulas match metrics_daily.
with base as (
    select
        hour,
        day,
        category,
        sum(usd) as volume_usd,
        count(*) as trades,
        count(distinct wallet) as active_traders,
        coalesce(sum(usd) filter (where size_band = '$1k+'), 0) as whale_volume,
        coalesce(sum(usd) filter (where side = 'buy'), 0) as buy_volume
    from {{ ref('fct_trades') }}
    group by hour, day, category
)

select
    hour,
    day,
    category,
    volume_usd,
    trades,
    active_traders,
    cast(whale_volume as double) / nullif(volume_usd, 0) as whale_share,
    cast(buy_volume as double) / nullif(volume_usd, 0) as buy_share
from base
