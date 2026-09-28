"""Series registry. lag_hours is the typical publication delay after valid_time; it sets
available_at for backfilled rows (see Observation). Negative = known before the hour."""

from __future__ import annotations

from datetime import datetime

from gridcase.store.models import Series

EIA_START = datetime(2018, 7, 1)
OM_START = datetime(2018, 1, 1)

SERIES: list[Series] = [
    Series(
        series_id="eia.D",
        name="Demand (served load)",
        unit="MW",
        source="eia",
        lag_hours=1,
        history_start=EIA_START,
    ),
    Series(
        series_id="eia.DF",
        name="Day-ahead demand forecast",
        unit="MW",
        source="eia",
        lag_hours=-14,
        history_start=EIA_START,
    ),
    Series(
        series_id="eia.NG",
        name="Net generation",
        unit="MW",
        source="eia",
        lag_hours=24,
        history_start=EIA_START,
    ),
    Series(
        series_id="eia.TI",
        name="Total interchange (+ = export)",
        unit="MW",
        source="eia",
        lag_hours=24,
        history_start=EIA_START,
    ),
    Series(
        series_id="eia.fuel",
        name="Net generation by fuel type",
        unit="MW",
        source="eia",
        lag_hours=24,
        history_start=EIA_START,
    ),
    Series(
        series_id="eia.interchange",
        name="BA-to-BA interchange (+ = export)",
        unit="MW",
        source="eia",
        lag_hours=48,
        history_start=EIA_START,
    ),
    Series(
        series_id="eia.subba_demand",
        name="Subregion demand",
        unit="MW",
        source="eia",
        lag_hours=36,
        history_start=EIA_START,
    ),
    Series(
        series_id="om.temp_2m",
        name="Air temperature 2 m",
        unit="degC",
        source="openmeteo",
        lag_hours=120,
        history_start=OM_START,
    ),
    Series(
        series_id="om.wind_10m",
        name="Wind speed 10 m",
        unit="km/h",
        source="openmeteo",
        lag_hours=120,
        history_start=OM_START,
    ),
    Series(
        series_id="om.cloud_cover",
        name="Cloud cover",
        unit="pct",
        source="openmeteo",
        lag_hours=120,
        history_start=OM_START,
    ),
    Series(
        series_id="gs.lmp_da_hub",
        name="Day-ahead hub LMP",
        unit="USD/MWh",
        source="gridstatus",
        lag_hours=-12,
    ),
    Series(
        series_id="gs.lmp_rt_hub",
        name="Real-time hub LMP (hourly avg)",
        unit="USD/MWh",
        source="gridstatus",
        lag_hours=2,
    ),
]

SERIES_BY_ID: dict[str, Series] = {s.series_id: s for s in SERIES}
