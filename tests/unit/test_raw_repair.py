"""A crash while the collector is appending can leave an hour file DuckDB cannot read: zero
bytes between gzip members (the 2026-10-26 kayak crash, where the page cache was lost after the
file grew) or a member cut off mid-write. One such file used to fail every load after it, so the
collector repairs recent files when it starts, and the loader skips a file it cannot read."""

from __future__ import annotations

import gzip
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import pytest

from gridcase.ingest.polymarket import CORRUPT_DIR, load_state, repair_hour_file, repair_recent
from gridcase.ingest.polymarket_load import load_raw
from gridcase.store.db import connect

T0 = 1790711000  # 2026-09-29T19:43:20Z


def line(ts: int, tx: str) -> bytes:
    t: dict[str, Any] = {
        "proxyWallet": "0xw", "side": "BUY", "asset": "a1", "conditionId": "0xc", "size": 10.0,
        "price": 0.5, "timestamp": ts, "title": "Will X?", "slug": "will-x", "eventSlug": "x",
        "outcome": "Yes", "transactionHash": tx, "collected_at": "2026-09-29T19:44:00+00:00",
    }  # fmt: skip
    return json.dumps(t).encode() + b"\n"


def member(*lines: bytes) -> bytes:
    return gzip.compress(b"".join(lines))


def hour_file(root: Path, hour: int, body: bytes) -> Path:
    path = root / "trades" / "2026" / "09" / "29" / f"{hour:02d}.jsonl.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def txs(path: Path) -> list[str]:
    with gzip.open(path, "rt") as f:
        return [json.loads(x)["transactionHash"] for x in f if x.strip()]


def loaded(root: Path) -> list[str]:
    conn = connect(root / "store.duckdb")
    load_raw(conn, root)
    return [r[0] for r in conn.execute("SELECT tx_hash FROM trades ORDER BY ts").fetchall()]


def test_zero_bytes_between_members_are_removed_and_every_trade_kept(tmp_path: Path) -> None:
    body = member(line(T0, "t1")) + b"\x00" * 488 + member(line(T0 + 1, "t2"))
    path = hour_file(tmp_path, 19, body)
    assert repair_hour_file(path, tmp_path / CORRUPT_DIR) is True
    assert txs(path) == ["t1", "t2"]
    assert loaded(tmp_path) == ["t1", "t2"]
    kept = list((tmp_path / CORRUPT_DIR).iterdir())
    assert len(kept) == 1 and kept[0].read_bytes() == body


def test_a_member_cut_off_mid_write_keeps_its_complete_lines(tmp_path: Path) -> None:
    cut = member(line(T0 + 1, "t2"), line(T0 + 2, "t3"))
    path = hour_file(tmp_path, 19, member(line(T0, "t1")) + cut[: len(cut) - 30])
    assert repair_hour_file(path, tmp_path / CORRUPT_DIR) is True
    assert txs(path)[0] == "t1" and "t3" not in txs(path)
    assert loaded(tmp_path)[0] == "t1"


def test_a_healthy_file_is_left_alone(tmp_path: Path) -> None:
    body = member(line(T0, "t1")) + member(line(T0 + 1, "t2"))
    path = hour_file(tmp_path, 19, body)
    before = path.stat().st_mtime_ns
    assert repair_hour_file(path, tmp_path / CORRUPT_DIR) is False
    assert path.read_bytes() == body and path.stat().st_mtime_ns == before
    assert not (tmp_path / CORRUPT_DIR).exists()


def test_the_collector_starts_on_a_record_whose_newest_file_was_cut_off(tmp_path: Path) -> None:
    """load_state reads the newest hour file; a cut-off member there used to stop the collector
    from starting at all. Only files changed in the last few days are checked."""
    cut = member(line(T0 + 5, "t2"))
    hour_file(tmp_path, 19, member(line(T0, "t1")) + cut[:-30])
    old = hour_file(tmp_path, 18, member(line(T0 - 3600, "t0")) + b"\x00" * 8)
    week_ago = time.time() - 7 * 86400
    os.utime(old, (week_ago, week_ago))
    (tmp_path / "trades" / "2026" / "09" / "29" / "19.jsonl.gz.tmp").write_bytes(b"x")
    with pytest.raises(EOFError):
        load_state(tmp_path)
    assert repair_recent(tmp_path) == [tmp_path / "trades" / "2026" / "09" / "29" / "19.jsonl.gz"]
    assert load_state(tmp_path).last_ts == T0


def test_the_loader_skips_a_file_it_cannot_read_and_tries_it_again(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    bad = hour_file(tmp_path, 19, member(line(T0, "t1")) + b"\x00" * 8 + member(line(T0 + 1, "t2")))
    hour_file(tmp_path, 20, member(line(T0 + 3600, "t3")))
    conn = connect(tmp_path / "store.duckdb")
    with caplog.at_level(logging.WARNING, logger="gridcase"):
        r = load_raw(conn, tmp_path)
    assert r.files == 1 and r.new == 1 and "19.jsonl.gz" in caplog.text
    repair_hour_file(bad, tmp_path / CORRUPT_DIR)
    assert load_raw(conn, tmp_path).new == 2
    assert [x[0] for x in conn.execute("SELECT tx_hash FROM trades ORDER BY ts").fetchall()] == [
        "t1", "t2", "t3"
    ]  # fmt: skip
