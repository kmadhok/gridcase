-- One row per wallet. "New" means new to our record (collection began 2026-09-29 19:37 UTC).
select
    wallet,
    min(ts) as first_seen_at,
    cast(min(ts) as date) as first_day,
    count(*) as trades,
    sum(usd) as volume_usd
from {{ ref('stg_polymarket__trades') }}
group by wallet
