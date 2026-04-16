"""Specialized-layer entrypoint shared by each build_spec_*.py script.

Provides the build/validate/register lifecycle via ``run()``.
"""

import logging
import os
import sys
import time
from pathlib import Path

import boto3
import duckdb

from common.pipeline import (
    cleanup_duckdb,
    ensure_glue_database,
    glue_columns_from_parquet,
    init_duckdb,
    month_range,
    register_glue_partitions,
    upsert_glue_table,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("specialized")

DB_PATH = Path("/tmp/_build_specialized.duckdb")


def _create_trusted_view(
    con: duckdb.DuckDBPyConnection,
    bucket: str,
    trusted_prefix: str,
    year_months_csv: str,
) -> int:
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


def _build(
    con: duckdb.DuckDBPyConnection,
    bucket: str,
    spec_prefix: str,
    table_name: str,
    sql: str,
) -> tuple[str, int]:
    log.info("=" * 70)
    log.info("BUILDING %s", table_name)
    log.info("=" * 70)

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
    log.info("  %s rows  (%.1fs)", f"{n:,}", elapsed)

    return s3_dir, n


def _validate(
    con: duckdb.DuckDBPyConnection,
    table_name: str,
    s3_dir: str,
    row_count: int,
    trusted_count: int,
    year_months: list[int],
    *,
    has_trip_count: bool = False,
) -> bool:
    log.info("=" * 70)
    log.info("VALIDATION")
    log.info("=" * 70)

    all_ok = True

    ok = row_count > 0
    if not ok:
        all_ok = False
    log.info("  [%s] %-25s %6s rows", "OK" if ok else "FAIL", table_name, f"{row_count:,}")

    if has_trip_count:
        ym_csv = ", ".join(str(ym) for ym in year_months)
        glob = f"{s3_dir}/**/*.parquet"
        trip_sum = con.execute(f"""
            SELECT SUM(trip_count)
            FROM read_parquet('{glob}', hive_partitioning=true)
            WHERE year_month IN ({ym_csv})
        """).fetchone()[0]
        ok = trip_sum == trusted_count
        if not ok:
            all_ok = False
        log.info(
            "  [%s] trip_count sum: %s (expected %s)",
            "OK" if ok else "FAIL", f"{trip_sum:,}", f"{trusted_count:,}",
        )

    return all_ok


def _register_glue(
    con: duckdb.DuckDBPyConnection,
    glue_client,
    database: str,
    table_name: str,
    description: str,
    s3_dir: str,
    bucket: str,
    spec_prefix: str,
    year_months: list[int],
) -> None:
    log.info("=" * 70)
    log.info("GLUE DATA CATALOG REGISTRATION")
    log.info("=" * 70)

    ensure_glue_database(glue_client, database)

    partition_keys = [{"Name": "year_month", "Type": "int"}]
    s3_location = f"s3://{bucket}/{spec_prefix}/{table_name}/"
    s3_glob = f"{s3_dir}/**/*.parquet"
    columns = glue_columns_from_parquet(
        con, s3_glob, hive_partitioning=True, exclude={"year_month"},
    )

    action = upsert_glue_table(
        glue_client, database, table_name,
        s3_location, columns, description,
        partition_keys=partition_keys,
    )
    log.info(
        "  [%s] %s.%-25s (%d cols) -> %s",
        action, database, table_name, len(columns), s3_location,
    )

    n_parts = register_glue_partitions(
        glue_client, database, table_name,
        s3_location, columns, year_months,
    )
    log.info("    %d partition(s) registered", n_parts)


def run(
    *,
    table_name: str,
    description: str,
    sql: str,
    has_trip_count: bool = False,
) -> None:
    """Full lifecycle: read config, build, validate, register in Glue."""
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
    log.info("  Table           : %s", table_name)
    log.info("  Months          : %s -> %s (%d)", start, end, len(months))
    log.info("  Trusted input   : s3://%s/%s/", bucket, trusted_prefix)
    log.info("  Specialized out : s3://%s/%s/", bucket, spec_prefix)
    log.info("  Glue database   : %s", glue_database)
    log.info("=" * 70)

    con = init_duckdb(DB_PATH, region)

    try:
        log.info("Creating trusted view …")
        trusted_count = _create_trusted_view(
            con, bucket, trusted_prefix, year_months_csv,
        )

        s3_dir, row_count = _build(
            con, bucket, spec_prefix, table_name, sql,
        )
        ok = _validate(
            con, table_name, s3_dir, row_count,
            trusted_count, year_months,
            has_trip_count=has_trip_count,
        )

        _register_glue(
            con, glue_client, glue_database,
            table_name, description, s3_dir,
            bucket, spec_prefix, year_months,
        )
    finally:
        con.close()
        cleanup_duckdb(DB_PATH)

    elapsed = time.time() - t_start
    log.info("=" * 70)
    status = "BUILD COMPLETE" if ok else "BUILD COMPLETED WITH WARNINGS"
    log.info(status)
    log.info("  Table      : %s", table_name)
    log.info("  Total time : %.1fs", elapsed)
    log.info("  Input rows : %s", f"{trusted_count:,}")
    log.info("  Output     : %s/", s3_dir)
    log.info("  Glue DB    : %s", glue_database)
    log.info("=" * 70)

    if not ok:
        sys.exit(1)
