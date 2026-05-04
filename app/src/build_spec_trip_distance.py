"""Specialized table: distance distribution statistics (Q3 — average distance)."""

from common.specialized import run

SQL = """\
SELECT
    year_month,
    company_name,
    COUNT(*)                  AS trip_count,
    AVG(trip_miles)           AS avg_miles,
    MEDIAN(trip_miles)        AS median_miles,
    APPROX_QUANTILE(trip_miles, 0.95) AS p95_miles,
    STDDEV(trip_miles)        AS stddev_miles,
    MIN(trip_miles)           AS min_miles,
    MAX(trip_miles)           AS max_miles
FROM trusted_trips
GROUP BY year_month, company_name
"""

if __name__ == "__main__":
    run(
        table_name="spec_trip_distance",
        description="Distance distribution statistics (Q3: average distance)",
        sql=SQL,
    )
