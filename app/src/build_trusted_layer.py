"""Build the TRUSTED layer from raw FHVHV trip data stored in S3.

Reads raw context tables and dimension tables from S3, joins them into
a single denormalized ``trusted_trips`` table, applies data-quality
filters, computes derived fields, writes Hive-partitioned parquet back
to S3, and registers the table in the AWS Glue Data Catalog.

Designed to run inside an ECS Fargate task.  Configuration is read from
environment variables:

    S3_BUCKET              – S3 bucket (required)
    START_MONTH            – first month to process, YYYY-MM (required)
    END_MONTH              – last  month to process, YYYY-MM (required)
    S3_RAW_PREFIX          – raw layer key prefix   (default: "raw")
    S3_TRUSTED_PREFIX      – trusted output prefix  (default: "trusted")
    GLUE_DATABASE          – Glue database name     (default: "trip_record_data")
    AWS_REGION             – AWS region             (default: "us-east-1")
"""

import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import boto3
import duckdb

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Glue helpers (shared constants) ──────────────────────────────────────

DUCKDB_TO_GLUE_TYPE = {
    "VARCHAR": "string",
    "BIGINT": "bigint",
    "INTEGER": "int",
    "SMALLINT": "smallint",
    "TINYINT": "tinyint",
    "DOUBLE": "double",
    "FLOAT": "float",
    "REAL": "float",
    "TIMESTAMP": "timestamp",
    "TIMESTAMP WITH TIME ZONE": "timestamp",
    "TIMESTAMP_S": "timestamp",
    "TIMESTAMP_MS": "timestamp",
    "TIMESTAMP_NS": "timestamp",
    "DATE": "date",
    "BOOLEAN": "boolean",
    "BLOB": "binary",
}

_PARQUET_SERDE = {
    "InputFormat": (
        "org.apache.hadoop.hive.ql.io.parquet"
        ".MapredParquetInputFormat"
    ),
    "OutputFormat": (
        "org.apache.hadoop.hive.ql.io.parquet"
        ".MapredParquetOutputFormat"
    ),
    "SerdeInfo": {
        "SerializationLibrary": (
            "org.apache.hadoop.hive.ql.io.parquet"
            ".serde.ParquetHiveSerDe"
        ),
    },
}

DB_PATH = Path("/tmp/_build_trusted.duckdb")

RAW_TABLES = [
    "raw_dispatch_base",
    "raw_trip_time_location",
    "raw_fare_payment",
    "raw_request_flags",
]

