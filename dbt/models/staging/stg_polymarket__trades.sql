{#- One row per fill, kept once. trade_id = md5 of (transactionHash, asset, side, size, price),
    exactly as gridcase/ingest/polymarket_load.py; the earliest collected copy wins.
    Incremental: a run reads only files from two hours before the newest trade it holds (the
    collector keeps appending to the current hour's file) and skips trades already kept. -#}
{{ config(materialized='incremental', incremental_strategy='append') }}

{% set newer_than = none %}
{% if is_incremental() %}
  {% set q %}
    select strftime(max(collected_at) - interval 2 hour, '%Y/%m/%d/%H') from {{ this }}
  {% endset %}
  {% set newer_than = run_query(q).columns[0].values()[0] %}
{% endif %}

with raw as (
    select
        md5(concat_ws('|', transactionHash, asset, side, cast(size as varchar),
                      cast(price as varchar))) as trade_id,
        cast(to_timestamp(timestamp) as timestamp) as ts,
        proxyWallet as wallet,
        side,
        size,
        price,
        size * price as usd,
        asset,
        conditionId as condition_id,
        slug as market_slug,
        nullif(eventSlug, '') as event_slug,
        outcome,
        title,
        transactionHash as tx_hash,
        cast(cast(collected_at as timestamptz) as timestamp) as collected_at,
        filename as source_file
    from {{ raw_trades(newer_than) }}
    where proxyWallet is not null and conditionId is not null
),

kept as (
    select * from raw
    qualify row_number() over (partition by trade_id order by collected_at, source_file) = 1
)

select * from kept
{% if is_incremental() %}
where trade_id not in (
    select trade_id from {{ this }} where ts >= (select min(ts) from kept)
)
{% endif %}
