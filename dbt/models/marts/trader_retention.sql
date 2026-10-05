-- One row per first-day cohort and days since: how many of the wallets first seen that UTC day
-- traded again N days later. "First seen" is in our record, which began mid-day on its first
-- day, so that day is not a cohort (every wallet looks new on it), and cohorts in the first
-- week are flagged: some of their "new" wallets traded before collection began. The latest day
-- is still being collected, so it is neither a cohort nor an activity day.
with bounds as (
    select min(day) as record_start_day, max(day) - 1 as last_complete_day
    from {{ ref('fct_trades') }}
),

cohorts as (
    select w.wallet, w.first_day as cohort_day
    from {{ ref('dim_wallets') }} w, bounds b
    where w.first_day > b.record_start_day and w.first_day <= b.last_complete_day
),

cohort_sizes as (
    select cohort_day, count(*) as cohort_size from cohorts group by cohort_day
),

grid as (
    select s.cohort_day, s.cohort_size, cast(n.day_number as integer) as day_number
    from cohort_sizes s, bounds b,
        range(0, cast(b.last_complete_day - s.cohort_day as bigint) + 1) as n(day_number)
),

active as (
    select distinct wallet, day from {{ ref('fct_trades') }}
),

retained as (
    select c.cohort_day, cast(a.day - c.cohort_day as integer) as day_number,
        count(*) as retained_traders
    from cohorts c
    join active a on a.wallet = c.wallet and a.day >= c.cohort_day
    group by 1, 2
)

select
    g.cohort_day,
    g.day_number,
    g.cohort_day + g.day_number as activity_day,
    g.cohort_size,
    coalesce(r.retained_traders, 0) as retained_traders,
    cast(coalesce(r.retained_traders, 0) as double) / g.cohort_size as retention_rate,
    g.cohort_day >= b.record_start_day + 7 as warmed_up
from grid g
cross join bounds b
left join retained r using (cohort_day, day_number)
