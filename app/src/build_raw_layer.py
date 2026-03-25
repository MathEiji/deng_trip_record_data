"""Build the RAW layer from staging FHVHV trip data stored in S3.

Reads staging parquet files from S3 via DuckDB httpfs, runs data quality
analysis, splits the monolithic schema into context-based raw tables,
writes them back to S3, and registers every table in the AWS Glue Data
Catalog so they're immediately queryable from Athena / Spark / Redshift
Spectrum.

Only the months specified by START_MONTH / END_MONTH are processed.

Designed to run inside an ECS Fargate task.  Configuration is read from
environment variables:

    S3_BUCKET              – S3 bucket for input and output (required)
    START_MONTH            – first month to process, YYYY-MM (required)
    END_MONTH              – last  month to process, YYYY-MM (required)
    S3_STAGING_PREFIX      – key prefix for staging parquets (default: "staging")
    S3_RAW_PREFIX          – key prefix for raw output       (default: "raw")
    GLUE_DATABASE          – Glue catalog database name      (default: "trip_record_data")
    AWS_REGION             – AWS region                      (default: "us-east-1")
    SKIP_QUALITY_ANALYSIS  – set "true" to skip the profiling step
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

# ── Table definitions ────────────────────────────────────────────────────

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

DB_PATH = Path("/tmp/_build_raw.duckdb")


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


def _find_reference_dir() -> Path:
    """Locate the reference/ directory (works both in Docker and local dev)."""
    script_dir = Path(__file__).resolve().parent
    for ancestor in (script_dir.parent, script_dir.parent.parent):
        candidate = ancestor / "reference"
        if candidate.is_dir():
            return candidate
    return script_dir.parent / "reference"


def init_duckdb(region: str) -> duckdb.DuckDBPyConnection:
    """Create a DuckDB connection with S3 via IAM credential chain.

    Uses a persistent on-disk database so DuckDB can spill large
    intermediates to the container's ephemeral storage.
    """
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


def staging_s3_paths(
    bucket: str, prefix: str, months: list[str],
) -> list[str]:
    """Build the explicit list of S3 parquet URIs for the given months."""
    return [
        f"s3://{bucket}/{prefix}/fhvhv_tripdata_{ym}.parquet"
        for ym in months
    ]


def staging_paths_sql(paths: list[str]) -> str:
    """Format a list of S3 URIs into a DuckDB list literal for read_parquet()."""
    inner = ", ".join(f"'{p}'" for p in paths)
    return f"[{inner}]"


# ── Discovery ────────────────────────────────────────────────────────────

def verify_staging_files(
    s3_client, bucket: str, paths: list[str],
) -> list[dict]:
    """HEAD each expected staging file. Returns [{"Key": ..., "Size": ...}].

    Exits with error if any file is missing.
    """
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


# ── Data quality ─────────────────────────────────────────────────────────

def analyze_data_quality(
    con: duckdb.DuckDBPyConnection, paths_lit: str,
) -> int:
    """Profile the staging data and return total row count.

    *paths_lit* is a DuckDB list literal, e.g. ``['s3://…/a.parquet', …]``.
    """
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

    # Null analysis
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

    # Numeric statistics
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

    # Categorical distributions
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


# ── Raw table build ──────────────────────────────────────────────────────

def build_raw_tables(
    con: duckdb.DuckDBPyConnection,
    paths_lit: str,
    total_rows: int,
    bucket: str,
    raw_prefix: str,
) -> dict[str, str]:
    """Materialize staging with trip_id, split into raw tables on S3.

    *paths_lit* is a DuckDB list literal for ``read_parquet()``.
    Output is Hive-partitioned by ``year_month`` (yyyyMM derived from the
    staging filename suffix).
    Returns ``{table_name: s3_base_directory}``.
    """
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
                regexp_extract(filename, 'fhvhv_tripdata_(\d{{4}}-\d{{2}})', 1),
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


# ── Dimension tables ─────────────────────────────────────────────────────

def build_dimension_tables(
    con: duckdb.DuckDBPyConnection,
    bucket: str,
    raw_prefix: str,
) -> dict[str, str]:
    """Convert bundled reference CSVs into parquet dimension tables on S3.

    Returns ``{table_name: s3_parquet_path}``.
    """
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


# ── Validation ───────────────────────────────────────────────────────────

def validate_raw_layer(
    con: duckdb.DuckDBPyConnection,
    total_rows: int,
    table_paths: dict[str, str],
) -> bool:
    """Check row counts and join consistency across raw tables in S3."""
    log.info("=" * 70)
    log.info("VALIDATION")
    log.info("=" * 70)

    all_ok = True

    def _raw_glob(s3_dir: str) -> str:
        return f"{s3_dir}/**/*.parquet"

    for table_name in RAW_TABLE_COLUMNS:
        s3_dir = table_paths[table_name]
        n = con.execute(
            f"SELECT count(*) FROM read_parquet('{_raw_glob(s3_dir)}', hive_partitioning=true)"
        ).fetchone()[0]
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

    # Join consistency on a sample
    sample = min(100_000, total_rows)
    rp = {name: _raw_glob(table_paths[name]) for name in RAW_TABLE_COLUMNS}
    joined = con.execute(f"""
        SELECT count(*)
        FROM read_parquet('{rp["raw_dispatch_base"]}', hive_partitioning=true) d
        JOIN read_parquet('{rp["raw_trip_time_location"]}', hive_partitioning=true) t
             ON d.trip_id = t.trip_id
        JOIN read_parquet('{rp["raw_fare_payment"]}', hive_partitioning=true) f
             ON d.trip_id = f.trip_id
        JOIN read_parquet('{rp["raw_request_flags"]}', hive_partitioning=true) r
             ON d.trip_id = r.trip_id
        WHERE d.trip_id < {sample}
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


