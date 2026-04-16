"""Shared pipeline infrastructure — common to raw, trusted, and specialized layers."""

from common.pipeline import (
    DUCKDB_TO_GLUE_TYPE,
    PARQUET_SERDE,
    cleanup_duckdb,
    ensure_glue_database,
    glue_columns_from_parquet,
    init_duckdb,
    month_range,
    register_glue_partitions,
    upsert_glue_table,
)
from common.specialized import run as run_specialized

__all__ = [
    "DUCKDB_TO_GLUE_TYPE",
    "PARQUET_SERDE",
    "cleanup_duckdb",
    "ensure_glue_database",
    "glue_columns_from_parquet",
    "init_duckdb",
    "month_range",
    "register_glue_partitions",
    "run_specialized",
    "upsert_glue_table",
]
