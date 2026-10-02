# MarketCase in Hex

Use a Hex Community account and a MotherDuck account. In MotherDuck settings, create an
access token. Put it in the VPS repo's `.env` as `MOTHERDUCK_TOKEN`; keep it out of notebooks
and SQL. Run `uv run gridcase warehouse publish` after a successful dbt build, or let the
hourly Airflow DAG publish. Without the token, publishing skips successfully.

In Hex, add a data connection of type **MotherDuck**, enter the token in the connection's
credential field, and choose database **marketcase**. Create a project using that connection.
Add these three SQL cells and a chart after each. All dates and hours are UTC; the current
day and hour may be incomplete. Data reflects the most recent successful publication.

## Daily totals

```sql
select day, volume_usd, trades, active_traders
from marketcase.main.metrics_daily
order by day;
```

Chart: daily volume as bars; show trades and active traders in separate line charts so their
different scales stay readable.

## Volume share by category each hour

```sql
select hour, category,
       volume_usd / nullif(sum(volume_usd) over (partition by hour), 0) as volume_share
from marketcase.main.metrics_hourly_by_category
order by hour, category;
```

Chart: stacked area, hour on the x-axis, volume_share formatted as percent on the y-axis,
category as the series.

## Whale share and buy share over time

```sql
select hour, category, whale_share, buy_share
from marketcase.main.metrics_hourly_by_category
order by hour, category;
```

Chart: two line charts, one for each share, with category as the series and a percentage
y-axis. Whale share is the share of dollars from fills of at least $1,000; buy share is the
share of dollars from buys. Zero-volume hours have null shares. Distinct active traders
cannot be added across categories or hours.

Only the three marts are published. `dim_markets` has market names, but without trades or a
market-level volume mart it cannot produce a top-20-markets-by-volume chart. Use the share
charts above instead.
