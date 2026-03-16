"""Build the RAW layer from staging FHVHV trip data.

Reads all staging parquet files, runs data quality analysis, splits the
monolithic schema into context-based raw tables, and writes them to data/raw/.

Raw tables (each includes trip_id + processed_date):
  - raw_dispatch_base         Dispatch and base info
  - raw_trip_time_location    Trip timing, locations, distance
  - raw_fare_payment          Fare components and driver pay
  - raw_request_flags         Shared/WAV/accessibility flags

Dimension tables (from reference/ CSVs):
  - dim_hvfhs_license         HVFHS license → company mapping
  - dim_base                  TLC base number → company mapping

Usage:
    python app/src/build_raw_layer.py
"""

import sys
import time
from datetime import datetime
from pathlib import Path

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
STAGING_DIR = PROJECT_ROOT / "data" / "staging"
OUTPUT_DIR = PROJECT_ROOT / "data" / "raw"
REFERENCE_DIR = PROJECT_ROOT / "reference"

DIMENSION_TABLES = [
    "dim_hvfhs_license",
    "dim_base",
]

RAW_TABLE_CONTEXTS = {
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


def _staging_glob() -> str:
    return str(STAGING_DIR / "fhvhv_tripdata_*.parquet")


def discover_staging_files() -> list[Path]:
    files = sorted(STAGING_DIR.glob("fhvhv_tripdata_*.parquet"))
    if not files:
        print(f"ERROR: No staging files in {STAGING_DIR}", file=sys.stderr)
        sys.exit(1)
    return files


def analyze_data_quality(con: duckdb.DuckDBPyConnection, glob: str) -> int:
    """Profile the staging data and return total row count."""
    print("\n" + "=" * 70)
    print("DATA QUALITY ANALYSIS")
    print("=" * 70)

    schema = con.execute(
        f"DESCRIBE SELECT * FROM read_parquet('{glob}')"
    ).fetchall()

    # ── Rows per file ────────────────────────────────────────────────────
    file_counts = con.execute(f"""
        SELECT filename, count(*) AS n
        FROM read_parquet('{glob}', filename=true)
        GROUP BY filename ORDER BY filename
    """).fetchall()

    total_rows = sum(c for _, c in file_counts)
    print(f"\nRows per staging file (total: {total_rows:,}):")
    for fname, n in file_counts:
        print(f"  {Path(fname).name:>40s}  {n:>14,}")

    # ── Null analysis ────────────────────────────────────────────────────
    null_exprs = ", ".join(
        f'SUM(CASE WHEN "{col[0]}" IS NULL THEN 1 ELSE 0 END)'
        for col in schema
    )
    null_result = con.execute(
        f"SELECT {null_exprs} FROM read_parquet('{glob}')"
    ).fetchone()

    print(f"\nNull analysis:")
    print(f"  {'Column':<28s} {'Nulls':>14s} {'%':>8s}")
    print(f"  {'-' * 28} {'-' * 14} {'-' * 8}")
    found_nulls = False
    for i, col in enumerate(schema):
        null_count = null_result[i]
        if null_count > 0:
            found_nulls = True
            pct = null_count / total_rows * 100
            print(f"  {col[0]:<28s} {null_count:>14,} {pct:>7.2f}%")
    if not found_nulls:
        print("  No null values found across any column.")

    # ── Numeric statistics ───────────────────────────────────────────────
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
            f"SELECT {stat_exprs} FROM read_parquet('{glob}')"
        ).fetchone()

        print(f"\nNumeric column statistics:")
        print(
            f"  {'Column':<28s} {'Min':>12s} {'Avg':>12s}"
            f" {'Max':>14s} {'StdDev':>12s}"
        )
        print(f"  {'-' * 28} {'-' * 12} {'-' * 12} {'-' * 14} {'-' * 12}")
        for i, col_name in enumerate(numeric_cols):
            mn, avg, mx, sd = stats[i * 4 : (i + 1) * 4]
            print(
                f"  {col_name:<28s} {mn:>12.2f} {avg:>12.2f}"
                f" {mx:>14.2f} {sd:>12.2f}"
            )

    # ── Categorical distributions ────────────────────────────────────────
    categorical_cols = [col[0] for col in schema if col[1] == "VARCHAR"]
    for col_name in categorical_cols:
        dist = con.execute(f"""
            SELECT "{col_name}", count(*) AS n
            FROM read_parquet('{glob}')
            WHERE "{col_name}" IS NOT NULL
            GROUP BY "{col_name}"
            ORDER BY n DESC
            LIMIT 10
        """).fetchall()
        print(f"\n  {col_name} — top values:")
        for val, n in dist:
            pct = n / total_rows * 100
            print(f"    {val:<20s} {n:>14,} ({pct:5.2f}%)")

    return total_rows


