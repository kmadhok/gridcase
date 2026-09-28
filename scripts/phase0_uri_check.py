"""Phase 0: pull ERCOT (ERCO) hourly series for Winter Storm Uri from EIA API v2.

Writes data/raw/uri_2021/{D,DF,NG,TI,fuel}.parquet and docs/phase0_report.md.
Raw API responses are cached as JSON in data/raw/uri_2021/ before parsing; a
cached response is reused on rerun so the script is idempotent and offline-safe.

Timestamps are UTC throughout. "CT" comparisons use US/Central (CST, UTC-6, in Feb 2021).
The API key is read from .env and never printed or written anywhere.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "uri_2021"
REPORT = ROOT / "docs" / "phase0_report.md"

BASE = "https://api.eia.gov/v2/electricity/rto"
RESPONDENT = "ERCO"
START, END = "2021-02-13T00", "2021-02-21T23"
SERIES = ["D", "DF", "NG", "TI"]
PAGE = 5000
MIN_INTERVAL_S = 0.25  # < 5 req/s
MAX_ATTEMPTS = 5

CST = "-06:00"
BRIDGE_T0 = pd.Timestamp(f"2021-02-14T12:00{CST}")
BRIDGE_T1 = pd.Timestamp(f"2021-02-15T12:00{CST}")
EXPECTED_HOURS = pd.date_range("2021-02-13T00:00Z", "2021-02-21T23:00Z", freq="h")

FUEL_NAMES = {
    "NG": "natural gas",
    "COL": "coal",
    "NUC": "nuclear",
    "WND": "wind",
    "SUN": "solar",
    "WAT": "hydro",
    "OIL": "petroleum",
    "OTH": "other",
    "BAT": "battery",
}

Rows = list[dict[str, Any]]


def _api_key() -> str:
    load_dotenv(ROOT / ".env")
    key = os.environ.get("EIA_API_KEY", "").strip()
    if not key:
        sys.exit("EIA_API_KEY missing from .env")
    return key


def _redact(text: str, key: str) -> str:
    return text.replace(key, "<redacted>")


_last_call = 0.0


def _get(client: httpx.Client, url: str, params: dict[str, Any], key: str) -> dict[str, Any]:
    """One request with rate spacing and backoff on 429/5xx. Never logs the key."""
    global _last_call
    for attempt in range(1, MAX_ATTEMPTS + 1):
        wait = MIN_INTERVAL_S - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()
        try:
            r = client.get(url, params={**params, "api_key": key}, timeout=60)
        except httpx.TransportError as e:
            print(f"  transport error ({attempt}/{MAX_ATTEMPTS}): {_redact(str(e), key)}")
            time.sleep(2**attempt)
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            print(f"  HTTP {r.status_code} ({attempt}/{MAX_ATTEMPTS}); backing off")
            time.sleep(2**attempt)
            continue
        if r.status_code != 200:
            sys.exit(f"HTTP {r.status_code}: {_redact(r.text[:300], key)}")
        body: dict[str, Any] = r.json()
        if "response" not in body:
            sys.exit(f"unexpected body: {_redact(json.dumps(body)[:300], key)}")
        return body
    sys.exit(f"gave up after {MAX_ATTEMPTS} attempts on {url}")


def _fetch_all(client: httpx.Client, route: str, params: dict[str, Any], key: str) -> Rows:
    rows: Rows = []
    offset = 0
    while True:
        page = {**params, "offset": offset, "length": PAGE}
        body = _get(client, f"{BASE}/{route}/data/", page, key)
        data: Rows = body["response"]["data"]
        rows.extend(data)
        total = int(body["response"].get("total", len(rows)))
        offset += len(data)
        if not data or offset >= total:
            return rows


def _cached(name: str, fetch: Callable[[], Rows]) -> Rows:
    path = RAW / f"{name}.json"
    if path.exists():
        print(f"  {name}: using cached {path.name}")
        cached: Rows = json.loads(path.read_text())
        return cached
    rows = fetch()
    path.write_text(json.dumps(rows))
    print(f"  {name}: fetched {len(rows)} rows, cached to {path.name}")
    return rows


def _base_params() -> dict[str, Any]:
    return {
        "frequency": "hourly",
        "data[0]": "value",
        "facets[respondent][]": RESPONDENT,
        "start": START,
        "end": END,
        "sort[0][column]": "period",
        "sort[0][direction]": "asc",
    }


def _frame(rows: Rows, keys: list[str]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    df["period"] = pd.to_datetime(df["period"], utc=True)
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    cols = ["period", *keys, "value"]
    return df[cols].dropna().drop_duplicates(["period", *keys]).sort_values(["period", *keys])


def pull() -> tuple[dict[str, pd.Series], pd.DataFrame]:
    key = _api_key()
    RAW.mkdir(parents=True, exist_ok=True)
    series: dict[str, pd.Series] = {}
    with httpx.Client(headers={"User-Agent": "gridcase-phase0"}) as client:

        def fetch(route: str, params: dict[str, Any]) -> Callable[[], Rows]:
            return lambda: _fetch_all(client, route, params, key)

        for typ in SERIES:
            rows = _cached(typ, fetch("region-data", {**_base_params(), "facets[type][]": typ}))
            df = _frame(rows, [])
            df.to_parquet(RAW / f"{typ}.parquet", index=False)
            series[typ] = df.set_index("period")["value"].reindex(EXPECTED_HOURS)

        rows = _cached("fuel", fetch("fuel-type-data", _base_params()))
        fuel = _frame(rows, ["fueltype"])
        fuel.to_parquet(RAW / "fuel.parquet", index=False)
    return series, fuel


def _fmt_ct(t: pd.Timestamp) -> str:
    return t.tz_convert("US/Central").strftime("%Y-%m-%d %H:00 CT")


def _argmax_time(s: pd.Series) -> pd.Timestamp:
    """Timestamp of the max value; typed explicitly because idxmax() is Hashable in stubs."""
    return pd.Timestamp(pd.DatetimeIndex(s.index)[int(s.to_numpy().argmax())])


def _coverage_lines(series: dict[str, pd.Series]) -> list[str]:
    lines = [
        "## Coverage per series (216 expected hours, 2021-02-13T00Z to 2021-02-21T23Z)",
        "",
        "| Series | Rows | Missing | Missing hours (UTC) |",
        "|---|---|---|---|",
    ]
    for name, s in series.items():
        gaps = pd.DatetimeIndex(s[s.isna()].index)
        gap_str = ", ".join(t.strftime("%Y-%m-%dT%H") for t in gaps) or "none"
        lines.append(f"| {name} | {int(s.notna().sum())} | {len(gaps)} | {gap_str} |")
    return lines


def _gap_lines(series: dict[str, pd.Series]) -> list[str]:
    gap = (series["DF"] - series["NG"]).dropna()
    local_date = pd.DatetimeIndex(gap.index).tz_convert("US/Central").date
    on_day = gap[local_date == pd.Timestamp("2021-02-15").date()]
    peak_t = _argmax_time(on_day)
    peak = float(on_day.to_numpy().max())
    overall_t = _argmax_time(gap)
    overall = float(gap.to_numpy().max())
    return [
        "## Peak forecast-minus-generation gap (DF − NG)",
        "",
        f"On 2021-02-15 (Central time) the peak gap was **{peak:,.0f} MW** at {_fmt_ct(peak_t)} "
        f"({peak_t.strftime('%Y-%m-%dT%HZ')}): DF {series['DF'][peak_t]:,.0f} MW vs NG "
        f"{series['NG'][peak_t]:,.0f} MW. Peak over the whole window: {overall:,.0f} MW at "
        f"{_fmt_ct(overall_t)}.",
    ]


def _bridge_lines(fuel: pd.DataFrame) -> list[str]:
    piv = fuel.pivot_table(index="period", columns="fueltype", values="value", aggfunc="first")
    t0, t1 = piv.loc[BRIDGE_T0], piv.loc[BRIDGE_T1]
    delta = pd.Series(t1 - t0).sort_values()
    lines = [
        f"## Fuel bridge, {_fmt_ct(BRIDGE_T0)} → {_fmt_ct(BRIDGE_T1)}",
        "",
        "| Fuel | t0 MW | t1 MW | Δ MW |",
        "|---|---|---|---|",
    ]
    for f in delta.index:
        name = FUEL_NAMES.get(str(f), str(f))
        lines.append(f"| {name} ({f}) | {t0[f]:,.0f} | {t1[f]:,.0f} | {delta[f]:+,.0f} |")
    total = float(delta.to_numpy().sum())
    lines.append(f"| **total** | {t0.sum():,.0f} | {t1.sum():,.0f} | {total:+,.0f} |")
    top = str(delta.index[0])
    share = abs(float(delta.to_numpy()[0])) / abs(total)
    lines += [
        "",
        f"Largest absolute drop: **{FUEL_NAMES.get(top, top)} ({top})**, {delta[top]:+,.0f} MW, "
        f"{share:.0%} of the total generation change.",
    ]
    return lines


def _ti_lines(series: dict[str, pd.Series]) -> list[str]:
    ti = series["TI"].dropna()
    max_abs_t = _argmax_time(ti.abs())
    return [
        "## Total interchange (TI)",
        "",
        f"Range {ti.min():,.0f} to {ti.max():,.0f} MW (EIA sign convention: positive = net "
        f"export). Max |TI| = {ti.abs().max():,.0f} MW, at {_fmt_ct(max_abs_t)}. ERCOT's DC ties "
        "to SPP and Mexico total roughly 1.2 GW, so |TI| ≤ 1,500 MW is expected.",
    ]


SERVED_LOAD_PARAGRAPH = (
    'In this window EIA\'s "demand" (D) for ERCOT is **served load**, not true demand. Form '
    'EIA-930 defines D as "actual demand" as reported by the balancing authority, i.e. the load '
    "actually metered on the system; the form makes no allowance for load that was curtailed or "
    "could not be served (Form EIA-930 instructions, DATA DEFINITIONS, "
    "https://www.eia.gov/survey/form/eia_930/instructions.pdf ). ERCOT entered EEA3 early on "
    "2021-02-15 and ordered firm load shed, rotating outages that became prolonged outages of "
    "roughly 20 GW at the peak, continuing until 2021-02-18. EIA's *Today in Energy* note of "
    '2021-02-19, "Extreme winter weather is disrupting energy supply and demand, particularly in '
    'Texas" ( https://www.eia.gov/todayinenergy/detail.php?id=46836 ), records that generation '
    '"fell sharply once ERCOT began implementing rotating outages at midnight on February 15." '
    "Metered demand fell because supply was unavailable while weather-implied demand kept "
    "rising. Any analysis of this window must not read the fall in D as a fall in demand; the "
    "day-ahead forecast (DF) is the better proxy for what consumers wanted, and DF − NG is the "
    "shortfall."
)


def write_report(series: dict[str, pd.Series], fuel: pd.DataFrame) -> None:
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Phase 0 report — Winter Storm Uri, ERCOT (ERCO), 2021-02-13 to 2021-02-21",
        "",
        f"Generated {now} from EIA API v2 `electricity/rto/region-data` and "
        "`electricity/rto/fuel-type-data`, respondent `ERCO`, `frequency=hourly` (UTC). "
        "Raw responses cached in `data/raw/uri_2021/*.json`.",
        "",
        *_coverage_lines(series),
        "",
        *_gap_lines(series),
        "",
        *_bridge_lines(fuel),
        "",
        *_ti_lines(series),
        "",
        "## Served load versus true demand",
        "",
        SERVED_LOAD_PARAGRAPH,
        "",
    ]
    REPORT.write_text("\n".join(lines))
    print(f"wrote {REPORT.relative_to(ROOT)}")


def main() -> None:
    print("pulling ERCO series for Uri window")
    series, fuel = pull()
    write_report(series, fuel)


if __name__ == "__main__":
    main()
