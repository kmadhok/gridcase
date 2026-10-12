# MarketCase data feasibility: can we RL-tune a small model on investigation tasks?

Audit run 2026-10-11/12 on branch `claude/polymarket-data-feasibility-anj2kc`. Code: `experiments/feasibility/` (a separate module; the collector and the product are untouched). Every number below comes from a script in that folder; §6 says how to rerun each one and what is left for the VPS.

## Summary

- **Our own record is too small to train on.** It is 12 days of trades (from 2026-09-29 19:36 UTC), with no resolutions stored. Only about 9 days of it can start a spike that has 72 hours of follow-up.
- **The public APIs can backfill what we need, and it costs nothing.**
  - Gamma lists every closed market with its settled outcome.
  - CLOB `/prices-history` serves **1-minute prices back to at least mid-2023**, in 15-day windows.
  - data-api v2 serves each market's **full trade history**. The repo's "never assume a backfill is possible" was true of the v1 API, not of v2.
- **Gradable events are plentiful, even after strict filtering.**
  - The 6-month window is markets that closed 2026-04-11 → 2026-10-11, non-recurring, resolved binary.
  - **Spikes:** at ≥ 10 points within 6 hours on markets with ≥ $100k volume, I estimate **~27,000 event clusters** (90% interval 17k–39k).
  - **After merging cross-event news clusters:** about 21,000.
  - **Directly observed, no extrapolation:** the $1M+ markets trading in **August 2026 alone** gave **291 independent clusters**.
  - **Class balance is usable:** 58% held, 42% reverted at +72 h.
  - **Brier-scorable markets:** ~23,000 markets with ≥ $100k volume had a price strictly between 5¢ and 95¢ 24 h before close.
- **Recommendation: (a) RL is viable on counts, with three conditions (§5).** The main one: build the evaluation benchmark first, from events after every compared model's training cutoff (in practice Jul–Oct 2026, split by news cluster), then train on the earlier history. We don't need finer than hourly collection: the backfill already has 1-minute prices.
- **Urgent and outside this audit:** Polymarket retires Data API v1 on **2026-10-24**, and the production collector uses it (§2).

## 1. What we already have (Phase 1 inventory)

Run from a cloud container with no route to the VPS, so the live store was **not queried**. Sources: the counts in `docs/DECISIONS.md`, the public API (`/api/now`, `/api/explore/series`), and the one real collector hour in `tests/fixtures/`. `experiments/feasibility/vps_inventory.sql` gives the exact numbers on the VPS (see Handoff).

**Correction to the brief.** The collector does not take hourly snapshots from Oct 1. It saves **every trade (fill)** from **2026-09-29 19:36 UTC**, polling `data-api.polymarket.com/trades` (v1) for 10,000-trade pages every 4 minutes. Hourly is only the KPI grain.

| | Value | Source |
|---|---|---|
| Record | 2026-09-29 19:36 UTC → now (~12.1 days on 2026-10-11) | API `record_start` |
| Trades | 8,868,673 by 2026-10-06 12:17; ~16M estimated by 2026-10-11 | DECISIONS (M13 phase C) + 168h of API hourly counts |
| Trades / hour | 38k–75k (mean ~55k) | API, last 168 h |
| Markets traded / hour | 2,460–6,689 (mean ~3,700) | API, last 168 h |
| In one 23-minute slice | 24,989 trades, 1,906 markets, 2,372 tokens, 894 events, 4,783 wallets | fixture hour |
| Trades per market in that slice | median 2, p90 13; 145 of 1,906 markets have ≥ 20 | fixture |
| Recurring "Up or Down" crypto markets | 59% of trades, ~20% of dollars, 109 markets | fixture |
| Hours touching a collector gap (last 168 h) | 10 (Oct 5 00, Oct 6 21, Oct 7 10 & 23, Oct 8 16, Oct 10 15/21/22, Oct 11 13/14); recorded gaps before that: 36 s (Oct 2), 2 min (Oct 5) | API blanks those hours; DECISIONS |

