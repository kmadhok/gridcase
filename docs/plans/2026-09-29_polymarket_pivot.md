# Plan: GridCase becomes a live product-analytics agent for Polymarket

Status: approved direction 2026-09-29 (user chose Polymarket; rework this repo in place).
Collector live on the VPS since 2026-09-29 19:50 UTC.

## The product in one paragraph

We play Polymarket's growth-analytics team. A handful of KPIs the team owns are defined once in
the semantic layer. Every hour the system checks each KPI against what is normal for that hour;
when one leaves its range, the agent investigates the way an analyst would — slice the KPI by
category, market, trade size and new-vs-returning traders, find the segment that moved, and
write a short answer where every number links to its query. A PM can also ask a question in
plain words ("why did sports volume jump this afternoon?") and get the same kind of answer.
No models: comparisons, breakdowns and shares. The value is visible the moment it fires:
"volume is up 3.1x; 72% of it is one market, the England match, and 40% of those traders are
new."

## Data

- **Trades**: `data-api.polymarket.com/trades`, public, no key. Every trade: wallet, side,
  size, price (so dollars = size x price), market, outcome, time. ~900-1,300 trades a minute.
  The feed only reaches back ~10,000 trades (~11 minutes) and is CDN-cached for 5 minutes, so
  history exists only because the collector (`gridcase collect`, user service
  `gridcase-collector`) saves it: 10,000-trade pages every 4 minutes, raw gzip JSONL per hour,
  with a gap check.
- **Markets**: `gamma-api.polymarket.com` for each market's event, category and tags (cached).

## KPIs (the governed layer; first version)

| KPI | Definition | Why a PM cares |
|---|---|---|
| Trading volume | sum of size x price per hour ($) | the platform's revenue driver |
| Active traders | distinct wallets trading in the hour | engagement |
| New traders | wallets never seen before, per hour | acquisition / activation |
| Average trade size | volume / trades | who is trading (retail vs whales) |
| Returning-trader share | share of active traders seen on an earlier day | retention |

Dimensions to slice by: category (Sports, Politics, Crypto, Culture, Economy, Tech & Science,
Weather, Other), market / event, trade size band (<$10, $10-100, $100-1k, $1k+), trader type
(new / returning), side (buy / sell), odds band (longshot <20%, toss-up, favourite >80%).

## The alarm (simple statistics, as for the grid)

Each KPI, hour by hour: compare with the same hour of the day over the previous days (up to 28;
at least 3 before alarming — until then the page says "learning what normal looks like"). Alarm
when the KPI is more than 3 standard deviations away for 2 hours in a row, or 5 for one hour.
Volume is heavy-tailed, so it is judged on a log scale (a 3x jump and a 3x drop count the
same). Config in `ontology/alarm.yaml`, receipts with SQL, as today.

## The investigation ("why?")

Tools (pure functions of inputs and as_of, SQL from the semantic layer only):
- `kpi.compare` — the KPI in the window vs the same hours on previous days.
- `kpi.breakdown` — the change split by one dimension: each segment's before/after and its
  share of the total change; for averages and rates, how much is mix (more of a segment) vs
  rate (the segment itself changed).
- `kpi.top_markets` — the markets that moved most.
- `kpi.drill` — breakdown within the top segment of a previous breakdown (two levels max).

The model's goal, given to it verbatim: *explain the breach in one sentence a PM can act on,
naming the segment and market that moved and whether it is concentrated or broad-based; then
say what the product team should check.* It chooses which dimensions to slice and in what
order (the validator requires at least two dimensions and caps the steps); the verifier checks
every number against the receipts, as today.

## The goal loop (beyond alarms)

Standing questions per KPI, answered in a short daily digest even when nothing alarms, e.g.
"Which category gained share of volume this week?", "Did yesterday's new traders come back
today?", "Are whales or retail driving the change in average trade size?"

## Pages

- **Now**: the KPI board — each KPI's last hours vs its normal band, status, and live
  investigations (replaces the map).
- **Investigation**: the existing case page, reworded for segments (reused).
- **Ask**: the evidence-only question box (reused), now over live data.

## Order of work

1. Collector (done).
2. Load raw trades into the store: `trades` fact table and `markets` dimension (category from
   the gamma API), refreshed every few minutes by the server.
3. Semantic layer v2: measures and dimensions over the fact table; compiler emits SQL.
4. KPI alarm generalised to the new KPIs; watch opens investigations.
5. Breakdown tools, planner goal, explainer wording; question box context.
6. Now page; case page wording.
7. The power-grid version is kept on the `power-grid` branch / tag `v1-power-grid`; grid ingest
   and watch are switched off on the VPS when step 4 goes live.

Alarms need 3 days of collected history; steps 2-6 are built while it accumulates, and the
question box can answer questions from day one.
