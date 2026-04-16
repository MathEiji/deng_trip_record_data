# S3_BUCKET              – bucket for input/output (required)
# START_MONTH / END_MONTH – YYYY-MM range (required)
# S3_STAGING_PREFIX      – staging key prefix  (default: "staging")
# S3_RAW_PREFIX          – raw output prefix   (default: "raw")
# GLUE_DATABASE          – Glue database name  (default: "trip_record_data")
# AWS_REGION             – AWS region          (default: "us-east-1")
# SKIP_QUALITY_ANALYSIS  – "true" to skip profiling

import os
import sys
import time
from pathlib import Path

import boto3
import duckdb

from common.pipeline import (
    cleanup_duckdb,
    configure_logging,
    ensure_glue_database,
    glue_columns_from_parquet,
    init_duckdb,
    parse_base_env,
    register_hive_table,
    upsert_glue_table,
)

log = configure_logging(__name__)

RAW_TABLE_COLUMNS = {
    "raw_dispatch_base": [
        "hvfhs_license_num",
        "dispatching_base_num",
        "originating_base_num",
    ],
    "raw_trip_time_location": [
        "request_datetime",
        "on_scene_datetime",
        "pickup_datetime",
        "dropoff_datetime",
        "PULocationID",
        "DOLocationID",
        "trip_miles",
        "trip_time",
    ],
    "raw_fare_payment": [
        "base_passenger_fare",
        "tolls",
        "bcf",
        "sales_tax",
        "congestion_surcharge",
        "airport_fee",
        "tips",
        "driver_pay",
        "cbd_congestion_fee",
    ],
    "raw_request_flags": [
        "shared_request_flag",
        "shared_match_flag",
        "access_a_ride_flag",
        "wav_request_flag",
        "wav_match_flag",
    ],
}

DIMENSION_TABLES = ["dim_hvfhs_license", "dim_base"]

DB_PATH = Path("/tmp/_build_raw.duckdb")


def _find_reference_dir() -> Path:
    script_dir = Path(__file__).resolve().parent
    for ancestor in (script_dir.parent, script_dir.parent.parent):
        candidate = ancestor / "reference"
        if candidate.is_dir():
            return candidate
    return script_dir.parent / "reference"


def staging_s3_paths(
    bucket: str, prefix: str, months: list[str],
) -> list[str]:
    return [
        f"s3://{bucket}/{prefix}/fhvhv_tripdata_{ym}.parquet"
        for ym in months
    ]


def staging_paths_sql(paths: list[str]) -> str:
    inner = ", ".join(f"'{p}'" for p in paths)
    return f"[{inner}]"


def verify_staging_files(
    s3_client, bucket: str, paths: list[str],
) -> list[dict]:
    files: list[dict] = []
    missing: list[str] = []

    for uri in paths:
        key = uri.split(f"s3://{bucket}/", 1)[1]
        try:
            resp = s3_client.head_object(Bucket=bucket, Key=key)
            files.append({"Key": key, "Size": resp["ContentLength"]})
        except s3_client.exceptions.ClientError:
            missing.append(uri)

    if missing:
        log.error("Staging files not found in S3:")
        for m in missing:
            log.error("  %s", m)
        sys.exit(1)

    return files


