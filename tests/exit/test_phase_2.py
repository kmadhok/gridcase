"""Phase 2 exit test: governed metrics compile to deterministic SQL over as_of; ontology
handles fuel-category versioning; eight hypothesis templates validate against the tool registry.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import duckdb
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from gridcase.planner.hypotheses import load_templates
from gridcase.semantic.compiler import compile_query
from gridcase.semantic.ontology import canonical_fuel, load_ontology
from gridcase.semantic.registry import load_metrics
from gridcase.semantic.schema import MetricQuery
from gridcase.tools.registry import TOOL_NAMES

REGIONS = ["ERCO", "CISO", "PJM", "US48"]
NORMAL_WEEK = (datetime(2023, 5, 1), datetime(2023, 5, 8))
AS_OF = datetime(2023, 6, 1)


def _run(conn: duckdb.DuckDBPyConnection, q: MetricQuery) -> list[tuple[object, ...]]:
    compiled = compile_query(q)
    return [tuple(r) for r in conn.execute(compiled.sql).fetchall()]


def test_1_every_metric_compiles_and_executes_for_all_regions(
    fixture_store: duckdb.DuckDBPyConnection,
) -> None:
    metrics = load_metrics()
    assert len(metrics) >= 15
    assert all(m.authority == "governed" for m in metrics.values())
    for metric_id, m in metrics.items():
        for region in REGIONS:
            q = MetricQuery(
                metric=metric_id, entity=region, grain=m.grains[0], window=NORMAL_WEEK, as_of=AS_OF
            )
            compiled = compile_query(q)
            assert "as_of(" in compiled.sql and "now()" not in compiled.sql.lower()
            fixture_store.execute(compiled.sql).fetchall()  # must not raise


_METRIC_IDS = sorted(load_metrics())


@given(
    metric=st.sampled_from(_METRIC_IDS),
    region=st.sampled_from(REGIONS),
    as_of_h=st.integers(0, 24 * 40),
    start_h=st.integers(0, 24 * 25),
    length_h=st.integers(1, 24 * 5),
)
@settings(max_examples=40, deadline=None)
def test_2_compiled_query_is_deterministic(
    fixture_store: duckdb.DuckDBPyConnection,
    metric: str,
    region: str,
    as_of_h: int,
    start_h: int,
    length_h: int,
) -> None:
    base = datetime(2023, 7, 15)
    q = MetricQuery(
        metric=metric,
        entity=region,
        grain="hourly"
        if "hourly" in load_metrics()[metric].grains
        else load_metrics()[metric].grains[0],
        window=(base + timedelta(hours=start_h), base + timedelta(hours=start_h + length_h)),
        as_of=base + timedelta(hours=as_of_h),
    )
    c1, c2 = compile_query(q), compile_query(q)
    assert c1.sql == c2.sql
    assert _run(fixture_store, q) == _run(fixture_store, q)


def test_3_balance_residual_is_small_on_a_normal_week(
    fixture_store: duckdb.DuckDBPyConnection,
) -> None:
    resid = dict(
        (r[0], r[-1])
        for r in _run(
            fixture_store,
            MetricQuery(
                metric="balance_residual",
                entity="ERCO",
                grain="hourly",
                window=NORMAL_WEEK,
                as_of=AS_OF,
            ),
        )
    )
    demand = dict(
        (r[0], r[-1])
        for r in _run(
            fixture_store,
            MetricQuery(
                metric="demand", entity="ERCO", grain="hourly", window=NORMAL_WEEK, as_of=AS_OF
            ),
        )
    )
    assert len(resid) == 24 * 7
    ok = sum(1 for t, v in resid.items() if abs(v) <= 0.03 * demand[t])
    assert ok / len(resid) >= 0.95, f"only {ok}/{len(resid)} hours within 3%"


def test_4_eight_templates_validate_against_the_registry() -> None:
    templates = load_templates()
    ids = [t.id for t in templates]
    assert ids[0] == "H0_DATA_ARTIFACT"
    assert len(ids) == 8 and len(set(ids)) == 8
    for t in templates:
        assert t.mechanism.strip() and t.falsifier.strip()
        assert t.tests, f"{t.id} has no tests"
        for step in t.tests:
            assert step.tool in TOOL_NAMES, f"{t.id} references unknown tool {step.tool}"
        assert t.prior.a > 0 and t.prior.b > 0


def test_5_fuel_codes_map_to_the_same_canonical_fuel_across_the_2024_schema_change() -> None:
    onto = load_ontology()
    assert canonical_fuel(onto, "COL", date(2023, 5, 1)) == canonical_fuel(
        onto, "COL", date(2025, 6, 1)
    )
    assert canonical_fuel(onto, "NG", date(2023, 5, 1)) == "natural_gas"
    assert canonical_fuel(onto, "BAT", date(2025, 6, 1)) == "storage"
    assert canonical_fuel(onto, "SNB", date(2025, 6, 1)) == "solar"
    with pytest.raises(KeyError):
        canonical_fuel(onto, "NOPE", date(2025, 6, 1))
