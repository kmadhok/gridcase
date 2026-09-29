"""Receipts are content-addressed; the ledger is append-only."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import duckdb
import pytest

from gridcase.ledger.schema import Receipt
from gridcase.ledger.store import get_receipt, read_receipts, write_receipt
from gridcase.store.db import connect


@pytest.fixture()
def db(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    conn = connect(tmp_path / "l.duckdb")
    yield conn
    conn.close()


def _receipt(**over: object) -> Receipt:
    base: dict[str, object] = {
        "tool": "identity.balance",
        "inputs": {"entity": "ERCO", "window": [datetime(2023, 5, 1), datetime(2023, 5, 2)]},
        "as_of": datetime(2023, 5, 10),
        "data_version": datetime(2023, 5, 2, 1),
        "output": {"max_abs_z": 0.4, "n_missing": 0},
        "effect_size": 0.4,
        "runtime_ms": 12,
        "sql": [{"sql": "SELECT 1", "rows_hash": "abc"}],
    }
    base.update(over)
    return Receipt.build(**base)  # type: ignore[arg-type]


def test_receipt_id_depends_on_tool_inputs_as_of_and_output_only() -> None:
    a = _receipt()
    assert a.receipt_id == _receipt(runtime_ms=999).receipt_id
    assert a.receipt_id == _receipt(sql=[]).receipt_id
    assert a.receipt_id != _receipt(output={"max_abs_z": 0.5}).receipt_id
    assert a.receipt_id != _receipt(as_of=datetime(2023, 5, 11)).receipt_id
    assert a.receipt_id != _receipt(tool="anomaly.detect").receipt_id
    assert len(a.receipt_id) == 24


def test_receipt_id_is_stable_across_input_key_order() -> None:
    a = _receipt(inputs={"entity": "ERCO", "window": [datetime(2023, 5, 1), datetime(2023, 5, 2)]})
    b = _receipt(inputs={"window": [datetime(2023, 5, 1), datetime(2023, 5, 2)], "entity": "ERCO"})
    assert a.receipt_id == b.receipt_id


def test_ledger_is_append_only_and_readable_by_case(db: duckdb.DuckDBPyConnection) -> None:
    r = _receipt()
    write_receipt(db, "case-1", r)
    write_receipt(db, "case-1", r)  # same receipt again: no duplicate row
    write_receipt(db, "case-2", _receipt(output={"max_abs_z": 9.0}))
    assert [x.receipt_id for x in read_receipts(db, "case-1")] == [r.receipt_id]
    assert len(read_receipts(db, "case-2")) == 1
    assert get_receipt(db, r.receipt_id).output == r.output
    with pytest.raises(KeyError):
        get_receipt(db, "nope")
    n = db.execute("SELECT count(*) FROM ledger").fetchone()
    assert n is not None and n[0] == 2
