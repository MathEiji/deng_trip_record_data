"""Build the SPECIALIZED layer from trusted FHVHV trip data stored in S3.

Reads the ``trusted_trips`` table and produces four pre-aggregated tables
designed to answer specific business questions:

    spec_hourly_volume  – trip volume by hour of day   (Q1)
    spec_daily_volume   – trip volume by day of week   (Q2)
    spec_trip_distance  – distance distribution stats  (Q3)
    spec_distance_fare  – distance ↔ fare relationship (Q4)

Each table is Hive-partitioned by ``year_month`` (yyyyMM integer) and
registered in the AWS Glue Data Catalog.

Configuration via environment variables:

    S3_BUCKET              – S3 bucket (required)
    START_MONTH            – first month, YYYY-MM (required)
    END_MONTH              – last  month, YYYY-MM (required)
    S3_TRUSTED_PREFIX      – trusted layer prefix      (default: "trusted")
    S3_SPECIALIZED_PREFIX  – specialized output prefix  (default: "specialized")
    GLUE_DATABASE          – Glue database name         (default: "trip_record_data")
    AWS_REGION             – AWS region                 (default: "us-east-1")
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

# ── Glue helpers ─────────────────────────────────────────────────────────

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

DB_PATH = Path("/tmp/_build_specialized.duckdb")

# ── Specialized table SQL definitions ────────────────────────────────────

SPECIALIZED_TABLES: dict[str, tuple[str, str]] = {
    "spec_hourly_volume": (
        "Trip volume by hour of day (Q1: peak hours)",
        """\
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
        """,
    ),
    "spec_daily_volume": (
        "Trip volume by day of week (Q2: peak weekdays)",
        """\
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
        """,
    ),
    "spec_trip_distance": (
        "Distance distribution statistics (Q3: average distance)",
        """\
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
        """,
    ),
    "spec_distance_fare": (
        "Distance vs fare relationship (Q4)",
        """\
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
        """,
    ),
}


# ── Helpers ──────────────────────────────────────────────────────────────

def month_range(start: str, end: str) -> list[str]:
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
    con = duckdb.connect(str(DB_PATH))
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("INSTALL aws; LOAD aws;")
    con.execute(f"SET s3_region = '{region}';")
    con.execute("CREATE SECRET (TYPE S3, PROVIDER CREDENTIAL_CHAIN);")
    return con


def cleanup_duckdb() -> None:
    for p in (DB_PATH, DB_PATH.with_suffix(".duckdb.wal")):
        if p.exists():
            p.unlink()


# ── Build ────────────────────────────────────────────────────────────────

def create_trusted_view(
    con: duckdb.DuckDBPyConnection,
    bucket: str,
    trusted_prefix: str,
    year_months_csv: str,
) -> int:
    """Register a lazy view over trusted_trips and return the row count."""
    glob = f"s3://{bucket}/{trusted_prefix}/trusted_trips/**/*.parquet"
    con.execute(f"""
        CREATE OR REPLACE VIEW trusted_trips AS
        SELECT *
        FROM read_parquet('{glob}', hive_partitioning=true)
        WHERE year_month IN ({year_months_csv})
    """)
    n = con.execute("SELECT count(*) FROM trusted_trips").fetchone()[0]
    log.info("  VIEW trusted_trips -> %s  (%s rows)", glob, f"{n:,}")
    return n


def build_specialized_tables(
    con: duckdb.DuckDBPyConnection,
    bucket: str,
    spec_prefix: str,
) -> dict[str, str]:
    """Run each aggregation query and write partitioned output to S3.

    Returns ``{table_name: s3_base_directory}``.
    """
    log.info("=" * 70)
    log.info("BUILDING SPECIALIZED LAYER")
    log.info("=" * 70)

    table_paths: dict[str, str] = {}

    for table_name, (description, sql) in SPECIALIZED_TABLES.items():
        t0 = time.time()
        s3_dir = f"s3://{bucket}/{spec_prefix}/{table_name}"

        con.execute(f"""
            COPY (
                {sql}
            ) TO '{s3_dir}' (FORMAT PARQUET, PARTITION_BY (year_month), OVERWRITE_OR_IGNORE)
        """)

        glob = f"{s3_dir}/**/*.parquet"
        n = con.execute(
            f"SELECT count(*) FROM read_parquet('{glob}', hive_partitioning=true)"
        ).fetchone()[0]
        elapsed = time.time() - t0
        log.info(
            "  %-25s %6s rows  (%.1fs)  %s",
            table_name, f"{n:,}", elapsed, description,
        )
        table_paths[table_name] = s3_dir

    return table_paths


# ── Validation ───────────────────────────────────────────────────────────

def validate_specialized(
    con: duckdb.DuckDBPyConnection,
    table_paths: dict[str, str],
    trusted_count: int,
    year_months: list[int],
) -> bool:
    """Verify each table is non-empty and hourly counts sum correctly."""
    log.info("=" * 70)
    log.info("VALIDATION")
    log.info("=" * 70)

    all_ok = True
    ym_csv = ", ".join(str(ym) for ym in year_months)

    def _glob(s3_dir: str) -> str:
        return f"{s3_dir}/**/*.parquet"

    for table_name, s3_dir in table_paths.items():
        n = con.execute(f"""
            SELECT count(*)
            FROM read_parquet('{_glob(s3_dir)}', hive_partitioning=true)
            WHERE year_month IN ({ym_csv})
        """).fetchone()[0]
        ok = n > 0
        if not ok:
            all_ok = False
        log.info("  [%s] %-25s %6s rows", "OK" if ok else "FAIL", table_name, f"{n:,}")

    hourly_sum = con.execute(f"""
        SELECT SUM(trip_count)
        FROM read_parquet('{_glob(table_paths["spec_hourly_volume"])}', hive_partitioning=true)
        WHERE year_month IN ({ym_csv})
    """).fetchone()[0]
    ok = hourly_sum == trusted_count
    if not ok:
        all_ok = False
    log.info(
        "  [%s] Hourly trip_count sum: %s (expected %s)",
        "OK" if ok else "FAIL", f"{hourly_sum:,}", f"{trusted_count:,}",
    )

    daily_sum = con.execute(f"""
        SELECT SUM(trip_count)
        FROM read_parquet('{_glob(table_paths["spec_daily_volume"])}', hive_partitioning=true)
        WHERE year_month IN ({ym_csv})
    """).fetchone()[0]
    ok = daily_sum == trusted_count
    if not ok:
        all_ok = False
    log.info(
        "  [%s] Daily trip_count sum:  %s (expected %s)",
        "OK" if ok else "FAIL", f"{daily_sum:,}", f"{trusted_count:,}",
    )

    return all_ok


# ── Glue Data Catalog ────────────────────────────────────────────────────

def _glue_columns_from_parquet(
    con: duckdb.DuckDBPyConnection,
    s3_glob: str,
    *,
    hive_partitioning: bool = False,
    exclude: set[str] | None = None,
) -> list[dict]:
    hp = ", hive_partitioning=true" if hive_partitioning else ""
    schema = con.execute(
        f"DESCRIBE SELECT * FROM read_parquet('{s3_glob}'{hp})"
    ).fetchall()
    skip = exclude or set()
    return [
        {
            "Name": col_name,
            "Type": DUCKDB_TO_GLUE_TYPE.get(col_type, "string"),
        }
        for col_name, col_type, *_ in schema
        if col_name not in skip
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
    table_input: dict = {
        "Name": table_name,
        "Description": description or f"Specialized layer: {table_name}",
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
    """Create or update Glue partitions for each year_month value."""
    existing: set[tuple] = set()
    paginator = glue_client.get_paginator("get_partitions")
    for page in paginator.paginate(DatabaseName=database, TableName=table_name):
        for p in page.get("Partitions", []):
            existing.add(tuple(p["Values"]))

    to_create: list[dict] = []
    to_update: list[dict] = []
    for ym in year_months:
        part_input = {
            "Values": [str(ym)],
            "StorageDescriptor": {
                "Columns": columns,
                "Location": f"{s3_location}year_month={ym}/",
                **_PARQUET_SERDE,
            },
        }
        if (str(ym),) in existing:
            to_update.append({"PartitionValueList": [str(ym)], "PartitionInput": part_input})
        else:
            to_create.append(part_input)

    batch = 100
    if to_create:
        for i in range(0, len(to_create), batch):
            glue_client.batch_create_partition(
                DatabaseName=database,
                TableName=table_name,
                PartitionInputList=to_create[i : i + batch],
            )
    if to_update:
        for i in range(0, len(to_update), batch):
            glue_client.batch_update_partition(
                DatabaseName=database,
                TableName=table_name,
                Entries=to_update[i : i + batch],
            )

    return len(to_create) + len(to_update)


def register_specialized_tables(
    con: duckdb.DuckDBPyConnection,
    glue_client,
    database: str,
    table_paths: dict[str, str],
    bucket: str,
    spec_prefix: str,
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

    partition_keys = [{"Name": "year_month", "Type": "int"}]

    for table_name, s3_dir in table_paths.items():
        description = SPECIALIZED_TABLES[table_name][0]
        s3_location = f"s3://{bucket}/{spec_prefix}/{table_name}/"
        s3_glob = f"{s3_dir}/**/*.parquet"
        columns = _glue_columns_from_parquet(
            con, s3_glob, hive_partitioning=True, exclude={"year_month"},
        )

        action = _upsert_glue_table(
            glue_client, database, table_name,
            s3_location, columns, description,
            partition_keys=partition_keys,
        )
        log.info(
            "  [%s] %s.%-25s (%d cols) -> %s",
            action, database, table_name, len(columns), s3_location,
        )

        n_parts = _register_glue_partitions(
            glue_client, database, table_name,
            s3_location, columns, year_months,
        )
        log.info("    %d partition(s) registered", n_parts)


# ── Main ─────────────────────────────────────────────────────────────────

def main() -> None:
    t_start = time.time()

    bucket = os.environ.get("S3_BUCKET")
    start = os.environ.get("START_MONTH")
    end = os.environ.get("END_MONTH")
    trusted_prefix = os.environ.get("S3_TRUSTED_PREFIX", "trusted").strip("/")
    spec_prefix = os.environ.get("S3_SPECIALIZED_PREFIX", "specialized").strip("/")
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
    log.info("SPECIALIZED LAYER BUILD")
    log.info("  Months          : %s -> %s (%d)", start, end, len(months))
    log.info("  Trusted input   : s3://%s/%s/", bucket, trusted_prefix)
    log.info("  Specialized out : s3://%s/%s/", bucket, spec_prefix)
    log.info("  Glue database   : %s", glue_database)
    log.info("  Tables          : %s", ", ".join(SPECIALIZED_TABLES))
    log.info("=" * 70)

    con = init_duckdb(region)

    try:
        log.info("Creating trusted view …")
        trusted_count = create_trusted_view(
            con, bucket, trusted_prefix, year_months_csv,
        )

        table_paths = build_specialized_tables(con, bucket, spec_prefix)
        ok = validate_specialized(con, table_paths, trusted_count, year_months)

        register_specialized_tables(
            con, glue_client, glue_database,
            table_paths, bucket, spec_prefix,
            year_months,
        )
    finally:
        con.close()
        cleanup_duckdb()

    elapsed = time.time() - t_start
    log.info("=" * 70)
    status = "BUILD COMPLETE" if ok else "BUILD COMPLETED WITH WARNINGS"
    log.info(status)
    log.info("  Total time : %.1fs", elapsed)
    log.info("  Input rows : %s", f"{trusted_count:,}")
    log.info("  Tables     : %d", len(table_paths))
    log.info("  Output     : s3://%s/%s/", bucket, spec_prefix)
    log.info("  Glue DB    : %s", glue_database)
    log.info("=" * 70)

    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