# ── Glue Data Catalog ────────────────────────────────────────────────────

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


def _glue_columns_from_parquet(
    con: duckdb.DuckDBPyConnection,
    s3_path: str,
    hive_partitioning: bool = False,
    exclude: set[str] | None = None,
) -> list[dict]:
    """Derive Glue-compatible column definitions from S3 parquet file(s)."""
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
    partition_keys: list[dict] | None = None,
) -> str:
    """Create or update a single Glue table. Returns 'created' or 'updated'."""
    table_input = {
        "Name": table_name,
        "Description": f"Raw layer table: {table_name}",
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
    """Register Hive-style partitions in Glue. Returns count of new partitions."""
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

    created, updated = 0, 0
    for i in range(0, len(partition_inputs), 100):
        batch = partition_inputs[i : i + 100]
        resp = glue_client.batch_create_partition(
            DatabaseName=database,
            TableName=table_name,
            PartitionInputList=batch,
        )
        errors = resp.get("Errors", [])
        created += len(batch) - len(errors)

        to_update = []
        for err in errors:
            code = err["ErrorDetail"]["ErrorCode"]
            if code == "AlreadyExistsException":
                vals = err["PartitionValues"]
                matching = [p for p in batch if p["Values"] == vals]
                if matching:
                    to_update.append(matching[0])
            else:
                log.warning(
                    "  Partition %s error: %s",
                    err["PartitionValues"], err["ErrorDetail"]["ErrorMessage"],
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
            updated += len(to_update)

    return created + updated


def register_glue_tables(
    con: duckdb.DuckDBPyConnection,
    glue_client,
    database: str,
    table_paths: dict[str, str],
    bucket: str,
    raw_prefix: str,
    year_months: list[int],
) -> None:
    """Register all raw and dimension tables in the Glue Data Catalog."""
    log.info("=" * 70)
    log.info("GLUE DATA CATALOG REGISTRATION")
    log.info("=" * 70)

    try:
        glue_client.create_database(
            DatabaseInput={
                "Name": database,
                "Description": "NYC TLC FHVHV trip record data — raw layer",
            }
        )
        log.info("Created Glue database: %s", database)
    except glue_client.exceptions.AlreadyExistsException:
        log.info("Glue database exists: %s", database)

    for table_name, s3_path in table_paths.items():
        is_raw = table_name in RAW_TABLE_COLUMNS
        s3_location = f"s3://{bucket}/{raw_prefix}/{table_name}/"

        if is_raw:
            glob_path = f"{s3_path}/**/*.parquet"
            columns = _glue_columns_from_parquet(
                con, glob_path,
                hive_partitioning=True,
                exclude={"year_month"},
            )
            partition_keys = [{"Name": "year_month", "Type": "int"}]
        else:
            columns = _glue_columns_from_parquet(con, s3_path)
            partition_keys = None

        action = _upsert_glue_table(
            glue_client, database, table_name, s3_location,
            columns, partition_keys,
        )
        log.info(
            "  [%s] %s.%-25s (%d cols) -> %s",
            action, database, table_name, len(columns), s3_location,
        )

        if is_raw and year_months:
            n_parts = _register_glue_partitions(
                glue_client, database, table_name,
                s3_location, columns, year_months,
            )
            log.info(
                "         %d new partition(s) registered", n_parts,
            )


# ── Main ─────────────────────────────────────────────────────────────────

def main() -> None:
    t_start = time.time()

    bucket = os.environ.get("S3_BUCKET")
    start = os.environ.get("START_MONTH")
    end = os.environ.get("END_MONTH")
    staging_prefix = os.environ.get("S3_STAGING_PREFIX", "staging").strip("/")
    raw_prefix = os.environ.get("S3_RAW_PREFIX", "raw").strip("/")
    glue_database = os.environ.get("GLUE_DATABASE", "trip_record_data")
    region = os.environ.get("AWS_REGION", "us-east-1")
    skip_qa = os.environ.get("SKIP_QUALITY_ANALYSIS", "").lower() == "true"

    if not bucket:
        log.error("S3_BUCKET is required (set via environment variable)")
        sys.exit(1)
    if not start or not end:
        log.error("START_MONTH and END_MONTH are required (set via environment variables)")
        sys.exit(1)

    months = month_range(start, end)
    year_months = [int(ym.replace("-", "")) for ym in months]
    s3_client = boto3.client("s3", region_name=region)
    glue_client = boto3.client("glue", region_name=region)

    s3_paths = staging_s3_paths(bucket, staging_prefix, months)
    paths_lit = staging_paths_sql(s3_paths)

    log.info("=" * 70)
    log.info("RAW LAYER BUILD")
    log.info("  Months        : %s → %s (%d file(s))", start, end, len(months))
    log.info("  Partitions    : %s", ", ".join(str(ym) for ym in year_months))
    log.info("  Staging       : s3://%s/%s/", bucket, staging_prefix)
    log.info("  Output        : s3://%s/%s/", bucket, raw_prefix)
    log.info("  Glue database : %s", glue_database)
    log.info("=" * 70)

    files = verify_staging_files(s3_client, bucket, s3_paths)
    log.info("%d staging file(s) verified:", len(files))
    for f in files:
        size_mb = f["Size"] / (1024 * 1024)
        log.info("  %s  (%.0f MB)", f["Key"], size_mb)

    con = init_duckdb(region)

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
            con, paths_lit, total_rows, bucket, raw_prefix,
        )
        dim_paths = build_dimension_tables(con, bucket, raw_prefix)

        all_paths = {**raw_paths, **dim_paths}
        ok = validate_raw_layer(con, total_rows, all_paths)

        register_glue_tables(
            con, glue_client, glue_database, all_paths,
            bucket, raw_prefix, year_months,
        )
    finally:
        con.close()
        cleanup_duckdb()

    elapsed = time.time() - t_start
    log.info("=" * 70)
    status = "BUILD COMPLETE" if ok else "BUILD COMPLETED WITH WARNINGS"
    log.info(status)
    log.info("  Total time : %.1fs", elapsed)
    log.info("  Months     : %s → %s", start, end)
    log.info("  Total rows : %s", f"{total_rows:,}")
    log.info("  Raw tables : %d", len(RAW_TABLE_COLUMNS))
    log.info("  Dimensions : %d", len(dim_paths))
    log.info("  Output     : s3://%s/%s/", bucket, raw_prefix)
    log.info("  Glue DB    : %s", glue_database)
    log.info("=" * 70)

    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
