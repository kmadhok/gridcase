# MarketCase M13 — Explore page and a semantic-layer chat

Status: **approved 2026-10-05.** The user's decisions are listed below. The eval harness (`2026-10-05_eval_harness.md`) moves to M14. Mockup: `.lavish/explore-page-mockup.html`.

## Why

Visitors can only read MarketCase today. Explore gives them something to do. They pick a KPI and a slice, draw their own lines, click any hour to ask why it moved, and chat about what is on screen. The chat works like Cube. The model never writes SQL and never computes a number. It fills in a query form whose choices come from `ontology/kpis.yaml`, and the semantic layer runs it.

## User decisions (2026-10-05)

- Explore comes before the eval harness.
- Visitors may start 10 investigations a day, paid from their own budget (about $1.60/day). After that, the free standard playbook answers.
- Explore is a new tab in the top bar. Now stays the home page.
- The chat uses cheap open-weight models through OpenRouter. Sending visitors' questions to third-party model hosts is accepted. Frontier models are not needed.
- The paid model-pick test (phase A) is approved.
- The user left it to me where a visitor's lines are kept. Decision: in the page link (`#/explore?kpi=…&floor=…`). A refresh or a shared link keeps them, and nothing is stored on the server.

## What already exists (measured 2026-10-05)

- **The query form exists.** `semantic/kpis.py::KpiQuery` is `{kpi, grain (hour|day|week|month|total), window, as_of, filters, limits, group_by}`, which is Cube's `{measures, timeDimensions, filters, dimensions}`. `compile_kpi` and `run_kpi` compile and run it.
- **The chart's data exists.** `market_alarm.series_view(conn, WatchedSeries(kpi, filters), as_of, hours)` already returns any KPI × filter hour by hour, with its usual band and the alarm's verdict. The Now page draws watched series from it, and it accepts unwatched ones too.
- **The question box** (`ask/llm.py`) is an Anthropic-only open loop. It runs up to 3 checks, chosen one call at a time, then a forced answer, so up to 4 calls that each resend the context. It answers single questions with no memory. Its checks are shaped for investigations (compare vs usual, break down by Y). It has no "give me the numbers" query.
- **Case states** (detected → planned → executing → concluded) already let the page show an investigation's progress without changing the trace.

## Design

### 1. The query form (`gridcase/explore/query.py`)

`ExploreQuery` is what the model fills in:
- `kpi`: an enum of the public KPIs.
- `group_by`: an enum of dimensions, or null.
- `filters`: a list of `{dimension, values[]}`. Values are enums per dimension.
- `grain`: `hour`, `day` or `week`.
- `window`: a named window or an explicit `{start, end}`. The named windows are `last_24h`, `last_3d`, `last_7d`, `since_start`, `today` and `yesterday`. There is no `this_week`: on a Monday it would mean only today, so "this week" maps to `last_7d`.
- `top_n`: used only when grouping.

The enums are generated at startup from `load_kpis()`, so a model cannot name a KPI, dimension or value that does not exist. The server turns named windows into UTC times at the request's `as_of`, so the model never does date arithmetic.

`market` has no fixed values, so it is allowed only as `group_by` (top markets), never as a filter. Filtering on a free-text market name is fuzzy and opens a prompt-injection path.

A new read-only tool, `kpi.series`, maps `ExploreQuery` to `KpiQuery` with `as_of` = the request time. It runs `run_kpi` and returns a Receipt. Hard rule 2 holds: it is a pure function of its inputs and `as_of`, and reads only `trades_as_of`.

### 2. The chat: two calls per turn (`gridcase/explore/chat.py`)

1. **Pick.** The model receives:
   - the catalog skill: KPI ids, plain-language labels and definitions from `kpis.yaml` and `ask/glossary.py`, dimensions and their values, and the named windows;
   - the page state: KPI, filters, range, the user's lines, and the flagged hours;
   - the chart's own receipt;
   - up to the last 6 turns.

   It returns one of three things, as validated JSON:
   - `{queries: ExploreQuery[≤3]}`;
   - `{answer_from_screen: true}` when the page's receipt already answers;
   - `{investigate: {hour}}` for a "why did this hour move" question.

   Refusals, such as an off-topic question or an instruction to ignore rules, are a fourth reply, `{refuse: true}`.
2. **Run.** The server validates every query and runs it through `kpi.series` (free).
3. **Write.** The model writes the existing `Answer` (sentences that cite receipts) from this turn's receipts plus the page receipt. `explain/verifier.py` removes uncited numbers exactly as it does today.