**Fields.** Per trade: price, size, side, wallet, `asset` (= CLOB token id), `conditionId`, market slug, `eventSlug`, outcome label, timestamp; `outcomeIndex` is in the raw files but not loaded into the store. Price is the **traded price**, not a midpoint. **Not captured:** order book, bid/ask, liquidity, open interest, Gamma volume.

**Identity and resolution.** Markets are keyed by `conditionId`; tokens by `asset`; events by `eventSlug`. The `markets` table (`condition_id, question, event_slug, event_title, category, tags, fetched_at`) is looked up **once** and never refreshed: there is **no closed flag, end date, winner, outcome prices or `clobTokenIds`**. Resolution is not stored anywhere.

## 2. What can be backfilled (Phase 2)

Endpoints checked against docs.polymarket.com on 2026-10-11 (the docs have changed a lot since the repo was built), then tested live. All keyless; nothing authenticated or trading was called.

| Need | Endpoint (as documented) | What we found live |
|---|---|---|
| Closed / resolved markets | `gamma-api /markets/keyset?closed=true&end_date_min&end_date_max&volume_num_min&order=volumeNum&ascending=false&limit=100&after_cursor` | Works. The documented `order=volume_num` is rejected; `volumeNum` works. `/markets` refuses offsets ≥ 10,000, so counting needs keyset paging. |
| Winner | Gamma `outcomePrices` (`["1","0"]`), `umaResolutionStatus`; data-api `/v2/resolutions?condition=` (≤ 20 ids); `/v2/tokens?condition=` (`final_price`, `resolved`) | All three agree (see probe table). `/v2/resolutions` also carries `was_disputed`. |
| Price history | CLOB `/prices-history?market=<token>&startTs&endTs&fidelity=<min>` | **1-minute points for markets that closed in June 2023** (10,080 points/week). Any window > ~15 days is a 400 ("interval is too long") at every fidelity → fetch in 14-day chunks. `interval=max` returns nothing for closed markets. |
| Price history (new) | data-api `/v2/prices-history?token_id&interval=max` or `start/end` (≤ 15 days) + `bucket_seconds` | Documented as the successor, with 1m ≥ 7 d, 5m ≥ 60 d, 30m ≥ 90 d, 3h/12h permanent. **Live, a closed market gets 12-hour points only**: asking for 3h returns the same 12-hour points relabelled; 1h and finer return empty pages. Not usable for spikes on resolved markets. |
| Trades | data-api `/v2/trades?condition=<id>&limit=1000&cursor=` | **A market's whole trade history** (fixed 3-year window, keyset cursor, ~0.5 s/page). Taker side only unless `taker_only=false`. The bare feed (no condition) reaches back over the current and previous month. |

**This overturns a repo assumption.** `CLAUDE.md` says the trades feed reaches back only ~10,000 trades and "never assume a backfill is possible". That was true of v1's offset pagination. With v2's cursor, per-market trade history goes back three years and the global feed about a month.

**Urgent, outside this audit:** the docs say **Data API v1 is retired on 2026-10-24**. The production collector (`gridcase/ingest/polymarket.py`) calls v1 `GET /trades?limit=10000`. It needs moving to `/v2/trades` (cursor paging, `snake_case` fields, `condition` not `market`; default `taker_only=true`, which roughly matches what v1 served, but check counts before switching) within 13 days. With v2's month-deep cursor, the collector can also fill its own gaps after the fact.

### Probe: 20 resolved high-volume markets (`probe.py`)

The 20 largest resolved binary markets that closed 2026-04-11 → 2026-10-11, one per event, recurring series excluded. Life runs from `startDate` to the earlier of `endDate` and `closedTime`. The 1-minute series is CLOB token 0 in 14-day chunks. The trade walk was capped at 150 pages (150k trades).

