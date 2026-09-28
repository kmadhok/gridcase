"""ontology/metrics.yaml loads into typed MetricDefs and forms a DAG of expressions."""

from __future__ import annotations

import pytest

from gridcase.semantic.registry import RegistryError, load_metrics, parse_metrics

MINIMUM = {
    "demand",
    "demand_forecast",
    "forecast_error_mw",
    "forecast_error_pct",
    "net_generation",
    "generation_by_fuel",
    "net_interchange",
    "balance_residual",
    "temp_load_center",
    "hdd",
    "cdd",
    "lmp_rt_hub",
    "lmp_da_hub",
    "wind_capacity_factor_proxy",
    "solar_capacity_factor_proxy",
}


def test_registry_contains_the_spec_minimum_set_all_governed() -> None:
    metrics = load_metrics()
    assert set(metrics) >= MINIMUM
    assert all(m.authority == "governed" for m in metrics.values())
    assert all(m.unit and m.description for m in metrics.values())


def test_derived_metrics_reference_only_known_metrics_in_dependency_order() -> None:
    metrics = load_metrics()
    for m in metrics.values():
        for dep in m.dependencies():
            assert dep in metrics, f"{m.id} depends on unknown {dep}"
    assert metrics["balance_residual"].dependencies() == [
        "demand",
        "net_generation",
        "net_interchange",
    ]
    assert metrics["hdd"].dependencies() == ["temp_load_center"]


def test_registry_rejects_unknown_dependency() -> None:
    with pytest.raises(RegistryError, match="unknown metric"):
        parse_metrics(
            {
                "metrics": {
                    "x": {
                        "kind": "derived",
                        "expr": "y + 1",
                        "unit": "MW",
                        "description": "d",
                        "entity_kind": "ba",
                    }
                }
            }
        )


def test_registry_rejects_cycles() -> None:
    doc = {
        "metrics": {
            "a": {
                "kind": "derived",
                "expr": "b * 2",
                "unit": "MW",
                "description": "d",
                "entity_kind": "ba",
            },
            "b": {
                "kind": "derived",
                "expr": "a / 2",
                "unit": "MW",
                "description": "d",
                "entity_kind": "ba",
            },
        }
    }
    with pytest.raises(RegistryError, match="cycle"):
        parse_metrics(doc)


def test_registry_rejects_non_governed_authority() -> None:
    doc = {
        "metrics": {
            "demand": {
                "kind": "series",
                "series": "eia.D",
                "unit": "MW",
                "description": "d",
                "entity_kind": "ba",
                "authority": "adhoc",
            }
        }
    }
    with pytest.raises(RegistryError, match="authority"):
        parse_metrics(doc)


def test_daily_grain_aggregation_is_declared_per_metric() -> None:
    metrics = load_metrics()
    assert metrics["hdd"].daily_agg == "sum"
    assert metrics["demand"].daily_agg == "mean"
