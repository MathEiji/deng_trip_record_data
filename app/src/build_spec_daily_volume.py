"""Specialized table: trip volume by day of week (Q2 — peak weekdays)."""

from _specialized_common import run

SQL = """\
SELECT
    year_month,
    company_name,
    pickup_day_of_week,
    pickup_day_name,
    COUNT(*)             AS trip_count,
    AVG(trip_miles)      AS avg_trip_miles,
    AVG(total_fare)      AS avg_total_fare,
    AVG(trip_duration_seconds) AS avg_duration_seconds
FROM trusted_trips
GROUP BY year_month, company_name, pickup_day_of_week, pickup_day_name
"""

if __name__ == "__main__":
    run(
        table_name="spec_daily_volume",
        description="Trip volume by day of week (Q2: peak weekdays)",
        sql=SQL,
        has_trip_count=True,
    )
