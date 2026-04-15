"""Specialized table: distance vs fare relationship (Q4)."""

from _specialized_common import run

SQL = """\
SELECT
    year_month,
    company_name,
    CASE
        WHEN trip_miles <= 2  THEN '0-2 mi'
        WHEN trip_miles <= 5  THEN '2-5 mi'
        WHEN trip_miles <= 10 THEN '5-10 mi'
        WHEN trip_miles <= 20 THEN '10-20 mi'
        ELSE '20+ mi'
    END AS distance_bucket,
    COUNT(*)                       AS trip_count,
    AVG(base_passenger_fare)       AS avg_base_fare,
    AVG(total_fare)                AS avg_total_fare,
    AVG(fare_per_mile)             AS avg_fare_per_mile,
    AVG(tips)                      AS avg_tips,
    AVG(trip_duration_seconds)     AS avg_duration_seconds
FROM trusted_trips
GROUP BY year_month, company_name, distance_bucket
"""

if __name__ == "__main__":
    run(
        table_name="spec_distance_fare",
        description="Distance vs fare relationship (Q4)",
        sql=SQL,
    )