def build_raw_tables(
    glob: str, total_rows: int, processed_date: str
) -> None:
    """Materialise staging data with trip_id, then write each raw table."""
    print("\n" + "=" * 70)
    print("BUILDING RAW LAYER")
    print("=" * 70)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Persistent DB so DuckDB can spill to disk if memory is tight.
    db_path = OUTPUT_DIR / "_build_temp.duckdb"
    build_con = duckdb.connect(str(db_path))

    try:
        print("\nMaterializing staging data with trip_id …")
        t0 = time.time()
        build_con.execute(f"""
            CREATE TABLE staging_full AS
            SELECT
                (row_number() OVER ()) - 1 AS trip_id,
                '{processed_date}'         AS processed_date,
                *
            FROM read_parquet('{glob}')
        """)
        print(f"  Done in {time.time() - t0:.1f}s  ({total_rows:,} rows)")

        for table_name, columns in RAW_TABLE_CONTEXTS.items():
            t0 = time.time()
            cols_csv = ", ".join(f'"{c}"' for c in columns)
            out_path = OUTPUT_DIR / f"{table_name}.parquet"

            build_con.execute(f"""
                COPY (
                    SELECT trip_id, processed_date, {cols_csv}
                    FROM staging_full
                ) TO '{out_path}' (FORMAT PARQUET)
            """)
            elapsed = time.time() - t0
            size_mb = out_path.stat().st_size / (1024 * 1024)
            print(f"  {table_name:<30s}  {size_mb:>8.1f} MB  ({elapsed:.1f}s)")

        build_con.execute("DROP TABLE IF EXISTS staging_full")
    finally:
        build_con.close()
        for p in (db_path, db_path.with_suffix(".duckdb.wal")):
            if p.exists():
                p.unlink()


def build_dimension_tables(con: duckdb.DuckDBPyConnection) -> None:
    """Convert reference CSVs into parquet dimension tables in the raw layer."""
    print("\n  Dimension tables (from reference/ CSVs):")
    for dim_name in DIMENSION_TABLES:
        csv_path = REFERENCE_DIR / f"{dim_name}.csv"
        if not csv_path.exists():
            print(f"    [SKIP] {dim_name} — {csv_path} not found")
            continue

        out_path = OUTPUT_DIR / f"{dim_name}.parquet"
        con.execute(f"""
            COPY (SELECT * FROM read_csv_auto('{csv_path}'))
            TO '{out_path}' (FORMAT PARQUET)
        """)
        n = con.execute(
            f"SELECT count(*) FROM read_parquet('{out_path}')"
        ).fetchone()[0]
        size_kb = out_path.stat().st_size / 1024
        print(f"    {dim_name:<30s}  {n:>4} rows  ({size_kb:.1f} KB)")


def validate_raw_layer(
    con: duckdb.DuckDBPyConnection, total_rows: int
) -> bool:
    """Check row counts and join consistency across raw tables."""
    print("\n" + "=" * 70)
    print("VALIDATION")
    print("=" * 70)

    all_ok = True
    for table_name in RAW_TABLE_CONTEXTS:
        path = OUTPUT_DIR / f"{table_name}.parquet"
        n = con.execute(
            f"SELECT count(*) FROM read_parquet('{path}')"
        ).fetchone()[0]
        ok = n == total_rows
        if not ok:
            all_ok = False
        tag = "OK" if ok else "FAIL"
        print(f"  [{tag}] {table_name:<30s} {n:>14,} rows")

    # Dimension tables
    for dim_name in DIMENSION_TABLES:
        path = OUTPUT_DIR / f"{dim_name}.parquet"
        if path.exists():
            n = con.execute(
                f"SELECT count(*) FROM read_parquet('{path}')"
            ).fetchone()[0]
            tag = "OK" if n > 0 else "FAIL"
            if tag == "FAIL":
                all_ok = False
            print(f"  [{tag}] {dim_name:<30s} {n:>14,} rows")

    # Join consistency on a sample
    paths = {
        name: str(OUTPUT_DIR / f"{name}.parquet")
        for name in RAW_TABLE_CONTEXTS
    }
    sample = min(100_000, total_rows)
    joined = con.execute(f"""
        SELECT count(*)
        FROM read_parquet('{paths['raw_dispatch_base']}') d
        JOIN read_parquet('{paths['raw_trip_time_location']}') t
             ON d.trip_id = t.trip_id
        JOIN read_parquet('{paths['raw_fare_payment']}') f
             ON d.trip_id = f.trip_id
        JOIN read_parquet('{paths['raw_request_flags']}') r
             ON d.trip_id = r.trip_id
        WHERE d.trip_id < {sample}
    """).fetchone()[0]
    ok = joined == sample
    if not ok:
        all_ok = False
    tag = "OK" if ok else "FAIL"
    print(f"\n  [{tag}] Join consistency (first {sample:,}): "
          f"{joined:,} / {sample:,}")

    return all_ok


def main() -> None:
    t_start = time.time()
    processed_date = datetime.now().strftime("%Y%m%d")
    glob = _staging_glob()

    print("=" * 70)
    print("RAW LAYER BUILD")
    print(f"  Staging dir   : {STAGING_DIR}")
    print(f"  Output dir    : {OUTPUT_DIR}")
    print(f"  Processed date: {processed_date}")
    print("=" * 70)

    files = discover_staging_files()
    print(f"\n{len(files)} staging file(s) found:")
    for f in files:
        size_mb = f.stat().st_size / (1024 * 1024)
        print(f"  {f.name}  ({size_mb:.0f} MB)")

    con = duckdb.connect()

    total_rows = analyze_data_quality(con, glob)
    build_raw_tables(glob, total_rows, processed_date)
    build_dimension_tables(con)
    ok = validate_raw_layer(con, total_rows)

    con.close()

    elapsed = time.time() - t_start
    print("\n" + "=" * 70)
    status = "BUILD COMPLETE" if ok else "BUILD COMPLETED WITH WARNINGS"
    print(status)
    print(f"  Total time : {elapsed:.1f}s")
    print(f"  Total rows : {total_rows:,}")
    print(f"  Raw tables : {len(RAW_TABLE_CONTEXTS)}")
    print(f"  Output dir : {OUTPUT_DIR}")
    print("=" * 70)

    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