| market | vol $M | life d | 1-min points | coverage | v2 12h points | taker trades | complete | disputed | outcome agrees |
|---|---|---|---|---|---|---|---|---|---|
| MicroStrategy sells any Bitcoin by May 31, 2026? | 375.8 | 29.0 | 41,650 | 100% | 58 | 62,247 | ✓ | yes | ✓ |
| US forces enter Iran by April 30? | 269.0 | 21.3 | 30,599 | 100% | 22 | 85,284 | ✓ | yes | ✓ |
| US x Iran ceasefire extended by April 22, 2026? | 203.6 | 0.2 | 247 | 94% | 20 | 30,689 | ✓ | yes | ✓ |
| US x Iran permanent peace deal by June 15, 2026? | 177.4 | 30.2 | 43,267 | 100% | 67 | 88,926 | ✓ | yes | ✓ |
| Will Argentina win the 2026 FIFA World Cup? | 174.3 | 382.1 | 549,154 | 100% | 697 | 150,000+ | – | no | ✓ |
| Russia x Ukraine ceasefire by May 31, 2026? | 141.3 | 39.0 | 55,310 | 98% | 48 | 13,549 | ✓ | no | ✓ |
| Israel x Hezbollah ceasefire by April 18, 2026? | 98.6 | 4.4 | 6,225 | 98% | 0 | 9,767 | ✓ | yes | ✓ |
| Will Chelsea win the 2025–26 English Premier League? | 91.3 | 241.0 | 346,612 | 100% | 470 | 22,063 | ✓ | no | ✓ |
| Will Adanech Abiebie be the next PM of Ethiopia? | 83.5 | 35.3 | 50,602 | 100% | 325 | 15,781 | ✓ | no | ✓ |
| No change in Fed rates after the April meeting? | 76.9 | 167.0 | 240,067 | 100% | 263 | 74,720 | ✓ | no | ✓ |
| Will the Iranian regime fall by June 30? | 65.9 | 194.0 | 278,779 | 100% | 348 | 150,000+ | – | no | ✓ |
| Russia x Ukraine ceasefire by June 30, 2026? | 60.7 | 116.0 | 166,133 | 99% | 200 | 24,413 | ✓ | no | ✓ |
| No change in Fed rates after the September meeting? | 56.4 | 125.1 | 179,613 | 100% | 252 | 60,492 | ✓ | no | ✓ |
| Will the Iranian regime fall by April 30? | 52.7 | 55.3 | 79,344 | 100% | 82 | 65,162 | ✓ | no | ✓ |
| Strait of Hormuz traffic normal by end of June? | 44.1 | 77.1 | 110,532 | 100% | 154 | 84,136 | ✓ | no | ✓ |
| France vs. Spain: Team to Advance | 43.8 | 3.4 | 4,818 | 100% | 8 | 43,546 | ✓ | no | ✓ |
| Spain vs. Argentina: Team to Advance | 43.4 | 3.7 | 5,268 | 100% | 9 | 67,838 | ✓ | no | ✓ |
| No change in Fed rates after the June meeting? | 43.1 | 188.1 | 270,251 | 100% | 330 | 68,900 | ✓ | no | ✓ |
| Will the San Antonio Spurs win the 2026 NBA Finals? | 42.7 | 355.6 | 511,218 | 100% | 672 | 150,000+ | – | no | ✓ |
| No change in Fed rates after the July meeting? | 40.5 | 131.0 | 187,874 | 100% | 218 | 53,049 | ✓ | no | ✓ |

- **History depth:** every market has 1-minute prices from its first day; 3.16M points over 2,199 market-days, with coverage of 94–100% of minutes. One-off checks found the same density for markets that closed in December 2025, June 2025, October 2024 (the 2024 US presidential market) and June 2023.
- **Fidelity:**
  - CLOB: 1, 10 or 60 minutes, as asked.
  - v2 `/prices-history`: 12 hours only, for closed markets.
- **Resolution:** retrievable for all 20, and Gamma (`outcomePrices`) agrees with data-api `/v2/resolutions` for all 20.
  - UMA markets report `price` (1e18 = outcome 0); native v2 markets (the World Cup ones) report `payouts`.
  - 5 of the 20 were disputed (`was_disputed`) and still settled cleanly.
- **Trades:** 17 of 20 histories were complete within 150 pages. The three year-long markets have over 150k taker trades each.
- **Cost of the probe:** about 1,700 requests for prices, resolutions and trades. All succeeded; the client retries 429/5xx after `Retry-After` but doesn't log retries, so I can't say whether any happened.

### Full-backfill estimate

Request counts are from the census. Time assumes one request every ~0.33 s: the sample run spent 581 s of network time on 2,723 requests, and the client spaces requests at least 0.25 s apart.

