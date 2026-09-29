"""Re-derive available_at for synthetic-vintage rows from the current series lags.

Synthetic rows (backfilled history whose real vintage is unknowable) carry
available_at = min(valid_time + lag_hours, observed_at). When a series' lag_hours changes in
the registry, previously ingested rows keep the old stamp. This script recomputes it. It
never touches observed rows or values; it is a metadata correction, recorded in DECISIONS.

    uv run python scripts/restamp_synthetic_vintage.py [--db PATH]
"""

from __future__ import annotations

import argparse

from gridcase.config import DB_PATH
from gridcase.store.db import connect
from gridcase.store.series import SERIES


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=str(DB_PATH))
    args = p.parse_args()
    conn = connect(args.db)
    total = 0
    for s in SERIES:
        cur = conn.execute(
            """
            UPDATE observations
            SET available_at = least(valid_time + INTERVAL (?) HOUR, observed_at)
            WHERE series_id = ? AND vintage = 'synthetic'
              AND available_at <> least(valid_time + INTERVAL (?) HOUR, observed_at)
            """,
            [s.lag_hours, s.series_id, s.lag_hours],
        )
        n = cur.fetchone()
        changed = int(n[0]) if n else 0
        total += changed
        if changed:
            print(f"{s.series_id}: restamped {changed} rows to lag {s.lag_hours}h")
    conn.close()
    print(f"done: {total} rows restamped")


if __name__ == "__main__":
    main()
