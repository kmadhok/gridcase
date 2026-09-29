"""How often would the KPI alarm have fired? Replays gridcase.alarm.check_kpi once a day
(12:00 UTC, so each day's 24 settled hours are judged once) over a store's history, leak-free,
and groups alarms on consecutive days into events.

    uv run python scripts/alarm_history.py data/gridcase.duckdb [--start 2019-02-01]
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta

from gridcase import config
from gridcase.alarm import alarm_params, check_kpi
from gridcase.store.db import connect


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("db")
    p.add_argument("--start", default="2019-02-01")
    p.add_argument("--end", default=None)
    a = p.parse_args()
    conn = connect(a.db, read_only=True)
    start = datetime.fromisoformat(a.start).replace(hour=12)
    end = (
        datetime.fromisoformat(a.end)
        if a.end
        else datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    )
    for entity in config.REGIONS:
        params = alarm_params(entity)
        days, alarms, events, prev = 0, [], [], None
        t = start
        while t <= end:
            o = check_kpi(conn, entity, t, params).output
            if not o["insufficient"]:
                days += 1
                if o["alarm"]:
                    alarms.append((t.date(), o["direction"], o["worst_value"], o["run_hours"]))
                    if prev is None or (t.date() - prev).days > 1:
                        events.append([t.date(), t.date(), o["direction"], o["worst_value"]])
                    else:
                        ev = events[-1]
                        ev[1] = t.date()
                        if abs(o["worst_value"]) > abs(ev[3]):
                            ev[3] = o["worst_value"]
                    prev = t.date()
            t += timedelta(days=1)
        years = days / 365.25 if days else 0
        print(
            f"\n{entity}: {days} days judged, alarm on {len(alarms)} days, {len(events)} events"
            f" ({len(events) / years:.1f} per year)"
            if years
            else f"\n{entity}: no data"
        )
        for ev in sorted(events, key=lambda e: -abs(e[3]))[:8]:
            print(f"   {ev[0]}..{ev[1]}  {ev[2]:5s}  worst {ev[3]:+.1f}%")


if __name__ == "__main__":
    main()
