"""Extract: read the raw CSV files without modifying them.

Everything here happens *before* the database is touched, so a missing or malformed
input file can never leave the database half-updated.
"""
from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from .config import TableSpec

log = logging.getLogger(__name__)

# utf-8-sig transparently strips a byte-order mark. One of the source files
# (product_category_name_translation.csv) has one; without this the first column would
# be called "\ufeffproduct_category_name" and header validation would fail.
CSV_ENCODING = "utf-8-sig"


class ExtractError(RuntimeError):
    """Raised when an input file is missing, empty or structurally invalid."""


@dataclass
class CsvScan:
    header: list
    record_count: int  # data records, excluding header and blank lines


def scan_csv(path: Path) -> CsvScan:
    """Independent first pass with the stdlib ``csv`` module.

    Gives a source row count that does not depend on pandas and detects rows with the
    wrong number of fields (a classic sign of a broken export).
    """
    try:
        with open(path, encoding=CSV_ENCODING, newline="") as fh:
            reader = csv.reader(fh)
            try:
                header = [h.strip() for h in next(reader)]
            except StopIteration:
                raise ExtractError(f"{path.name}: file is empty (no header row)")
            count = 0
            ragged_total = 0
            ragged_examples = []  # (line number, field count)
            for row in reader:
                if not row:  # blank line
                    continue
                count += 1
                if len(row) != len(header):
                    ragged_total += 1
                    if len(ragged_examples) < 3:
                        ragged_examples.append((reader.line_num, len(row)))
            if ragged_total:
                raise ExtractError(
                    f"{path.name}: {ragged_total} row(s) do not have {len(header)} fields "
                    f"(first examples as (line, fields): {ragged_examples})"
                )
    except UnicodeDecodeError as exc:
        raise ExtractError(f"{path.name}: not valid UTF-8 ({exc})") from exc
    return CsvScan(header=header, record_count=count)


def read_table(data_dir: Path, spec: TableSpec) -> tuple[pd.DataFrame, int]:
    """Read one CSV as raw strings. Returns (dataframe, source_record_count)."""
    path = Path(data_dir) / spec.filename
    if not path.is_file():
        raise ExtractError(f"Input file not found: {path}")

    scan = scan_csv(path)

    missing = [c for c in spec.column_names if c not in scan.header]
    if missing:
        raise ExtractError(
            f"{path.name}: missing required column(s) {missing}. Found columns: {scan.header}"
        )
    extra = [c for c in scan.header if c not in spec.columns]
    if extra:
        log.warning("%s: ignoring unexpected extra column(s): %s", path.name, extra)
    if scan.record_count == 0:
        # An empty input would silently wipe the tables on a full refresh.
        raise ExtractError(f"{path.name}: file has a header but no data rows")

    # dtype=str + keep_default_na=False: no silent type guessing and no silent NaN
    # conversion. Typing and null handling are done explicitly in transform.py.
    df = pd.read_csv(
        path,
        dtype=str,
        keep_default_na=False,
        encoding=CSV_ENCODING,
        usecols=spec.column_names,
    )[spec.column_names]

    if len(df) != scan.record_count:
        raise ExtractError(
            f"{path.name}: pandas read {len(df)} rows but csv module counted "
            f"{scan.record_count}; the file may contain unbalanced quotes"
        )
    log.info("Extracted %-30s %9s rows from %s", spec.name, f"{len(df):,}", path.name)
    return df, scan.record_count
