"""Load and validate ontology/metrics.yaml into MetricDefs."""

from __future__ import annotations

from functools import cache
from typing import Any

import yaml
from pydantic import ValidationError

from gridcase.config import ONTOLOGY_DIR
from gridcase.semantic.schema import MetricDef


class RegistryError(ValueError):
    pass


def parse_metrics(doc: dict[str, Any]) -> dict[str, MetricDef]:
    metrics: dict[str, MetricDef] = {}
    for mid, body in doc.get("metrics", {}).items():
        if body.get("authority", "governed") != "governed":
            raise RegistryError(f"{mid}: authority must be governed")
        try:
            metrics[mid] = MetricDef(id=mid, **body)
        except (ValidationError, ValueError) as e:
            raise RegistryError(f"{mid}: {e}") from e
    for m in metrics.values():
        for dep in m.dependencies():
            if dep not in metrics:
                raise RegistryError(f"{m.id}: unknown metric {dep!r}")
    _check_acyclic(metrics)
    return metrics


def _check_acyclic(metrics: dict[str, MetricDef]) -> None:
    state: dict[str, int] = {}  # 1 = visiting, 2 = done

    def visit(mid: str, path: list[str]) -> None:
        if state.get(mid) == 2:
            return
        if state.get(mid) == 1:
            raise RegistryError(f"cycle: {' -> '.join([*path, mid])}")
        state[mid] = 1
        for dep in metrics[mid].dependencies():
            visit(dep, [*path, mid])
        state[mid] = 2

    for mid in sorted(metrics):
        visit(mid, [])


@cache
def load_metrics() -> dict[str, MetricDef]:
    doc = yaml.safe_load((ONTOLOGY_DIR / "metrics.yaml").read_text())
    return parse_metrics(doc)