TRUSTED_SQL = """\
SELECT
    d.trip_id,
    current_timestamp                                                AS processed_date,
    d.year_month,
    dim.company_name,
    d.hvfhs_license_num,
    t.request_datetime,
    t.pickup_datetime,
    t.dropoff_datetime,
    CAST(t.pickup_datetime AS DATE)                                  AS pickup_date,
    CAST(EXTRACT(HOUR FROM t.pickup_datetime) AS SMALLINT)           AS pickup_hour,
    CAST(EXTRACT(DOW  FROM t.pickup_datetime) AS SMALLINT)           AS pickup_day_of_week,
    DAYNAME(t.pickup_datetime)                                       AS pickup_day_name,
    CAST(EPOCH(t.pickup_datetime  - t.request_datetime) AS INTEGER)  AS wait_time_seconds,
    CAST(EPOCH(t.dropoff_datetime - t.pickup_datetime)  AS INTEGER)  AS trip_duration_seconds,
    t.trip_miles,
    t.trip_time                                                      AS trip_time_seconds,
    t."PULocationID"                                                 AS pickup_location_id,
    t."DOLocationID"                                                 AS dropoff_location_id,
    f.base_passenger_fare,
    f.tolls,
    f.congestion_surcharge,
    f.airport_fee,
    f.tips,
    f.driver_pay,
    (f.base_passenger_fare + f.tolls + f.bcf + f.sales_tax
     + f.congestion_surcharge + f.airport_fee + f.tips
     + f.cbd_congestion_fee)                                         AS total_fare,
    (f.base_passenger_fare + f.tolls + f.bcf + f.sales_tax
     + f.congestion_surcharge + f.airport_fee + f.tips
     + f.cbd_congestion_fee) / t.trip_miles                          AS fare_per_mile,
    (r.shared_request_flag = 'Y')                                    AS is_shared_request,
    (r.shared_match_flag   = 'Y')                                    AS is_shared_match,
    (r.wav_match_flag      = 'Y')                                    AS is_wav_match
FROM raw_dispatch_base d
JOIN raw_trip_time_location t ON d.trip_id = t.trip_id AND d.year_month = t.year_month
JOIN raw_fare_payment f       ON d.trip_id = f.trip_id AND d.year_month = f.year_month
JOIN raw_request_flags r      ON d.trip_id = r.trip_id AND d.year_month = r.year_month
LEFT JOIN dim_hvfhs_license dim ON d.hvfhs_license_num = dim.hvfhs_license_num
WHERE t.trip_miles > 0
  AND t.trip_miles <= 200
  AND t.trip_time > 0
  AND t.trip_time <= 14400
  AND f.base_passenger_fare > 0
  AND f.base_passenger_fare <= 500
  AND t.dropoff_datetime > t.pickup_datetime
  AND t.pickup_datetime IS NOT NULL
  AND t.dropoff_datetime IS NOT NULL
"""


# ── Helpers ──────────────────────────────────────────────────────────────

def month_range(start: str, end: str) -> list[str]:
    """Return a list of 'YYYY-MM' strings from *start* to *end* inclusive."""
    start_dt = datetime.strptime(start, "%Y-%m")
    end_dt = datetime.strptime(end, "%Y-%m")
    if start_dt > end_dt:
        raise ValueError(f"Start {start} is after end {end}")
    months: list[str] = []
    current = start_dt
    while current <= end_dt:
        months.append(current.strftime("%Y-%m"))
        if current.month == 12:
            current = current.replace(year=current.year + 1, month=1)
        else:
            current = current.replace(month=current.month + 1)
    return months


def init_duckdb(region: str) -> duckdb.DuckDBPyConnection:
    """Persistent DuckDB with S3 via IAM credential chain."""
    con = duckdb.connect(str(DB_PATH))
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("INSTALL aws; LOAD aws;")
    con.execute(f"SET s3_region = '{region}';")
    con.execute("SET memory_limit = '3GB';")
    con.execute("CREATE SECRET (TYPE S3, PROVIDER CREDENTIAL_CHAIN);")
    return con


def cleanup_duckdb() -> None:
    for p in (DB_PATH, DB_PATH.with_suffix(".duckdb.wal")):
        if p.exists():
            p.unlink()


def _raw_glob(bucket: str, prefix: str, table: str) -> str:
    return f"s3://{bucket}/{prefix}/{table}/**/*.parquet"


def _dim_path(bucket: str, prefix: str, name: str) -> str:
    return f"s3://{bucket}/{prefix}/{name}/{name}.parquet"


# ── Views ────────────────────────────────────────────────────────────────

def create_source_views(
    con: duckdb.DuckDBPyConnection,
    bucket: str,
    raw_prefix: str,
    year_months_csv: str,
) -> None:
    """Register lazy views over the raw S3 parquets and dimension table."""
    for table in RAW_TABLES:
        glob = _raw_glob(bucket, raw_prefix, table)
        con.execute(f"""
            CREATE OR REPLACE VIEW {table} AS
            SELECT *
            FROM read_parquet('{glob}', hive_partitioning=true)
            WHERE year_month IN ({year_months_csv})
        """)
        log.info("  VIEW %-30s -> %s", table, glob)

    dim_p = _dim_path(bucket, raw_prefix, "dim_hvfhs_license")
    con.execute(f"""
        CREATE OR REPLACE VIEW dim_hvfhs_license AS
        SELECT * FROM read_parquet('{dim_p}')
    """)
    log.info("  VIEW %-30s -> %s", "dim_hvfhs_license", dim_p)