def analyze_data_quality(
    con: duckdb.DuckDBPyConnection, paths_lit: str,
) -> int:
    log.info("=" * 70)
    log.info("DATA QUALITY ANALYSIS")
    log.info("=" * 70)

    schema = con.execute(
        f"DESCRIBE SELECT * FROM read_parquet({paths_lit})"
    ).fetchall()

    file_counts = con.execute(f"""
        SELECT filename, count(*) AS n
        FROM read_parquet({paths_lit}, filename=true)
        GROUP BY filename ORDER BY filename
    """).fetchall()

    total_rows = sum(c for _, c in file_counts)
    log.info("Rows per staging file (total: %s):", f"{total_rows:,}")
    for fname, n in file_counts:
        log.info("  %40s  %14s", Path(fname).name, f"{n:,}")

    null_exprs = ", ".join(
        f'SUM(CASE WHEN "{col[0]}" IS NULL THEN 1 ELSE 0 END)'
        for col in schema
    )
    null_result = con.execute(
        f"SELECT {null_exprs} FROM read_parquet({paths_lit})"
    ).fetchone()

    log.info("Null analysis:")
    found_nulls = False
    for i, col in enumerate(schema):
        null_count = null_result[i]
        if null_count > 0:
            found_nulls = True
            pct = null_count / total_rows * 100
            log.info("  %-28s %14s %7.2f%%", col[0], f"{null_count:,}", pct)
    if not found_nulls:
        log.info("  No null values found.")

    numeric_cols = [
        col[0]
        for col in schema
        if col[1] in ("DOUBLE", "BIGINT", "INTEGER", "FLOAT")
    ]
    if numeric_cols:
        stat_exprs = ", ".join(
            f'MIN("{c}"), AVG("{c}"), MAX("{c}"), STDDEV("{c}")'
            for c in numeric_cols
        )
        stats = con.execute(
            f"SELECT {stat_exprs} FROM read_parquet({paths_lit})"
        ).fetchone()

        log.info("Numeric column statistics:")
        for i, col_name in enumerate(numeric_cols):
            mn, avg, mx, sd = stats[i * 4 : (i + 1) * 4]
            log.info(
                "  %-28s min=%12.2f avg=%12.2f max=%14.2f std=%12.2f",
                col_name, mn, avg, mx, sd,
            )

    categorical_cols = [col[0] for col in schema if col[1] == "VARCHAR"]
    for col_name in categorical_cols:
        dist = con.execute(f"""
            SELECT "{col_name}", count(*) AS n
            FROM read_parquet({paths_lit})
            WHERE "{col_name}" IS NOT NULL
            GROUP BY "{col_name}"
            ORDER BY n DESC
            LIMIT 10
        """).fetchall()
        for val, n in dist:
            pct = n / total_rows * 100
            log.info(
                "  %s: %-20s %14s (%5.2f%%)",
                col_name, val, f"{n:,}", pct,
            )

    return total_rows


def build_raw_tables(
    con: duckdb.DuckDBPyConnection,
    paths_lit: str,
    total_rows: int,
    bucket: str,
    raw_prefix: str,
) -> dict[str, str]:
    log.info("=" * 70)
    log.info("BUILDING RAW LAYER")
    log.info("=" * 70)

    log.info("Materializing staging data with trip_id …")
    t0 = time.time()
    con.execute(f"""
        CREATE OR REPLACE TABLE staging_full AS
        SELECT
            (row_number() OVER ()) - 1 AS trip_id,
            current_timestamp AS processed_date,
            CAST(replace(
                regexp_extract(filename, 'fhvhv_tripdata_(\\d{{4}}-\\d{{2}})', 1),
                '-', ''
            ) AS INTEGER) AS year_month,
            * EXCLUDE (filename)
        FROM read_parquet({paths_lit}, filename=true)
    """)
    log.info("  Done in %.1fs  (%s rows)", time.time() - t0, f"{total_rows:,}")

    table_paths: dict[str, str] = {}
    for table_name, columns in RAW_TABLE_COLUMNS.items():
        t0 = time.time()
        cols_csv = ", ".join(f'"{c}"' for c in columns)
        s3_dir = f"s3://{bucket}/{raw_prefix}/{table_name}"

        con.execute(f"""
            COPY (
                SELECT trip_id, processed_date, year_month, {cols_csv}
                FROM staging_full
            ) TO '{s3_dir}' (FORMAT PARQUET, PARTITION_BY (year_month), OVERWRITE_OR_IGNORE)
        """)
        elapsed = time.time() - t0
        log.info("  %-30s -> %s/  (%.1fs)", table_name, s3_dir, elapsed)
        table_paths[table_name] = s3_dir

    con.execute("DROP TABLE IF EXISTS staging_full")
    return table_paths