| Scope (markets closed in 6 months, non-recurring, resolved) | Markets | Price requests (14-day chunks) | Time | 10-min points | 1-min points |
|---|---|---|---|---|---|
| ≥ $14k (census floor) | 120,474 | 192,555 | ~18 h | ~234M | ~2.3B |
| ≥ $100k | 37,281 | 72,854 | ~7 h | ~99M (~1 GB in DuckDB) | ~1B (~10 GB) |
| ≥ $1M | 4,558 | 15,946 | ~1.5 h | ~26M | ~260M |

- **Gamma metadata:** about 2,000 keyset pages per 6 months at ≥ $14k (25 min), or 440 pages at ≥ $100k.
- **Earlier periods:** the six months before (Oct 2025–Apr 2026) had 25,730 qualifying markets at ≥ $100k, 69% of the latest six months. So one year is about 1.7× the rows above. I didn't measure 2023–2025.
- **Trades:** optional; they're needed only for the driver breakdowns (by trader, trade size, odds band). Each page is about 0.5 s and holds 1,000 taker trades, and $1M+ markets have tens of thousands of trades. Allow roughly a day of requests for the ≥ $100k set; I didn't measure this precisely.
- **Cost:** $0 in API fees, since everything is keyless. The real costs are disk and the VPS's limited memory, so run it on the laptop or at `nice` priority, and write Parquet rather than this audit's JSON cache (3.3 GB here).

## 3. Gradable events (Phase 3)

**Frame.** Markets in the census (closed 2026-04-11 → 2026-10-11) that are resolved binary (outcome prices exactly 0/1), not a recurring "Up or Down" series, and above the census floor of $13,988 lifetime volume:

- **120,538 markets in 47,290 events** (`frame` in `sample.py`).
- The census itself is the 200,000 largest closed markets. 78,585 of them (39%) are recurring Up/Down series and 878 have no binary winner.
- A full census of one week down to $1k (Sep 1–8) had 26,392 markets: 18,958 resolved non-recurring, of which 3,294 are ≥ $25k and 1,388 ≥ $100k. The 6-month frame, 37,281 at ≥ $100k, is consistent with that (≈ 26 × 1,388).

**Liquidity thresholds.** Closed markets carry no liquidity figure, so I used lifetime volume.

- **Frame quantiles:** 25% $22.7k, 50% $45.3k, 75% $137.7k, 90% $423.8k, 99% $3.23M.
- **Proposed tiers** (the table shows all three):
  - **$100k** (roughly the 70th percentile): the main filter. At that level a 10-point move takes real money.
  - **$1M** (roughly the 96th percentile): the strict filter.
  - **$10k**: shown only to show what thin markets add.
- **Why the floor matters:** below $100k, a single trade across a wide spread can move the price.

**Sample.** 160 events: 40 drawn at random from each event-volume stratum ($10k–100k, $100k–1M, $1M–10M, $10M+), with every qualifying market in each event fetched (up to 25). That gives 1,017 markets and 4.17M 10-minute points. Stratum weights (events in the stratum ÷ events sampled) scale counts to the whole frame. Intervals are 90% bootstrap intervals that resample events within each stratum.

**Definitions** (all in `analyze.py`):

- **Spike:** on the outcome-0 price, on a 10-minute grid with the last price carried forward, a spike at t means |p(t) − p(t−Y)| ≥ X points, with:
  - p(t−Y) between 5¢ and 95¢ (not already decided);
  - t + 72 h before the market's close, so labels never see the resolution;
  - a 24 h cooldown per market after each spike, so a move and its own reversal count once.
- **Held / reverted:** retained(H) = (p(t+H) − p(t−Y)) / (p(t) − p(t−Y)). The spike held if retained ≥ 0.5 and reverted otherwise. "Clean" means retained ≥ 0.75 or ≤ 0.25.
- **Event cluster:** spikes in markets of the same event within 6 h of each other.
- **News cluster:** event clusters joined when spikes in different events are within 2 h and the events share a specific tag. A tag counts as specific if it's on ≤ 2% of the frame's events; 21 broad tags such as Sports, Politics and Crypto are excluded.