# ── Build ────────────────────────────────────────────────────────────────

def build_trusted_trips(
    con: duckdb.DuckDBPyConnection,
    bucket: str,
    trusted_prefix: str,
) -> tuple[str, int, int]:
    """Join raw tables, filter, derive, and write trusted_trips to S3.

    Returns ``(s3_dir, raw_count, trusted_count)``.
    """
    log.info("=" * 70)
    log.info("BUILDING TRUSTED LAYER")
    log.info("=" * 70)

    raw_count = con.execute(
        "SELECT count(*) FROM raw_dispatch_base"
    ).fetchone()[0]
    log.info("Raw input rows: %s", f"{raw_count:,}")

    s3_dir = f"s3://{bucket}/{trusted_prefix}/trusted_trips"

    log.info("Running trusted query + COPY to %s …", s3_dir)
    t0 = time.time()
    con.execute(f"""
        COPY (
            {TRUSTED_SQL}
        ) TO '{s3_dir}' (FORMAT PARQUET, PARTITION_BY (year_month), OVERWRITE_OR_IGNORE)
    """)
    elapsed = time.time() - t0
    log.info("  COPY finished in %.1fs", elapsed)

    trusted_glob = f"{s3_dir}/**/*.parquet"
    trusted_count = con.execute(
        f"SELECT count(*) FROM read_parquet('{trusted_glob}', hive_partitioning=true)"
    ).fetchone()[0]

    filtered = raw_count - trusted_count
    pct = filtered / raw_count * 100 if raw_count else 0
    log.info(
        "  Trusted rows : %s  (filtered %s = %.2f%%)",
        f"{trusted_count:,}", f"{filtered:,}", pct,
    )
    return s3_dir, raw_count, trusted_count


# ── Validation ───────────────────────────────────────────────────────────

def validate_trusted(
    con: duckdb.DuckDBPyConnection,
    s3_dir: str,
    trusted_count: int,
) -> bool:
    """Sanity-check the output parquet."""
    log.info("=" * 70)
    log.info("VALIDATION")
    log.info("=" * 70)

    glob = f"{s3_dir}/**/*.parquet"
    all_ok = True

    n = con.execute(
        f"SELECT count(*) FROM read_parquet('{glob}', hive_partitioning=true)"
    ).fetchone()[0]
    ok = n == trusted_count
    if not ok:
        all_ok = False
    log.info("  [%s] Row count: %s", "OK" if ok else "FAIL", f"{n:,}")

    # Null check on key derived columns
    null_check = con.execute(f"""
        SELECT
            SUM(CASE WHEN company_name       IS NULL THEN 1 ELSE 0 END),
            SUM(CASE WHEN pickup_hour        IS NULL THEN 1 ELSE 0 END),
            SUM(CASE WHEN pickup_day_of_week IS NULL THEN 1 ELSE 0 END),
            SUM(CASE WHEN total_fare         IS NULL THEN 1 ELSE 0 END),
            SUM(CASE WHEN fare_per_mile      IS NULL THEN 1 ELSE 0 END)
        FROM read_parquet('{glob}', hive_partitioning=true)
    """).fetchone()
    cols = ["company_name", "pickup_hour", "pickup_day_of_week",
            "total_fare", "fare_per_mile"]
    for col_name, null_count in zip(cols, null_check):
        ok = null_count == 0
        if not ok:
            all_ok = False
        log.info(
            "  [%s] %s nulls: %s",
            "OK" if ok else "WARN", col_name, f"{null_count:,}",
        )

    # Range checks
    ranges = con.execute(f"""
        SELECT
            MIN(trip_miles), MAX(trip_miles),
            MIN(base_passenger_fare), MAX(base_passenger_fare),
            MIN(trip_time_seconds), MAX(trip_time_seconds)
        FROM read_parquet('{glob}', hive_partitioning=true)
    """).fetchone()
    log.info(
        "  Ranges: trip_miles=[%.2f, %.2f]  fare=[%.2f, %.2f]  time=[%d, %d]",
        *ranges,
    )

    return all_ok