def build_dimension_tables(
    con: duckdb.DuckDBPyConnection,
    bucket: str,
    raw_prefix: str,
) -> dict[str, str]:
    log.info("Building dimension tables:")
    reference_dir = _find_reference_dir()
    table_paths: dict[str, str] = {}

    for dim_name in DIMENSION_TABLES:
        csv_path = reference_dir / f"{dim_name}.csv"
        if not csv_path.exists():
            log.warning("  [SKIP] %s — %s not found", dim_name, csv_path)
            continue

        s3_path = (
            f"s3://{bucket}/{raw_prefix}/{dim_name}/{dim_name}.parquet"
        )
        con.execute(f"""
            COPY (SELECT * FROM read_csv_auto('{csv_path}'))
            TO '{s3_path}' (FORMAT PARQUET)
        """)
        n = con.execute(
            f"SELECT count(*) FROM read_parquet('{s3_path}')"
        ).fetchone()[0]
        log.info("  %-30s %4d rows -> %s", dim_name, n, s3_path)
        table_paths[dim_name] = s3_path

    return table_paths


def validate_raw_layer(
    con: duckdb.DuckDBPyConnection,
    total_rows: int,
    table_paths: dict[str, str],
    year_months: list[int],
) -> bool:
    log.info("=" * 70)
    log.info("VALIDATION")
    log.info("=" * 70)

    all_ok = True
    ym_csv = ", ".join(str(ym) for ym in year_months)

    def _raw_glob(s3_dir: str) -> str:
        return f"{s3_dir}/**/*.parquet"

    for table_name in RAW_TABLE_COLUMNS:
        s3_dir = table_paths[table_name]
        n = con.execute(f"""
            SELECT count(*)
            FROM read_parquet('{_raw_glob(s3_dir)}', hive_partitioning=true)
            WHERE year_month IN ({ym_csv})
        """).fetchone()[0]
        ok = n == total_rows
        if not ok:
            all_ok = False
        tag = "OK" if ok else "FAIL"
        log.info("  [%s] %-30s %14s rows", tag, table_name, f"{n:,}")

    for dim_name in DIMENSION_TABLES:
        if dim_name in table_paths:
            s3_path = table_paths[dim_name]
            n = con.execute(
                f"SELECT count(*) FROM read_parquet('{s3_path}')"
            ).fetchone()[0]
            tag = "OK" if n > 0 else "FAIL"
            if tag == "FAIL":
                all_ok = False
            log.info("  [%s] %-30s %14s rows", tag, dim_name, f"{n:,}")

    sample = min(100_000, total_rows)
    rp = {name: _raw_glob(table_paths[name]) for name in RAW_TABLE_COLUMNS}
    joined = con.execute(f"""
        SELECT count(*)
        FROM read_parquet('{rp["raw_dispatch_base"]}', hive_partitioning=true) d
        JOIN read_parquet('{rp["raw_trip_time_location"]}', hive_partitioning=true) t
             ON d.trip_id = t.trip_id AND d.year_month = t.year_month
        JOIN read_parquet('{rp["raw_fare_payment"]}', hive_partitioning=true) f
             ON d.trip_id = f.trip_id AND d.year_month = f.year_month
        JOIN read_parquet('{rp["raw_request_flags"]}', hive_partitioning=true) r
             ON d.trip_id = r.trip_id AND d.year_month = r.year_month
        WHERE d.year_month IN ({ym_csv})
          AND d.trip_id < {sample}
    """).fetchone()[0]
    ok = joined == sample
    if not ok:
        all_ok = False
    tag = "OK" if ok else "FAIL"
    log.info(
        "  [%s] Join consistency (first %s): %s / %s",
        tag, f"{sample:,}", f"{joined:,}", f"{sample:,}",
    )

    return all_ok


