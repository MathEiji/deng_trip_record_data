"""Shared infrastructure for all pipeline layers (raw, trusted, specialized).

Provides DuckDB lifecycle, Glue Data Catalog helpers, month range
utilities, environment parsing, and common constants.
"""

import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import boto3
import duckdb

log = logging.getLogger(__name__)


def configure_logging(name: str = __name__) -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger(name)


@dataclass
class BaseEnv:
    """Common environment variables shared by every pipeline layer."""
    bucket: str
    start_month: str
    end_month: str
    region: str
    glue_database: str
    months: list[str]
    year_months: list[int]
    year_months_csv: str
    glue_client: object = field(repr=False)


def parse_base_env() -> BaseEnv:
    """Parse and validate the env vars every layer needs."""
    bucket = os.environ.get("S3_BUCKET")
    start = os.environ.get("START_MONTH")
    end = os.environ.get("END_MONTH")
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

    return BaseEnv(
        bucket=bucket,
        start_month=start,
        end_month=end,
        region=region,
        glue_database=glue_database,
        months=months,
        year_months=year_months,
        year_months_csv=year_months_csv,
        glue_client=glue_client,
    )

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

PARQUET_SERDE = {
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


def init_duckdb(
    db_path: Path,
    region: str,
    *,
    memory_limit: str | None = None,
) -> duckdb.DuckDBPyConnection:
    """On-disk DB so DuckDB can spill to ephemeral storage when needed."""
    con = duckdb.connect(str(db_path))
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("INSTALL aws; LOAD aws;")
    con.execute(f"SET s3_region = '{region}';")
    if memory_limit:
        con.execute(f"SET memory_limit = '{memory_limit}';")
    con.execute("CREATE SECRET (TYPE S3, PROVIDER CREDENTIAL_CHAIN);")
    return con


def cleanup_duckdb(db_path: Path) -> None:
    for p in (db_path, db_path.with_suffix(".duckdb.wal")):
        if p.exists():
            p.unlink()


def glue_columns_from_parquet(
    con: duckdb.DuckDBPyConnection,
    s3_path: str,
    *,
    hive_partitioning: bool = False,
    exclude: set[str] | None = None,
) -> list[dict]:
    hp = ", hive_partitioning=true" if hive_partitioning else ""
    schema = con.execute(
        f"DESCRIBE SELECT * FROM read_parquet('{s3_path}'{hp})"
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


def upsert_glue_table(
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
        "Description": description or table_name,
        "StorageDescriptor": {
            "Columns": columns,
            "Location": s3_location,
            **PARQUET_SERDE,
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


def register_glue_partitions(
    glue_client,
    database: str,
    table_name: str,
    s3_location: str,
    columns: list[dict],
    year_months: list[int],
) -> int:
    """Idempotent: creates new partitions, updates existing ones."""
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
                **PARQUET_SERDE,
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


def ensure_glue_database(glue_client, database: str) -> None:
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


def register_hive_table(
    con: duckdb.DuckDBPyConnection,
    glue_client,
    database: str,
    table_name: str,
    description: str,
    s3_dir: str,
    s3_location: str,
    year_months: list[int],
) -> None:
    """Register a single Hive-partitioned table + its partitions in Glue."""
    s3_glob = f"{s3_dir}/**/*.parquet"
    columns = glue_columns_from_parquet(
        con, s3_glob, hive_partitioning=True, exclude={"year_month"},
    )
    partition_keys = [{"Name": "year_month", "Type": "int"}]

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
