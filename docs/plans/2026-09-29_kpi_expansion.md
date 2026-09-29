# MarketCase — KPI expansion plan (M7)

Status: proposed 2026-09-29, awaiting review. Nothing here is implemented.

## Why

The semantic layer answers today's five team KPIs. A PM will soon ask questions it cannot express:
how many markets are live, who holds the money, how much betting is on near-sure things, which odds
flipped. This plan adds those KPIs, two new grains, richer filters and a readable KPI catalog. Every
new KPI stays governed in `ontology/kpis.yaml`; the model still never writes SQL.

## What the first live hour says (2026-09-29 20:00–21:00 UTC, VPS data)

| Measure | Value | What it tells a PM |
|---|---|---|
| Volume | $3.83M | baseline |
| Trades / active traders | 59,251 / 7,451 | about 8 trades per trader |
| Markets traded (distinct) | 4,055 | a very long tail |
| Top 5 markets' share of volume | 24.8% | fairly spread |
| Top 10 wallets' share of volume | 26.1% | a few wallets matter a lot |
| Whale share ($1k+ trades) | 54.3% from 531 trades | half the money is <1% of trades |
| "Sure thing" share (price > 95¢) | 40.8% | huge: people buying near-certain outcomes |
| Long-shot share (price < 10¢) | 12.8% of trades, 0.8% of volume | lottery tickets: many, tiny |
| Buy share of volume | 69.7% | money flowing in vs taking profit |
| Outcomes crossing 50¢ in the hour | 249 | odds flips, mostly live games |
| Crypto "Up or Down" share of crypto volume | 92.7% | crypto volume is 5-minute bets, not views on crypto |

Two lessons shape the plan. The flip count is dominated by 5-minute crypto "Up or Down" markets,
which flip by design, so it needs a filter that separates those markets. And shares, not totals,
carry the story. A share needs a numerator and a denominator from the same query.

## Part 1 — the KPIs you listed

| KPI | Plain definition | How it is computed | Compiler change |
|---|---|---|---|
| `markets_traded` | Markets with at least one trade | `count(DISTINCT condition_id)` | expose `condition_id` in the trade rows |
| `volume_share` | This slice's share of all volume (e.g. Sports' share) | filters apply to the numerator only | new "share" KPI kind (below) |
| `whale_share` | Share of volume from trades of $1k or more | `volume_usd` where size band is `$1k+`, over `volume_usd` | measure with a governed `where` |
| `trades_per_trader` | Average trades per active trader | `trades / active_traders` (derived) | none |
| `new_markets` | Markets that got their first trade in our record | first-seen per market, like `new_traders` | a market first-seen step; 7-day warm-up |

`new_markets` uses first trade seen, not Polymarket's creation date. We only look up markets that
trade, so a creation date would miss markets that never trade. It also keeps the KPI leak-free.

## Part 2 — "sexier" KPIs

Ranked by how often a PM would care and whether the trade feed can support them honestly.

1. **`sure_thing_share`**: the share of volume on outcomes priced above 95¢. It was 41% in the first
   hour. A rise means traders are parking money in near-resolved markets rather than taking views.
2. **`odds_flips`**: event outcomes whose price crossed 50¢ within the hour, with at least 5 trades.
   Recurring 5-minute markets are excluded. This is the "something just happened" KPI: upsets, news,
   live-game turns.
3. **`whale_count`**: wallets with at least $10k traded in the bucket. It pairs with `whale_share`
   to show whether a few wallets or many are behind a move.
4. **`top_market_share`**: the biggest market's share of volume. It works as a concentration alarm
   ("one match is eating the platform").
5. **`buy_share`**: buys as a share of volume. A drop means money is leaving positions.
6. **`longshot_share`**: the share of trades on outcomes under 10¢. It is a lottery-ticket gauge and
   tracks retail excitement.
7. **`recurring_share`**: the share of volume in recurring 5-minute and hourly markets. It shows how
   much of Polymarket is a casino loop versus event forecasting.

Rejected, with reasons:
- **Upsets and smart money.** Both need resolution outcomes and wallet profit and loss. The trade
  feed does not carry them. They could be revisited with the resolution API later.
- **Live-match share.** It needs game start times, and we have no source for them.
- **Day-1 retention.** `returning_share` already covers most of it. A true cohort KPI needs daily
  grains and 2 days of warm-up, so it is deferred to the daily digest in M6.

## Part 3 — compiler changes (`gridcase/semantic/kpis.py`)

Each change is one mechanism, reused by several KPIs.

1. **More columns in the trade rows.** Add `condition_id`, `asset`, `side` and the market title to
   the per-trade step. This enables `markets_traded` and the per-market steps.