`investigate` never spends money on its own. The UI shows an "Investigate this hour" button, and that path is capped.

**History without forged evidence.** The server stays stateless. The client sends past turns as plain text: each question, its queries, and the kept sentences. Earlier answers are context only and cannot be cited. Only receipts the server computes in this turn, plus the page receipt (recomputed on the server from the page state), are citable. So a client cannot inject a fake number as evidence. History is capped at about 4k tokens.

**Failure is visible.** If either call fails, or the JSON is still invalid after one retry with the validation error, the visitor sees "I couldn't answer that just now". The turn is logged as a fallback, never dressed up as an answer.

### 3. The model client (`gridcase/ask/openrouter.py`)

- `httpx` calls `https://openrouter.ai/api/v1/chat/completions` in the OpenAI format.
- Replies are requested as `response_format: json_schema` (strict) where the model's provider supports it. Otherwise JSON mode is used, and pydantic validation catches the rest.
- Usage accounting returns the actual cost of each call. The $2/day question cap is charged with that cost, so no price table is needed.
- Config:
  - `GRIDCASE_CHAT_MODEL`: the default is chosen in phase A.
  - `GRIDCASE_CHAT_FALLBACK_MODEL`: the runner-up from phase A, tried once on a provider error.
  - `OPENROUTER_API_KEY` in `.env`. It is already in the laptop `.env`. The VPS needs it copied with `scp` at deploy, on the user's say-so.
- Investigations and the case question box keep their Anthropic models, unchanged.

### 4. API

- `GET /api/explore/series?kpi&filters&hours`: `series_view` for any KPI × filters, up to 168 hours. Free. It reuses the settled-hour cache, so a request takes about 0.1 s per series.
- `POST /api/explore/investigate {kpi, filters, hour}`:
  - If a case already exists for that series and hour, it returns that case.
  - Otherwise it starts one and returns its id. The page polls the case state for progress.
  - It counts against `GRIDCASE_EXPLORE_PAID_PER_DAY=10`, a counter separate from the automatic investigations. After the cap, the standard playbook runs at $0.
  - Hours that are learning or in a collector gap are refused, with the reason.
- `POST /api/explore/ask {question, page, history}`: the two-call chat. It shares the per-visitor limiter and the $2/day cap.
- MCP stays private. These endpoints go through the same `PublicEdge` as `/api/ask`.

### 5. Page (`web/src/views/ExploreView.tsx`)

The page follows the mockup:
- controls: KPI, category, trade size, trader, range;
- the chart: an SVG line over the usual band, with draggable dashed lines, flagged dots and a hatched gap;
- the flagged-hours list, with "Cross my lines" and "Leave the usual range" modes;
- the selected-hour strip;
- the investigation panel, which shows case-state progress, then the result with a link to the case and its trace;
- the chat panel, built on `AskPanel` in a chat mode with history, suggested questions from the page state, a divider line when the view changes, and the budget line.

Page state lives in the URL hash. Tests: `web/src/__tests__/explore.test.tsx`, plus the plain-language test.

## Rule changes (logged in DECISIONS when they land)

- **Hard rule 6** becomes: "No free-form chat over the model's own knowledge. The case viewer and the Explore page are the UI. A question box or the Explore chat may answer only from governed evidence: a case's receipts, the page's own data, or semantic-layer queries the model fills in and the server runs. Every answer passes through `gridcase/explain/verifier.py`."
- **The `CLAUDE.md` stack line** becomes: "Anthropic SDK for the planner, explainer and case question box; OpenRouter (cheap open-weight models) for the Explore chat; model names in config, never hardcoded."

## Phases (each has a test written first and run red)

**A. Model pick (paid, approved). Done 2026-10-05: deepseek-v4-flash by default, gemini-3.1-flash-lite as fallback; see DECISIONS.** `experiments/model_pick/`.
- **Pick step.** 40 questions, each with its expected query or queries, written before any run. They cover:
  - plain questions about the screen;
  - grouping and filtering;
  - vague wording ("big bettors" → the $1k+ trade-size band);
  - relative time;
  - follow-ups that need history ("and Politics?");
  - "why" questions, which should return `investigate`;
  - off-topic questions and prompt injection, which should be refused;
  - unanswerable questions.

  Scoring is by fixed rules, with no model judge. It counts JSON validity, an exact match on (kpi, group_by, filters, grain, window), with listed acceptable alternatives, and correct refusals. It also records time and actual cost per question. Results are reported as counts ("37 of 40").
