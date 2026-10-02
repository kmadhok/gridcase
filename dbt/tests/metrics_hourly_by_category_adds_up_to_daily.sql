-- Regrouping can move floating-point sums in the last digits; tolerate one part in a trillion.
with hourly as (
    select day, sum(volume_usd) as volume_usd, sum(trades) as trades
    from {{ ref('metrics_hourly_by_category') }}
    group by day
)
select coalesce(h.day, d.day) as day
from hourly h full outer join {{ ref('metrics_daily') }} d using (day)
where h.day is null or d.day is null
    or abs(h.volume_usd - d.volume_usd) > 1e-12 * abs(d.volume_usd)
    or h.trades != d.trades