2. **Measures with a governed `where`.** A measure may carry a dimension condition, for example
   `whale_volume: {sql: sum(usd), where: {size_band: "$1k+"}}`. This covers `whale_share`,
   `sure_thing_share`, `buy_share`, `longshot_share` and `recurring_share` as derived ratios.
3. **Share KPIs.** Add `kind: share` with a `of: volume_usd` field. The query's filters become a
   `FILTER (WHERE …)` on the numerator instead of a `WHERE` on everything. This yields "Sports'
   share of volume" from the existing category filter.
4. **Two-step measures.** A measure may first aggregate per market or per wallet, then across them.
   It is written as governed SQL in two parts, `inner` and `outer`, with the level named in `per:`.
   This covers `odds_flips`, `whale_count`, `top_market_share` and `new_markets`.
5. **New dimension `market_kind`.** Titles containing "Up or Down" map to `recurring`, all others to
   `event`. The rule lives in the ontology.
6. **Grains `week` and `month`.** Extend the grain values and `date_trunc`. Month-to-date is a
   window preset (`window: "mtd"`: from the first of the month to `as_of`), not a grain. The hourly
   alarm stays hourly; the new grains serve questions and the daily digest.
7. **Richer filters.** A filter value may be a list (SQL `IN`). Two governed numeric fields,
   `trade_usd` and `price`, accept `>=` and `<`. Every value is still validated and quoted by the
   compiler. The model's filter shape becomes `{dimension, op: eq|in|gte|lt, values: [...]}`.

## Part 4 — watch, investigation, UI

- **Watched series** grow from 13 to 18. The new ones are `markets_traded`, `whale_share`,
  `sure_thing_share`, `odds_flips` and `top_market_share`, all overall. The others stay available for
  questions and the catalog. Alarms stay capped at 10 paid investigations a day. Raw trades are kept,
  so new series start with full history and skip the learning period.
- **Investigations** reuse the existing drivers. Breakdowns of share KPIs use the existing
  weight × value split for derived KPIs. One new breakdown, `by.kind` (recurring versus event), is
  added because it explains most crypto moves. No new hypothesis template is proposed.
- **KPI catalog page** at `#/kpis`, fed by a new `/api/catalog` endpoint:
  - one card per KPI with its plain definition, unit and last-hour value
  - whether it is watched, and its status
  - the formula in words ("volume from trades of $1k or more, divided by all volume")
  - the governed SQL fragment behind a "show the definition" toggle
  - A link from the Now page header and from each case's KPI name.
- **Labels** in the web labels file, the question-box glossary and the explainer's units.

## Order of work and tests

Each step writes its test red first, then implements, then commits.

1. **M7a, compiler.** Exit test `tests/exit/test_m7a.py` on the synthetic store:
   - `markets_traded` equals a hand count
   - `whale_share` and other `where` ratios match hand computation, and stay between 0 and 1
   - a share KPI's segments across all categories sum to 1
   - `odds_flips` counts a synthetic event flip and ignores a recurring market's flip
   - `new_markets` counts only markets first seen in the bucket
   - week, month and month-to-date totals equal the sum of their hours
   - an `IN` filter equals the sum of the single-value filters
   - a `trade_usd >= 1000` filter equals the `$1k+` size band
   - unknown values and bad operators are rejected
   - the same query gives identical SQL twice
   - nothing collected after `as_of` is used
2. **M7b, KPIs and watch.** Exit test `test_m7b.py`:
   - every KPI in the YAML compiles at every grain
   - the 18 watched series produce statuses
   - an injected whale surge alarms `whale_share`, and the investigation names the whale band
   - an injected event flip alarms `odds_flips`
3. **M7c, catalog.** Tests for the `/api/catalog` shape plus Vitest for the page:
   - every KPI has a plain definition
   - no raw SQL is visible until toggled
   - the plain-language test covers the page
4. **Deploy to the VPS**, then check that the new tiles show real values.

Performance check at M7a: time the hourly watch on the live store (28 days × 18 series). If it
exceeds 60 seconds, add an hourly rollup table before M7b. This is flagged, not assumed.

## Review items (for `docs/DECISIONS.md`)

- REVIEW: `new_markets` means "first trade in our record", not Polymarket's creation date.
- REVIEW: "Recurring" means a title containing "Up or Down". This is a text rule and may miss other
  recurring formats.
- REVIEW: thresholds are 95¢ for a sure thing, 10¢ for a long shot, 50¢ ± 5¢ with 5 trades for a
  flip, and $10k for a whale wallet.
- REVIEW: five new watched series (18 total). More alarms compete for the 10 paid investigations a
  day.