- **Candidates.** All take tools and structured output on OpenRouter. Prices are per million tokens in/out, as listed on 2026-10-05.

  | Model | In / out | Note |
  |---|---|---|
  | qwen3.8-flash | $0.15 / $0.47 | |
  | qwen3-235b-a22b-2507 | $0.09 / $0.55 | |
  | deepseek-v4.1-flash | $0.30 / $1.20 | |
  | deepseek-v4-flash | $0.03 / $1.28 | |
  | glm-5.3-flash | $0.15 / $0.50 | |
  | gemini-3.1-flash-lite | $0.25 / $1.50 | the only non-Chinese cheap one |
  | claude-haiku-4.5 | $1 / $5 | a reference point, not a candidate |
- **Write step (moved to phase C, because it needs `kpi.series`).** The 3 best pickers write answers on 10 questions against a synthetic store (`tests/polymarket_synth.py`). This measures sentences kept vs removed by the verifier. Expected cost is under $0.10.
- **Cost.** Measured in the smoke run: about 1.4k input tokens per pick. With 2 runs per question, that is about $0.03 per cheap model and about $0.20 for Haiku, run with a hard cap of $2.
- **Output.** The results are committed to `experiments/model_pick/results.json`, and a DECISIONS entry names the default and the fallback model.

**B. Query form and `kpi.series`** (free). Named windows, enums from the YAML, `ExploreQuery` → `KpiQuery`, and the receipt.

**C. Chat engine, OpenRouter client and `/api/explore/ask`.** Tests use a scripted model. Cost is charged from usage, and fallbacks are visible. The phase ends with the paid write-step check from phase A.

**D. `/api/explore/series` and `/api/explore/investigate`.** This includes the separate daily cap and returning an existing case. Done 2026-10-06; see DECISIONS. Changes from this plan: the page follows an investigation through `GET /api/explore/investigations/{task_id}`, then the case's own states, via a new `on_open` hook on `investigate()`. Chart requests have a per-visitor ceiling, and the server's DuckDB memory is measured in phase F.

**E. The Explore page.**

**F. Deploy.** Ask the user first, `scp` the key, check memory on the VPS, then run the browser check at desktop width and at 390 px.

## Exit test (`tests/exit/test_m13.py`, `web/src/__tests__/explore.test.tsx`)

- **Query form.**
  - An unknown KPI, dimension or value is rejected, and so is `market` as a filter.
  - Each named window resolves to the right UTC times at a fixed `as_of`.
  - `kpi.series` on a planted store equals a hand count, ignores trades collected after `as_of`, and returns a receipt with its SQL.
- **Chat, with a scripted model.**
  - A pick with 2 queries runs both and cites both.
  - `answer_from_screen` runs no query.
  - `investigate` starts nothing.
  - A refusal answers without querying.
  - A number that is not in this turn's receipts is removed.
  - A "receipt" planted in the history cannot be cited.
  - Invalid JSON gets one retry, then a visible fallback.
  - The cost charged equals the reported usage, and the $2 cap refuses the next question.
- **Endpoints.**
  - The series for an unwatched slice comes back with a band.
  - `investigate` returns an existing case for the same hour, refuses learning and gap hours, and switches to the standard playbook after 10.
  - MCP is still refused through the tunnel.
- **Page.**
  - The URL hash round-trips the state.
  - Dragging a line changes the flagged list.
  - A flagged hour opens an investigation.
  - The chat shows history and the divider line.
  - The page has no ids in visible text.

## Risks

- **Cheap models fill the form less reliably than frontier ones.** Phase A measures this before anything is built on it. If no cheap model gets at least 34 of 40 right, the default becomes the cheapest model that does, and the DECISIONS entry records the trade.
- **OpenRouter routes a model to different providers, whose behaviour and structured-output support vary.** Pin the provider order in the request and record it in the results.
- **Market titles are untrusted text** and can carry instructions. They reach the model only as data inside receipts, and the verifier still drops unsupported numbers. M14's injection story covers this.
- **The $2/day question cap is held in memory** and resets on restart (an existing `REVIEW:` item, unchanged).
- **`series_view` for unwatched slices** scans up to 28 days of history per request. Its settled-hour cache bounds the cost. This gets measured on the VPS, which is memory-tight, before deploy.
