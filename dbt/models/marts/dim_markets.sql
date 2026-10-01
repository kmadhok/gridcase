-- One row per market that traded. category and market_kind follow the semantic layer's
-- dimensions (ontology/kpis.yaml: category, market, market_kind).
with traded as (
    select
        condition_id,
        max(title) as title,
        max(market_slug) as market_slug,
        min(ts) as first_traded_at,
        max(ts) as last_traded_at
    from {{ ref('stg_polymarket__trades') }}
    group by condition_id
)

select
    t.condition_id,
    t.title,
    coalesce(nullif(m.event_title, ''), t.title) as market_name,
    coalesce(m.category, 'Other') as category,
    case when t.title ilike '%up or down%' then 'recurring' else 'event' end as market_kind,
    t.market_slug,
    m.event_slug,
    t.first_traded_at,
    t.last_traded_at
from traded t
left join {{ ref('stg_polymarket__markets') }} m using (condition_id)
