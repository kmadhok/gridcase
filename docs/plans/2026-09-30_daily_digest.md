# MarketCase — daily digest (M6) and public hosting (M8)

Status: approved 2026-09-30 ("I like the direction. Go for it."). Mockup:
https://claude.ai/artifact/WhKxXaR3ArmFYY3D7254tj

## Why

The watch answers "did something break this hour?". A PM also wants a morning read: what changed
this week, what traders believe, who is trading, how the market behaves, and what the watch caught.
Each of these is a standing question, answered once a day. Each answer is one sentence with one
chart, and every number links to its evidence.

## Decisions (first principles)

- **Checks and templates, not the model.** A chart needs the same structured numbers every day.
  A daily job should be free and give the same answer twice. So each standing question is a
  governed digest check (a tool that returns a receipt) plus a sentence template whose numbers
  come from that receipt. The verifier still checks every sentence. This changes the pivot
  plan, which said "answered by the ask engine".
- **Periods shrink to the history we have.** The record starts 2026-09-29 19:37 UTC. The digest
  compares the last P days with the P days before, where P = min(7, full days of record ÷ 2).
  With 2 full days it compares yesterday with the day before. With under 2 days, the comparison
  cards say there is not enough history yet.
- **Only through the semantic layer.** Checks read trades only via `ctx.kpi`, which is compiled
  from `ontology/kpis.yaml`. Governed additions:
  - dimension `outcome`: a market question with its outcome, such as "Will the Fed cut? · Yes"
  - dimension `first_seen_day`: the day a wallet first traded in our record
  - internal two-step KPIs `opening_price` and `closing_price`: the first and last price of an
    outcome in the period, for outcomes with at least 20 trades in event markets
  - Busiest hours, concentration and next-day return come from existing KPIs grouped or bucketed
    differently, so no new KPI is needed for them.
- **Digest checks are not planner tools.** They live in their own registry, so the planner, the
  question box and MCP are unchanged.

## Cards (the standing questions)

| Section | Question | Check | Chart |
|---|---|---|---|
| Header | The period in four numbers | `digest.totals`: volume, traders, markets, new traders, this period against the previous one | stat tiles |
| What changed | Which category gained share of volume? | `digest.share_shift` (volume_share by category, two periods) | slope chart |
| What changed | Which markets came out of nowhere? | `digest.movers` (volume by market yesterday against its daily average over the previous days) | ranked bars |
| What traders believe | Whose odds moved the most yesterday? | `digest.odds_swings` (opening and closing price by outcome) | dumbbells |
| Who is trading | Are whales or regular traders driving the money? | `digest.by_day` on whale_share | day bars |
| Who is trading | Did new traders come back the next day? | `digest.retention` (active traders by day × first_seen_day) | day bars |
| How the market behaves | When is Polymarket busiest? | `digest.rhythm` (hourly volume → weekday × hour) | heatmap |
| How the market behaves | How concentrated is the money? | `digest.concentration` (volume by market, ranked) | split bar |
| How the market behaves | 5-minute crypto rounds against real events? | `digest.by_day` on recurring_share | split bars |
| What the watch caught | What set off an alarm in the last 24 hours? | the cases opened in the last 24 hours, not a check | 24-hour strip |

The headline is the first card, in the order above, whose change passes a threshold: a category
shift of at least 3 points, a mover at 3 times its usual level or more, or an odds swing of at
least 30¢. The fallback is the totals sentence.

## Build

- `gridcase/tools/digest.py`: the checks. `DIGEST_INPUTS` sits next to `TOOL_INPUTS`, and
  `run_tool` accepts either.
- `gridcase/digest.py`: `build_digest(conn, day, as_of)` does the following:
  - runs every check with `case_id = digest-<day>`
  - writes the sentences and verifies them
  - stores the result in a new table `digests(day, as_of, created_at, digest_json)`
  - `latest_digest` and `get_digest` read it back
- CLI: `gridcase digest [--day YYYY-MM-DD]`. Server: builds yesterday's digest once a day, after
  00:30 UTC, which leaves time for the last trades of the day to be collected.
- API: `GET /api/digest` (the latest), `GET /api/digest/{day}`, and `GET /api/digests` (the list).
- Web: route `#/digest[/<day>]`, with a nav link "Morning read" and a link from the Now page. The
  chart components are hand-written SVG in the style of the mockup: grey marks, amber for the
  thing the sentence names. "Show the evidence" opens the receipts with the existing ReceiptCard.

## Tests (red first)

`tests/exit/test_m6.py`, on a synthetic 15-day store with injected stories:
- A category surge this week shows up as that category's gain, and the shares add up to 1.
- A market that trades 5 times its usual volume yesterday ranks first among movers.
- An outcome priced 30¢→80¢ yesterday is the top swing. Its mirror outcome is not listed twice,
  and an outcome with under 20 trades is ignored.
- Wallets new on day D who trade on D+1 give the right return rate.
- Busiest hours: the injected peak hour is the busiest one.
- Concentration: the number of markets making up half of volume equals a hand count.
- The whale and recurring by-day values equal the KPI for each day.
- Every sentence cites receipts and survives the verifier. The digest makes no model calls.
- With 1 day of history the comparison cards say "not enough history" and nothing crashes.
- Rebuilding a day gives the same digest. Nothing collected after as_of is used.
- The API returns the stored digest; the list and one day work; an unknown day is a 404.

Vitest: the page renders every card from a fixture, the plain-language test covers it, and the
evidence toggle shows receipts.

## M8 — Public hosting on kanumadhok.com (proposal; the go-live step needs the user)

Facts:
- The site is Next.js on Vercel. DNS is on Cloudflare.
- `/sql-rag` is already served by a Vercel rewrite to Cloud Run.
- MarketCase must run on the VPS, which today is reachable only over Tailscale. Vercel cannot
  run the collector or the single database writer.

Recommended route:
1. **Cloudflare Tunnel on the VPS.** `cloudflared` runs as its own user service. It only makes an
   outbound connection, so no port is opened and the firewall and host keys shared with T3 and
   Hermes are not touched. It publishes `marketcase.kanumadhok.com` → `localhost:8710`, and
   Cloudflare terminates TLS.
2. **Public mode in the app** (`GRIDCASE_PUBLIC=1`):
   - `/mcp` returns 404, and the tunnel's ingress rules also block `/mcp`
   - no write endpoints
   - the question box stays rate-limited, with a small daily dollar cap (proposed $2)
3. **Relative paths in the web build** (Vite `base: './'`, API calls without a leading slash), so
   the same build works at `/` and under `/marketcase/`.
4. **Portfolio site:**
   - a rewrite `/marketcase/:path*` → `https://marketcase.kanumadhok.com/:path*`, the same
     pattern as `/sql-rag`
   - a "MarketCase · Live" card in `components/SelectedWork.tsx`
   - pushing to `main` deploys it

Rejected:
- A static snapshot pushed to Vercel: the data goes stale, there is no question box, and it adds
  a second pipeline.
- Opening port 443 on the VPS with Caddy: it exposes the host IP and changes a firewall that
  other projects share.

User steps:
- `cloudflared tunnel login` in a browser, which is a one-time Cloudflare authorisation
- approving the public go-live and the portfolio push

## Review items

- REVIEW: the digest uses fixed templates, not the model. It is cheaper and repeatable, but the
  wording is less varied.
- REVIEW: an odds swing needs at least 20 trades on the outcome that day, in event markets only.
- REVIEW: "came back" means any trade on the next calendar day (UTC).
- REVIEW: the digest covers whole UTC days, so "Sunday evening" is in UTC.
