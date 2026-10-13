"""Load: write cleaned DataFrames into PostgreSQL staging tables, and run SQL files.

Loading strategy: documented FULL REFRESH. For each staging table we TRUNCATE and then
bulk-load with COPY. The caller wraps everything in ONE transaction, so a rerun replaces
the data (no duplicates) and a failure rolls back to the previous good state.
"""
from __future__ import annotations

import io
import logging
from pathlib import Path

import pandas as pd
import psycopg2
from psycopg2 import sql

from .config import COPY_NULL, TableSpec

log = logging.getLogger(__name__)


def connect(params: dict, autocommit: bool = False):
    conn = psycopg2.connect(**params)
    conn.autocommit = autocommit
    return conn


def run_sql_file(cur, path: Path) -> None:
    """Execute a whole .sql file (several statements) on an open cursor."""
    log.info("Running %s", Path(path).name)
    cur.execute(Path(path).read_text(encoding="utf-8"))


def truncate_staging(cur, specs) -> None:
    tables = sql.SQL(", ").join(sql.Identifier("staging", s.name) for s in specs)
    cur.execute(sql.SQL("TRUNCATE {}").format(tables))


def copy_dataframe(cur, spec: TableSpec, df: pd.DataFrame) -> int:
    """Bulk-load one DataFrame into its staging table with COPY. Returns rows loaded."""
    buf = io.StringIO()
    df.to_csv(buf, index=False, header=False, na_rep=COPY_NULL)
    buf.seek(0)
    stmt = sql.SQL(
        "COPY {table} ({cols}) FROM STDIN WITH (FORMAT csv, NULL {null})"
    ).format(
        table=sql.Identifier("staging", spec.name),
        cols=sql.SQL(", ").join(sql.Identifier(c) for c in spec.column_names),
        null=sql.Literal(COPY_NULL),
    )
    cur.copy_expert(stmt, buf)
    cur.execute(sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier("staging", spec.name)))
    loaded = int(cur.fetchone()[0])
    log.info("Loaded    %-30s %9s rows into staging.%s", spec.name, f"{loaded:,}", spec.name)
    return loaded
