-- Read-only Phase 1 inventory. Reads the collector's raw files, never the server's DuckDB store
-- (the server holds its write lock). Run on the VPS from the repo root at low priority:
--   nice -n 19 ionice -c3 duckdb -c ".read vps_inventory.sql"      (or via uv run python + duckdb)
SET memory_limit = '1GB'; SET threads = 2;

CREATE TEMP VIEW raw AS
SELECT DISTINCT ON (transactionHash, asset, side, size, price) *
FROM read_json('data/raw/polymarket/trades/*/*/*/*.jsonl.gz', format = 'newline_delimited',
               compression = 'gzip',
               columns = {proxyWallet: 'VARCHAR', side: 'VARCHAR', asset: 'VARCHAR',
                          conditionId: 'VARCHAR', size: 'DOUBLE', price: 'DOUBLE',
                          timestamp: 'BIGINT', title: 'VARCHAR', eventSlug: 'VARCHAR',
                          outcomeIndex: 'INTEGER', transactionHash: 'VARCHAR'});

-- 1. totals
SELECT count(*) trades, count(DISTINCT conditionId) markets, count(DISTINCT asset) tokens,
       count(DISTINCT eventSlug) events, count(DISTINCT proxyWallet) wallets,
       to_timestamp(min(timestamp)) first_ts, to_timestamp(max(timestamp)) last_ts,
       round(sum(size * price)) usd
FROM raw;

-- 2. per-day trades and markets
SELECT CAST(to_timestamp(timestamp) AS DATE) d, count(*) trades, count(DISTINCT conditionId) markets
FROM raw GROUP BY 1 ORDER BY 1;

-- 3. trades-per-token distribution over the whole record (liquidity proxy for Phase 3)
SELECT quantile_cont(n, [0.5, 0.75, 0.9, 0.99]) q, count(*) tokens,
       count(*) FILTER (WHERE n >= 100) tok_100, count(*) FILTER (WHERE n >= 1000) tok_1000
FROM (SELECT asset, count(*) n FROM raw GROUP BY 1);

-- 4. tokens with a trade in >= N distinct hours (can a price series be built?)
SELECT count(*) FILTER (WHERE h >= 24) tok_24h, count(*) FILTER (WHERE h >= 72) tok_72h,
       count(*) FILTER (WHERE h >= 168) tok_168h
FROM (SELECT asset, count(DISTINCT timestamp // 3600) h FROM raw GROUP BY 1);

-- 5. recurring short-horizon markets' share
SELECT title ILIKE '%up or down%' recurring, count(*) trades, count(DISTINCT conditionId) markets
FROM raw GROUP BY 1;

-- 6. collector gaps (every one ever detected)
SELECT to_timestamp(from_ts) gap_from, to_timestamp(to_ts) gap_to, to_ts - from_ts AS gap_seconds
FROM read_json('data/raw/polymarket/gaps.jsonl', format = 'newline_delimited') ORDER BY 1;

-- 7. hours with zero trades (collector down without a gap record, e.g. a restart)
WITH h AS (SELECT DISTINCT timestamp // 3600 hr FROM raw),
     span AS (SELECT range AS hr FROM range((SELECT min(hr) FROM h), (SELECT max(hr) FROM h) + 1))
SELECT to_timestamp(hr * 3600) empty_hour FROM span ANTI JOIN h USING (hr) ORDER BY 1;
