"""Phase 0 exit test: Winter Storm Uri is recoverable hour-by-hour from the EIA API.

Checks the artifacts written by scripts/phase0_uri_check.py:
  data/raw/uri_2021/{D,DF,NG,TI,fuel}.parquet and docs/phase0_report.md

All timestamps in the parquet files are UTC. "CT" in the spec means US/Central,
which is CST (UTC-6) for the whole February 2021 window.
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "data" / "raw" / "uri_2021"
REPORT = ROOT / "docs" / "phase0_report.md"

WINDOW_START = pd.Timestamp("2021-02-13T00:00Z")
WINDOW_END = pd.Timestamp("2021-02-21T23:00Z")
EXPECTED_HOURS = pd.date_range(WINDOW_START, WINDOW_END, freq="h")  # 216 slots
SERIES = ["D", "DF", "NG", "TI"]
MAX_MISSING = 4

CST = "-06:00"
BRIDGE_T0 = pd.Timestamp(f"2021-02-14T12:00{CST}")
BRIDGE_T1 = pd.Timestamp(f"2021-02-15T12:00{CST}")


def _load(name: str) -> pd.DataFrame:
    path = RAW / f"{name}.parquet"
    if not path.exists():
        pytest.fail(f"missing artifact {path}; run scripts/phase0_uri_check.py first")
    df = pd.read_parquet(path)
    assert {"period", "value"} <= set(df.columns), f"{name}: need period+value columns"
    df["period"] = pd.to_datetime(df["period"], utc=True)
    return df


def _report() -> str:
    if not REPORT.exists():
        pytest.fail(f"missing {REPORT}")
    return REPORT.read_text()


@pytest.fixture(scope="module")
def series() -> dict[str, pd.Series]:
    out: dict[str, pd.Series] = {}
    for name in SERIES:
        df = _load(name)
        s = df.set_index("period")["value"].sort_index()
        assert s.index.is_unique, f"{name}: duplicate hours"
        out[name] = s.reindex(EXPECTED_HOURS)
    return out


def _missing(s: pd.Series) -> list[pd.Timestamp]:
    return [pd.Timestamp(t) for t in s[s.isna()].index]


def test_1_hourly_coverage_and_gaps_listed(series: dict[str, pd.Series]) -> None:
    assert len(EXPECTED_HOURS) == 216
    report = _report()
    for name, s in series.items():
        gaps = _missing(s)
        assert len(gaps) <= MAX_MISSING, f"{name}: {len(gaps)} missing hours: {gaps}"
        assert len(s) == 216
        for g in gaps:
            stamp = g.strftime("%Y-%m-%dT%H")
            assert stamp in report, f"{name}: gap {stamp} not listed in report"


def test_2_peak_df_minus_ng_on_feb15_ct(series: dict[str, pd.Series]) -> None:
    gap = (series["DF"] - series["NG"]).dropna()
    local = pd.DatetimeIndex(gap.index).tz_convert("US/Central")
    on_day = gap[local.date == pd.Timestamp("2021-02-15").date()]
    assert not on_day.empty
    peak = float(on_day.to_numpy().max())
    assert peak >= 25_000, f"peak DF-NG on 2021-02-15 CT was {peak:,.0f} MW"
    # The report must state the same peak (rounded to the MW).
    assert f"{peak:,.0f}" in _report()


def test_3_natural_gas_is_largest_drop_in_fuel_bridge() -> None:
    fuel = _load("fuel")
    assert "fueltype" in fuel.columns
    piv = fuel.pivot_table(index="period", columns="fueltype", values="value", aggfunc="first")
    assert BRIDGE_T0 in piv.index and BRIDGE_T1 in piv.index, "bridge endpoints missing"
    delta = (piv.loc[BRIDGE_T1] - piv.loc[BRIDGE_T0]).dropna()
    largest_drop = delta.idxmin()
    assert largest_drop == "NG", f"largest drop was {largest_drop}: {delta.to_dict()}"
    assert float(delta.to_numpy()[list(delta.index).index("NG")]) < 0


def test_4_interchange_within_ercot_tie_limits(series: dict[str, pd.Series]) -> None:
    ti = series["TI"].dropna()
    assert ti.abs().max() <= 1_500, f"TI range {ti.min():.0f}..{ti.max():.0f} MW"


def test_5_report_addresses_served_load() -> None:
    report = _report().lower()
    assert "served load" in report
    assert "rotating outage" in report
    assert re.search(r"https?://\S*eia\.gov\S*", report), "report must cite an EIA source URL"
