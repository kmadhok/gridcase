"""Export the three fixture windows from the local store to tests/fixtures/store/observations.parquet.

Run after `gridcase ingest` has loaded 2021-02-13..22, 2023-05-01..08 and 2023-07-15..08-16
for all sources. Output is deterministic (sorted, zstd) so the file is stable in git.
"""

from __future__ import annotations

from pathlib import Path

from gridcase.config import DB_PATH, ROOT
from gridcase.store.db import connect

OUT = ROOT / "tests" / "fixtures" / "store" / "observations.parquet"
WINDOWS = [("2021-02-13", "2021-02-22"), ("2023-05-01", "2023-05-08"), ("2023-07-15", "2023-08-16")]


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(DB_PATH, read_only=True)
    where = " OR ".join(f"(valid_time >= '{a}' AND valid_time < '{b}')" for a, b in WINDOWS)
    conn.execute(
        f"""
        COPY (
            SELECT * FROM as_of(TIMESTAMP '2100-01-01')
            WHERE {where}
            ORDER BY series_id, entity_id, valid_time
        ) TO '{OUT}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """
    )
    n = conn.execute(f"SELECT count(*) FROM read_parquet('{OUT}')").fetchone()
    size = Path(OUT).stat().st_size / 1e6
    print(f"wrote {OUT.relative_to(ROOT)}: {n[0] if n else 0} rows, {size:.1f} MB")


if __name__ == "__main__":
    main()