### Sensitivity table: markets that closed in the 6 months (spikes over their whole lives)

`spikes`, `clusters` and `held` are raw counts in the 160-event sample. `est. 6 mo` is the weighted estimate of event clusters for the whole frame.

| X pts | Y h | min vol | spikes | held/rev @72h | clean | event clusters | news clusters | est. event clusters, 6 mo (90% CI) |
|---|---|---|---|---|---|---|---|---|
| 5 | 1 | $100k | 1,552 | 53/47 | 88% | 762 | 736 | 44,107 (27,477–66,847) |
| 5 | 6 | $100k | 1,789 | 57/43 | 89% | 867 | 839 | 48,418 (30,248–72,162) |
| 5 | 24 | $100k | 2,236 | 69/31 | 89% | 1,055 | 998 | 54,719 (34,565–81,826) |
| 10 | 1 | $10k | 1,333 | 56/44 | 88% | 629 | 615 | 74,039 (53,300–97,831) |
| 10 | 1 | $100k | 925 | 54/46 | 88% | 456 | 442 | 21,189 (13,823–30,352) |
| 10 | 1 | $1M | 417 | 49/51 | 84% | 259 | 246 | 4,227 (2,983–5,554) |
| 10 | 3 | $100k | 1,023 | 57/43 | 89% | 498 | 479 | 24,681 (15,637–35,248) |
| 10 | 6 | $10k | 1,577 | 60/40 | 89% | 728 | 709 | 90,418 (61,304–127,014) |
| **10** | **6** | **$100k** | **1,091** | **58/42** | **89%** | **534** | **517** | **26,816 (16,896–39,280)** |
| 10 | 6 | $1M | 506 | 51/49 | 86% | 316 | 300 | 5,073 (3,534–6,675) |
| 10 | 24 | $100k | 1,418 | 70/30 | 89% | 672 | 645 | 31,168 (19,483–46,099) |
| 10 | 24 | $1M | 679 | 66/34 | 87% | 430 | 406 | 6,678 (4,551–8,868) |
| 15 | 3 | $100k | 730 | 61/39 | 90% | 339 | 329 | 17,557 (10,694–25,883) |
| 15 | 6 | $100k | 781 | 59/41 | 88% | 379 | 371 | 18,371 (11,224–27,566) |
| 15 | 6 | $1M | 336 | 52/48 | 83% | 209 | 201 | 3,190 (2,173–4,297) |
| 20 | 1 | $100k | 478 | 62/38 | 90% | 218 | 214 | 9,460 (5,996–13,970) |
| 20 | 6 | $100k | 556 | 68/32 | 90% | 242 | 237 | 12,592 (7,268–18,674) |
| 20 | 6 | $1M | 226 | 60/40 | 89% | 124 | 119 | 1,794 (1,071–2,557) |
| 20 | 24 | $1M | 296 | 71/29 | 88% | 174 | 166 | 2,481 (1,515–3,577) |
| 30 | 1 | $1M | 74 | 58/42 | 96% | 47 | 44 | 568 (286–901) |
| 30 | 6 | $100k | 301 | 73/27 | 95% | 121 | 118 | 3,673 (1,948–6,282) |
| 30 | 6 | $1M | 120 | 68/32 | 93% | 61 | 58 | 799 (409–1,201) |
| 30 | 24 | $1M | 129 | 74/26 | 91% | 80 | 76 | 1,169 (630–1,835) |

All 60 cells (X ∈ {5, 10, 15, 20, 30} × Y ∈ {1, 3, 6, 24} × {$10k, $100k, $1M}) are in `data/feasibility/backfill_phase3.json`, and `analyze.py` prints them.

### Cross-check: every $1M+ market trading in August 2026 (no extrapolation)

`sample.py --dense 2026-08-01 2026-09-01 1000000` fetched 822 markets in 551 events, counting only spikes inside August. This is a calendar-month rate, and a lower bound, because markets still open after 2026-10-11 aren't in the census.