def register_glue_tables(
    con: duckdb.DuckDBPyConnection,
    glue_client,
    database: str,
    table_paths: dict[str, str],
    bucket: str,
    raw_prefix: str,
    year_months: list[int],
) -> None:
    log.info("=" * 70)
    log.info("GLUE DATA CATALOG REGISTRATION")
    log.info("=" * 70)

    ensure_glue_database(glue_client, database)

    for table_name, s3_path in table_paths.items():
        s3_location = f"s3://{bucket}/{raw_prefix}/{table_name}/"

        if table_name in RAW_TABLE_COLUMNS:
            register_hive_table(
                con, glue_client, database, table_name,
                f"Raw layer: {table_name}",
                s3_path, s3_location, year_months,
            )
        else:
            columns = glue_columns_from_parquet(con, s3_path)
            action = upsert_glue_table(
                glue_client, database, table_name, s3_location,
                columns, f"Dimension: {table_name}",
            )
            log.info(
                "  [%s] %s.%-25s (%d cols) -> %s",
                action, database, table_name, len(columns), s3_location,
            )


def main() -> None:
    t_start = time.time()
    env = parse_base_env()

    staging_prefix = os.environ.get("S3_STAGING_PREFIX", "staging").strip("/")
    raw_prefix = os.environ.get("S3_RAW_PREFIX", "raw").strip("/")
    skip_qa = os.environ.get("SKIP_QUALITY_ANALYSIS", "").lower() == "true"

    s3_client = boto3.client("s3", region_name=env.region)
    s3_paths = staging_s3_paths(env.bucket, staging_prefix, env.months)
    paths_lit = staging_paths_sql(s3_paths)

    log.info("=" * 70)
    log.info("RAW LAYER BUILD")
    log.info("  Months        : %s → %s (%d file(s))", env.start_month, env.end_month, len(env.months))
    log.info("  Partitions    : %s", ", ".join(str(ym) for ym in env.year_months))
    log.info("  Staging       : s3://%s/%s/", env.bucket, staging_prefix)
    log.info("  Output        : s3://%s/%s/", env.bucket, raw_prefix)
    log.info("  Glue database : %s", env.glue_database)
    log.info("=" * 70)

    files = verify_staging_files(s3_client, env.bucket, s3_paths)
    log.info("%d staging file(s) verified:", len(files))
    for f in files:
        size_mb = f["Size"] / (1024 * 1024)
        log.info("  %s  (%.0f MB)", f["Key"], size_mb)

    con = init_duckdb(DB_PATH, env.region)

    try:
        if skip_qa:
            log.info("Skipping quality analysis (SKIP_QUALITY_ANALYSIS=true)")
            total_rows = con.execute(
                f"SELECT count(*) FROM read_parquet({paths_lit})"
            ).fetchone()[0]
            log.info("Total staging rows: %s", f"{total_rows:,}")
        else:
            total_rows = analyze_data_quality(con, paths_lit)

        raw_paths = build_raw_tables(
            con, paths_lit, total_rows, env.bucket, raw_prefix,
        )
        dim_paths = build_dimension_tables(con, env.bucket, raw_prefix)

        all_paths = {**raw_paths, **dim_paths}
        ok = validate_raw_layer(con, total_rows, all_paths, env.year_months)

        register_glue_tables(
            con, env.glue_client, env.glue_database, all_paths,
            env.bucket, raw_prefix, env.year_months,
        )
    finally:
        con.close()
        cleanup_duckdb(DB_PATH)

    elapsed = time.time() - t_start
    log.info("=" * 70)
    status = "BUILD COMPLETE" if ok else "BUILD COMPLETED WITH WARNINGS"
    log.info(status)
    log.info("  Total time : %.1fs", elapsed)
    log.info("  Months     : %s → %s", env.start_month, env.end_month)
    log.info("  Total rows : %s", f"{total_rows:,}")
    log.info("  Raw tables : %d", len(RAW_TABLE_COLUMNS))
    log.info("  Dimensions : %d", len(dim_paths))
    log.info("  Output     : s3://%s/%s/", env.bucket, raw_prefix)
    log.info("  Glue DB    : %s", env.glue_database)
    log.info("=" * 70)

    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
