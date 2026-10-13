"""Transform: typed, cleaned DataFrames ready for loading into the staging tables.

Rules (all documented in the README):

* Text is trimmed. Empty strings and the tokens in ``config.NULL_TOKENS`` become NULL.
* Timestamps must match ``YYYY-MM-DD HH:MM:SS`` and fall within a plausible window.
* Integers must be whole numbers; decimals must be finite numbers.
* A value that cannot be parsed becomes NULL, is counted and reported with examples, and
  the run FAILS if the share of such values in any one column exceeds a threshold.
  Rows are never silently dropped.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .config import (
    MAX_VALID_TIMESTAMP,
    MIN_VALID_TIMESTAMP,
    NULL_TOKENS,
    TIMESTAMP_FORMAT,
    TableSpec,
)

log = logging.getLogger(__name__)


class TransformError(RuntimeError):
    """Raised when too many values in a column cannot be parsed."""


@dataclass
class TableReport:
    table: str
    source_rows: int
    rows_out: int = 0
    null_counts: dict = field(default_factory=dict)
    bad_values: dict = field(default_factory=dict)  # column -> {"count": n, "examples": [...]}

    def to_dict(self) -> dict:
        return {
            "source_rows": self.source_rows,
            "rows_out": self.rows_out,
            "null_counts": {k: v for k, v in self.null_counts.items() if v},
            "bad_values": self.bad_values,
        }


def _null_mask(stripped: pd.Series) -> pd.Series:
    return stripped.str.lower().isin(NULL_TOKENS)


def _clean_text(raw: pd.Series) -> tuple[pd.Series, pd.Series]:
    stripped = raw.str.strip()
    null = _null_mask(stripped)
    return stripped.mask(null), pd.Series(False, index=raw.index)


def _clean_int(raw: pd.Series) -> tuple[pd.Series, pd.Series]:
    stripped = raw.str.strip()
    null = _null_mask(stripped)
    num = pd.to_numeric(stripped.mask(null), errors="coerce").astype("float64")
    finite = np.isfinite(num)
    whole = finite & (num == np.floor(num.where(finite, 0)))
    bad = ~null & ~whole
    out = num.where(whole).astype("Int64")
    return out, bad


def _clean_decimal(raw: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Validate as numbers but keep the original text so no precision is lost."""
    stripped = raw.str.strip()
    null = _null_mask(stripped)
    num = pd.to_numeric(stripped.mask(null), errors="coerce").astype("float64")
    ok = np.isfinite(num)
    bad = ~null & ~ok
    return stripped.mask(null | bad), bad


def _clean_timestamp(raw: pd.Series) -> tuple[pd.Series, pd.Series]:
    stripped = raw.str.strip()
    null = _null_mask(stripped)
    parsed = pd.to_datetime(stripped.mask(null), format=TIMESTAMP_FORMAT, errors="coerce")
    in_range = (parsed >= pd.Timestamp(MIN_VALID_TIMESTAMP)) & (
        parsed < pd.Timestamp(MAX_VALID_TIMESTAMP)
    )
    bad = ~null & ~in_range.fillna(False)
    return parsed.where(~bad), bad


_CLEANERS = {
    "text": _clean_text,
    "int": _clean_int,
    "decimal": _clean_decimal,
    "timestamp": _clean_timestamp,
}


def clean_table(
    df: pd.DataFrame, spec: TableSpec, source_rows: int, max_bad_pct: float
) -> tuple[pd.DataFrame, TableReport]:
    """Apply the cleaning rules to every column of one table."""
    report = TableReport(table=spec.name, source_rows=source_rows)
    out = pd.DataFrame(index=df.index)
    n = len(df)

    for col, kind in spec.columns.items():
        cleaned, bad = _CLEANERS[kind](df[col])
        out[col] = cleaned
        report.null_counts[col] = int(cleaned.isna().sum())

        bad_count = int(bad.sum())
        if bad_count:
            examples = df.loc[bad, col].head(3).tolist()
            report.bad_values[col] = {"count": bad_count, "examples": examples}
            pct = 100.0 * bad_count / n
            msg = (
                f"{spec.name}.{col}: {bad_count} unparseable {kind} value(s) "
                f"({pct:.3f}% of rows), e.g. {examples}"
            )
            if pct > max_bad_pct:
                raise TransformError(f"{msg} - exceeds the {max_bad_pct}% threshold")
            log.warning("%s - set to NULL", msg)

    report.rows_out = len(out)
    return out, report