| X | Y | spikes | held/rev @72h | event clusters | news clusters | news ÷ event |
|---|---|---|---|---|---|---|
| 5 | 1 | 741 | 59/41 | 616 | 456 | 0.74 |
| 10 | 1 | 356 | 66/34 | 306 | 238 | 0.78 |
| **10** | **6** | **446** | **68/32** | **369** | **291** | **0.79** |
| 10 | 24 | 560 | 74/26 | 469 | 350 | 0.75 |
| 20 | 6 | 144 | 73/27 | 122 | 105 | 0.86 |
| 30 | 6 | 51 | 69/31 | 45 | 42 | 0.93 |

### Clustering

- **Within one event:** markets move together. At X=10, Y=6, ≥ $100k there are 2.0 spikes per event cluster in the sample, because the sample keeps every market of an event, such as all candidates in a "who wins" event. In the $1M+ August set it's 1.2.
- **Across events:** the August set, where whole news days are present, shows tag-linked news merging another **21%** of clusters at X=10 (79% remain), and less for bigger moves (14% at 20 points, 7% at 30).
- **How it was measured:** the random 160-event sample can't measure this, because related events are rarely sampled together; its news-cluster column is only 2–4% below the event-cluster column. So **apply the August ratio**: X=10, Y=6, ≥ $100k gives about 26,816 × 0.79 ≈ **21,000 independent clusters**.
- **Tag-linking is a proxy.** It misses related events that share no specific tag (Fed and crypto, say), and it joins unrelated events that share a theme tag such as "Iran". Treat 0.79 as ±0.1.
- **Splits must group by news cluster, not market or event.** `analyze.clusters()` gives the cluster id.

### Class balance

- **Held vs reverted at +72 h** runs from 49/51 (small, fast moves in the biggest markets) to 82/18 (30-point moves over 24 h). The main cell is 58/42; August's $1M+ set is 68/32.
- **Bigger, slower moves hold more often.** That makes "will it revert" a balanced task at X=5–15 and an easy, skewed one at X ≥ 20.
- **Labels are clear-cut:** 83–96% of spikes retain ≥ 75% or ≤ 25% of the move, so few sit near the 50% line.

### Category mix (X=10, Y=6, ≥ $100k)

- **Main sample:** sports 64% (mostly long-dated futures such as league or tournament winners), politics/geopolitics 22%, crypto 10%, esports 2%.
- **August $1M+:** sports 38%, esports 26%, politics/geo 18%, crypto 8%, econ/finance 6%.
- **Held rate** is similar across categories (53–77%).
- **Implication:** a benchmark that should be about news will need per-category quotas.

### Brier-scorable resolved markets

A market counts if it resolved binary and its price **24 h** (or **7 d**) before close was between 5¢ and 95¢, meaning the outcome wasn't already priced in. Estimates are for markets that closed in the 6 months.

| Horizon | Sample markets | Sample events | Est. markets, 6 mo | Est. ≥ $100k, 6 mo | Est. events, 6 mo |
|---|---|---|---|---|---|
| 24 h | 610 | 127 | ~83,000 | ~22,900 | ~34,300 |
| 7 d | 320 | 66 | ~47,100 | ~11,800 | ~17,600 |

In the August $1M+ set alone, 573 markets (440 events) qualify at 24 h and 340 (254 events) at 7 d. Most are sports matches; score per event, or per mutually exclusive group, not per market.

### Extrapolation

| Count (filters: ≥ $100k, X=10, Y=6, 72 h labels) | 6 months | 12 months (×1.7) |
|---|---|---|
| Spike event clusters (main estimate) | 26,800 (16,900–39,300) | ~46,000 |
| …after news clustering (×0.79) | ~21,000 | ~36,000 |
| Strictest: ≥ $1M, X=30, Y=1, after news clustering | ~530 | ~900 |
| Observed directly, August $1M+, news clusters | 291 in one month | – |
| Brier tasks, 24 h horizon, ≥ $100k markets | ~22,900 | ~39,000 |

Our own record (12 days) adds nothing at this scale. Its value is a live check that the backfilled CLOB series match what we saw ourselves (§6, step 2).

## 4. Data-quality issues found

