-- A cohort is exactly the day's new traders, all of them active on day 0.
select r.cohort_day
from {{ ref('trader_retention') }} r
left join {{ ref('metrics_daily') }} m on m.day = r.cohort_day
where r.day_number = 0
    and (m.day is null or r.cohort_size != m.new_traders or r.retained_traders != r.cohort_size)