# ── Glue Data Catalog ────────────────────────────────────────────────────

def _glue_columns_from_parquet(
    con: duckdb.DuckDBPyConnection,
    s3_path: str,
    hive_partitioning: bool = False,
    exclude: set[str] | None = None,
) -> list[dict]:
    hp = ", hive_partitioning=true" if hive_partitioning else ""
    schema = con.execute(
        f"DESCRIBE SELECT * FROM read_parquet('{s3_path}'{hp})"
    ).fetchall()
    excl = exclude or set()
    return [
        {
            "Name": col_name,
            "Type": DUCKDB_TO_GLUE_TYPE.get(col_type, "string"),
        }
        for col_name, col_type, *_ in schema
        if col_name not in excl
    ]


def _upsert_glue_table(
    glue_client,
    database: str,
    table_name: str,
    s3_location: str,
    columns: list[dict],
    description: str = "",
    partition_keys: list[dict] | None = None,
) -> str:
    table_input = {
        "Name": table_name,
        "Description": description or f"Trusted layer: {table_name}",
        "StorageDescriptor": {
            "Columns": columns,
            "Location": s3_location,
            **_PARQUET_SERDE,
        },
        "TableType": "EXTERNAL_TABLE",
        "Parameters": {
            "classification": "parquet",
            "typeOfData": "file",
        },
    }
    if partition_keys:
        table_input["PartitionKeys"] = partition_keys

    try:
        glue_client.update_table(
            DatabaseName=database, TableInput=table_input,
        )
        return "updated"
    except glue_client.exceptions.EntityNotFoundException:
        glue_client.create_table(
            DatabaseName=database, TableInput=table_input,
        )
        return "created"


def _register_glue_partitions(
    glue_client,
    database: str,
    table_name: str,
    s3_location: str,
    columns: list[dict],
    year_months: list[int],
) -> int:
    partition_inputs = [
        {
            "Values": [str(ym)],
            "StorageDescriptor": {
                "Columns": columns,
                "Location": f"{s3_location}year_month={ym}/",
                **_PARQUET_SERDE,
            },
        }
        for ym in year_months
    ]

    registered = 0
    for i in range(0, len(partition_inputs), 100):
        batch = partition_inputs[i : i + 100]
        resp = glue_client.batch_create_partition(
            DatabaseName=database,
            TableName=table_name,
            PartitionInputList=batch,
        )
        errors = resp.get("Errors", [])
        registered += len(batch) - len(errors)

        to_update = []
        for err in errors:
            if err["ErrorDetail"]["ErrorCode"] == "AlreadyExistsException":
                vals = err["PartitionValues"]
                matching = [p for p in batch if p["Values"] == vals]
                if matching:
                    to_update.append(matching[0])
            else:
                log.warning(
                    "  Partition %s: %s",
                    err["PartitionValues"],
                    err["ErrorDetail"]["ErrorMessage"],
                )

        if to_update:
            glue_client.batch_update_partition(
                DatabaseName=database,
                TableName=table_name,
                Entries=[
                    {
                        "PartitionValueList": p["Values"],
                        "PartitionInput": p,
                    }
                    for p in to_update
                ],
            )
            registered += len(to_update)

    return registered