1. **Data API v1 retires on 2026-10-24.** The collector must move to v2 before then. I've raised it as a separate suggested task.
2. **The "no backfill" assumption in `CLAUDE.md` is out of date.** v2 trades go back three years per market and about a month globally, and CLOB prices go back to 2023 at 1-minute fidelity.
3. **v2 `/prices-history` doesn't match its docs for closed markets.** It returns 12-hour points only; 3-hour requests come back as 12-hour points relabelled. Use CLOB `/prices-history`.
4. **CLOB `/prices-history` quirks:** windows over ~15 days are a 400 error (chunk them); `interval=max` returns nothing for closed markets. Its `p` is the displayed market price, not a traded price, so it doesn't by itself show whether money changed hands at that level.
5. **Gamma quirks:**
   - `order=volume_num` (as documented) is rejected; use `volumeNum`.
   - Offsets ≥ 10,000 are refused; use `/markets/keyset`.
   - Some markets have `endDate` before `startDate`; fall back to `closedTime`.
   - Trading often continues after `endDate` while resolution is pending. I cut lives at the earlier of `endDate` and `closedTime`.
6. **Gamma `volumeNum` is not trade dollars.** It runs 1.6–33× the dollar sum of v2 taker trades (median 2.6× over the 17 complete probe markets). Use it only to rank or filter.
7. **Two resolution formats.** UMA markets have `price` (1e18 scale); native v2 markets have `payouts`. A quarter of the top 20 were disputed before settling.
8. **Recurring series dominate counts.** They're 39% of large closed markets and 59% of our trades. They're flagged by title or slug (`census.RECURRING`), which misses daily price-threshold markets ("Solana above $50 on September 2?"); those are short-lived, and the 72 h rule mostly removes them.
9. **Our own record:**
   - Ten hours in the last week touch a collector gap (durations unknown from here).
   - Market metadata is never refreshed, so resolution, close and end dates are missing.
   - `outcomeIndex` is saved raw but not loaded into the store.
   - The trade price is a traded price, not a midpoint.
10. **Knowledge cutoff.** The models being compared know about events before their training cutoffs: a frontier model may simply remember who won. Evaluation tasks must come from after the latest cutoff of any compared model (for current frontier models, mid-2026), which means Jul–Oct 2026 here: a little over half the 6-month counts. Training data can be older.

## 5. Recommendation

**(a) RL is viable on counts.** Even the strictest cell (≥ $1M, 30-point moves within an hour, after news clustering) leaves several hundred independent spike tasks per 6 months. The main cell (≥ $100k, 10 points in 6 h) leaves about 21,000, and Brier tasks add more than 20,000. Hourly collection isn't a limit: we don't need finer granularity, because the backfill gives 1-minute prices.

Three conditions, in order:

