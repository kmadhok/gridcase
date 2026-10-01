-- One row per UTC day. Each column is the governed KPI of the same id in ontology/kpis.yaml;
-- tests/exit/test_m9.py checks every value against the semantic layer's own SQL.
with base as (
    select
        day,
        sum(usd) as volume_usd,
        count(*) as trades,
        count(distinct wallet) as active_traders,
        count(distinct wallet) filter (where is_new_trader) as new_traders,
        count(distinct condition_id) as markets_traded,
        coalesce(sum(usd) filter (where size_band = '$1k+'), 0) as whale_volume,
        coalesce(sum(usd) filter (where price > 0.95), 0) as sure_thing_volume,
        coalesce(sum(usd) filter (where side = 'buy'), 0) as buy_volume,
        count(*) filter (where price < 0.10) as longshot_trades,
        coalesce(sum(usd) filter (where market_kind = 'recurring'), 0) as recurring_volume
    from {{ ref('fct_trades') }}
    group by day
)

select
    day,
    volume_usd,
    trades,
    active_traders,
    new_traders,
    markets_traded,
    cast(volume_usd as double) / nullif(trades, 0) as avg_trade_usd,
    cast(trades as double) / nullif(active_traders, 0) as trades_per_trader,
    cast(whale_volume as double) / nullif(volume_usd, 0) as whale_share,
    cast(sure_thing_volume as double) / nullif(volume_usd, 0) as sure_thing_share,
    cast(buy_volume as double) / nullif(volume_usd, 0) as buy_share,
    cast(longshot_trades as double) / nullif(trades, 0) as longshot_share,
    cast(recurring_volume as double) / nullif(volume_usd, 0) as recurring_share,
    1 - cast(new_traders as double) / nullif(active_traders, 0) as returning_share
from base