def register_trusted_table(
    con: duckdb.DuckDBPyConnection,
    glue_client,
    database: str,
    s3_dir: str,
    bucket: str,
    trusted_prefix: str,
    year_months: list[int],
) -> None:
    log.info("=" * 70)
    log.info("GLUE DATA CATALOG REGISTRATION")
    log.info("=" * 70)

    try:
        glue_client.create_database(
            DatabaseInput={
                "Name": database,
                "Description": "NYC TLC FHVHV trip record data",
            }
        )
        log.info("Created Glue database: %s", database)
    except glue_client.exceptions.AlreadyExistsException:
        log.info("Glue database exists: %s", database)

    glob = f"{s3_dir}/**/*.parquet"
    s3_location = f"s3://{bucket}/{trusted_prefix}/trusted_trips/"
    columns = _glue_columns_from_parquet(
        con, glob, hive_partitioning=True, exclude={"year_month"},
    )
    partition_keys = [{"Name": "year_month", "Type": "int"}]

    action = _upsert_glue_table(
        glue_client, database, "trusted_trips", s3_location,
        columns, "Trusted layer: denormalized, cleaned trip data",
        partition_keys,
    )
    log.info(
        "  [%s] %s.trusted_trips (%d cols) -> %s",
        action, database, len(columns), s3_location,
    )

    n_parts = _register_glue_partitions(
        glue_client, database, "trusted_trips",
        s3_location, columns, year_months,
    )
    log.info("         %d partition(s) registered", n_parts)


# ── Main ─────────────────────────────────────────────────────────────────

def main() -> None:
    t_start = time.time()

    bucket = os.environ.get("S3_BUCKET")
    start = os.environ.get("START_MONTH")
    end = os.environ.get("END_MONTH")
    raw_prefix = os.environ.get("S3_RAW_PREFIX", "raw").strip("/")
    trusted_prefix = os.environ.get("S3_TRUSTED_PREFIX", "trusted").strip("/")
    glue_database = os.environ.get("GLUE_DATABASE", "trip_record_data")
    region = os.environ.get("AWS_REGION", "us-east-1")

    if not bucket:
        log.error("S3_BUCKET is required")
        sys.exit(1)
    if not start or not end:
        log.error("START_MONTH and END_MONTH are required")
        sys.exit(1)

    months = month_range(start, end)
    year_months = [int(ym.replace("-", "")) for ym in months]
    year_months_csv = ", ".join(str(ym) for ym in year_months)
    glue_client = boto3.client("glue", region_name=region)

    log.info("=" * 70)
    log.info("TRUSTED LAYER BUILD")
    log.info("  Months        : %s -> %s (%d)", start, end, len(months))
    log.info("  Raw input     : s3://%s/%s/", bucket, raw_prefix)
    log.info("  Trusted output: s3://%s/%s/", bucket, trusted_prefix)
    log.info("  Glue database : %s", glue_database)
    log.info("=" * 70)

    con = init_duckdb(region)

    try:
        log.info("Creating source views …")
        create_source_views(con, bucket, raw_prefix, year_months_csv)

        s3_dir, raw_count, trusted_count = build_trusted_trips(
            con, bucket, trusted_prefix,
        )
        ok = validate_trusted(con, s3_dir, trusted_count)

        register_trusted_table(
            con, glue_client, glue_database,
            s3_dir, bucket, trusted_prefix, year_months,
        )
    finally:
        con.close()
        cleanup_duckdb()

    elapsed = time.time() - t_start
    log.info("=" * 70)
    status = "BUILD COMPLETE" if ok else "BUILD COMPLETED WITH WARNINGS"
    log.info(status)
    log.info("  Total time    : %.1fs", elapsed)
    log.info("  Raw rows      : %s", f"{raw_count:,}")
    log.info("  Trusted rows  : %s", f"{trusted_count:,}")
    log.info("  Filtered      : %s", f"{raw_count - trusted_count:,}")
    log.info("  Output        : s3://%s/%s/trusted_trips/", bucket, trusted_prefix)
    log.info("  Glue table    : %s.trusted_trips", glue_database)
    log.info("=" * 70)

    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
