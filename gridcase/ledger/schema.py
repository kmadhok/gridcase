"""Receipt: the unit of evidence. Its id is a content hash of (tool, inputs, as_of, output)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=_default)


def _default(o: Any) -> Any:
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, tuple | set):
        return list(o)
    if hasattr(o, "model_dump"):
        return o.model_dump(mode="json")
    raise TypeError(f"not serialisable: {type(o).__name__}")


class Receipt(BaseModel):
    receipt_id: str
    tool: str
    inputs: dict[str, Any]
    as_of: datetime
    data_version: datetime | None
    output: dict[str, Any]
    effect_size: float | None
    runtime_ms: int
    sql: list[dict[str, str]] = Field(default_factory=list)  # [{sql, rows_hash}] for audit

    @staticmethod
    def compute_id(
        tool: str, inputs: dict[str, Any], as_of: datetime, output: dict[str, Any]
    ) -> str:
        payload = canonical_json({"tool": tool, "inputs": inputs, "as_of": as_of, "output": output})
        return hashlib.sha256(payload.encode()).hexdigest()[:24]

    @classmethod
    def build(
        cls,
        tool: str,
        inputs: dict[str, Any],
        as_of: datetime,
        output: dict[str, Any],
        data_version: datetime | None,
        effect_size: float | None,
        runtime_ms: int,
        sql: list[dict[str, str]],
    ) -> Receipt:
        inputs_json = json.loads(canonical_json(inputs))
        return cls(
            receipt_id=cls.compute_id(tool, inputs_json, as_of, output),
            tool=tool,
            inputs=inputs_json,
            as_of=as_of,
            data_version=data_version,
            output=output,
            effect_size=effect_size,
            runtime_ms=runtime_ms,
            sql=sql,
        )