1. **Build the evaluation set first, then train.**
   - Freeze an eval benchmark from post-cutoff events (Jul–Oct 2026 plus the collector's live record going forward), split by news cluster.
   - Measure the untuned small model and the frontier model on it.
   - Only then build the training set from earlier history, with no news cluster in both.
   - This is option (b)'s order with (a)'s data.
2. **A "why did it move" task needs more than prices.** The driver breakdowns (market, category, trade size, trader type, odds band) need trades. Prices alone support "did it hold" and Brier tasks, but trade-level investigation tasks need the v2 trade backfill for the chosen markets, at roughly a day of requests for the ≥ $100k set.
3. **The backfill is a new data source under hard rule 5.**
   - It needs its own plan and a DECISIONS line.
   - It should land in the semantic layer, with `trades_as_of`-style point-in-time filtering on `collected_at` = the original trade or bucket time, so all three models see the same governed view.

Not (c): no collection change is needed for feasibility. The v2 collector migration is needed anyway.

## 6. Handoff: continue on the laptop / VPS

Everything above ran in a cloud container with no route to the VPS. The scripts are in `experiments/feasibility/`; the raw API cache, DuckDB files and JSON outputs are under `data/feasibility/` (gitignored, so **not** in the branch: a re-run rebuilds them, using a fresh cache).

**Steps already taken (in order):**
1. Phase 1 inventory from the repo, `docs/DECISIONS.md`, the public API and the fixture hour.
2. Read the current docs (docs.polymarket.com `llms.txt`, the CLOB / Gamma / data-api v2 OpenAPI specs) and probed every endpoint live (`api.py`, `backfill.py` docstrings record what was found).
3. `census.py` (2,000 pages, 25 min): the 200,000 largest closed markets that ended 2026-04-11 → 2026-10-11, complete down to $13,988 volume. Plus a full one-week census to $1k (`tail_week.duckdb`) and the previous six months at ≥ $100k (`census_prev.duckdb`).
4. `probe.py --n 20 --max-trade-pages 150`: the Phase 2 table.
5. `sample.py --per-stratum 40 --seed 7` (160 events, 1,017 markets, 4.17M points, 2,723 requests) and `sample.py --dense 2026-08-01 2026-09-01 1000000` (every ≥ $1M market trading in August).
6. `analyze.py` and `analyze.py --prefix dense --window 2026-08-01 2026-09-01`: the Phase 3 tables.

**Where each number comes from:**

| Section | Script → output |
|---|---|
| §1 inventory (cloud) | public API `/api/explore/series?kpi=trades&hours=168`, `markets_traded`; fixture profile = `vps_inventory.sql` run on `tests/fixtures/polymarket` |
| §1 inventory (VPS, to do) | `run_sql.py vps_inventory.sql` |
| §2 probe table | `probe.py` → `data/feasibility/probe.json`, table `probe` |
| §2 backfill estimate | `census.py` → table `census`; `sample.py` builds `frame`; request counts = Σ ceil(life / 14 d) over `frame` |
| §3 frame, liquidity, sensitivity, Brier, category mix | `sample.py` → `sample_markets`, `prices`; `analyze.py` → `data/feasibility/backfill_phase3.json` (keys `liquidity`, `sensitivity`, `brier`, `category_mix_x10_y6_100k`, `examples`) |
| §3 August cross-check | `sample.py --dense …` → `dense_markets`, `dense_prices`; `analyze.py --prefix dense --window …` → `backfill_phase3_dense.json` |
| §3 tail and growth | `tail_week.duckdb`, `census_prev.duckdb` (census.py with `FEASIBILITY_DB`) |

**To reproduce here or on the laptop** (keyless, ~1.5 h of polite requests):
```bash
git checkout claude/polymarket-data-feasibility-anj2kc && uv sync
cd experiments/feasibility
uv run python census.py --start 2026-04-11 --end 2026-10-11          # ~25 min
FEASIBILITY_DB=../../data/feasibility/tail_week.duckdb uv run python census.py --start 2026-09-01 --end 2026-09-08
FEASIBILITY_DB=../../data/feasibility/census_prev.duckdb uv run python census.py --start 2025-10-11 --end 2026-04-11 --min-volume 100000
uv run python probe.py --n 20 --max-trade-pages 150
uv run python sample.py --per-stratum 40 --seed 7                      # ~15 min
uv run python sample.py --dense 2026-08-01 2026-09-01 1000000          # ~15 min
uv run python analyze.py
uv run python analyze.py --prefix dense --window 2026-08-01 2026-09-01
```
Census paging is by volume, so a later re-run sees the same markets (closed markets don't change) but the sample draws (`setseed`) depend on census row order; expect the sampled events to differ slightly and the estimates to move within their intervals.

**Still to do on the VPS (needs the collected data):**
1. Inventory: from the repo root, `nice -n 19 ionice -c3 uv run python experiments/feasibility/run_sql.py experiments/feasibility/vps_inventory.sql` (reads only the raw files; the server keeps its lock on the store). Replace §1's estimates with exact counts: total trades, distinct markets/tokens/events/wallets over the whole record, every gap with its length, and any empty hours.
2. Spikes on our own record: `nice -n 19 ionice -c3 uv run python experiments/feasibility/collector_series.py` then `FEASIBILITY_DB=data/feasibility/collector.duckdb uv run python experiments/feasibility/analyze.py --no-census`. Compare spikes per market-day with the backfill sample, to check that the CLOB 10-minute series and our trade-built series agree. Memory: DuckDB is capped at 1 GB; run it away from the hourly :20 dbt build.
3. Before 2026-10-24: move the collector to data-api v2 (separate task; see §2).
4. Decide (see §5) whether to run the full backfill; it would be a new data source under hard rule 5 and should get its own plan in `docs/plans/`.
