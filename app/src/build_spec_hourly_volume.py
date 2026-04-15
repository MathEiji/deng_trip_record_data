"""Specialized table: trip volume by hour of day (Q1 — peak hours)."""

from _specialized_common import run

SQL = """\
SELECT
    year_month,
    company_name,
    pickup_hour,
    COUNT(*)             AS trip_count,
    AVG(trip_miles)      AS avg_trip_miles,
    AVG(total_fare)      AS avg_total_fare,
    AVG(trip_duration_seconds) AS avg_duration_seconds
FROM trusted_trips
GROUP BY year_month, company_name, pickup_hour
"""

if __name__ == "__main__":
    run(
        table_name="spec_hourly_volume",
        description="Trip volume by hour of day (Q1: peak hours)",
        sql=SQL,
        has_trip_count=True,
    )
