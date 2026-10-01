-- Market metadata the server looked up in Polymarket's public API (category from
-- ontology/categories.yaml), exported to Parquet after each refresh.
select condition_id, question, event_slug, event_title, category, tags, fetched_at
from {{ market_exports() }}
