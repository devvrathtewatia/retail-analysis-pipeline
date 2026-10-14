"""Unit tests for extract.py and transform.py. No database required."""
from __future__ import annotations

import pandas as pd
import pytest

from src import config, extract, transform
from src.config import TableSpec

from .conftest import ITEMS, ORDERS, TRANSLATION, set_value, tiny_data, write_dataset


def make_spec(columns: dict) -> TableSpec:
    return TableSpec(name="t", filename="t.csv", columns=columns, primary_key=(next(iter(columns)),))


def clean(values: list, kind: str, max_bad_pct: float = 100.0):
    df = pd.DataFrame({"a": values, "k": ["x"] * len(values)})
    spec = make_spec({"k": "text", "a": kind})
    out, report = transform.clean_table(df, spec, len(df), max_bad_pct)
    return out["a"], report


# --------------------------------------------------------------------------- nulls / text
def test_text_is_trimmed_and_null_tokens_become_null():
    out, report = clean(["  abc ", "", "   ", "NULL", "null", "NaN", "nan", "None", "NA"], "text")
    assert out.iloc[0] == "abc"
    assert out.iloc[1:7].isna().all()
    # "None" and "NA" are deliberately NOT null tokens (could be real values)
    assert out.iloc[7] == "None" and out.iloc[8] == "NA"
    assert report.null_counts["a"] == 6


def test_leading_zeros_in_text_are_preserved():
    out, _ = clean(["09790", "01001"], "text")
    assert out.tolist() == ["09790", "01001"]


# --------------------------------------------------------------------------- timestamps
def test_valid_timestamps_parse():
    out, report = clean(["2018-01-05 12:30:45", ""], "timestamp")
    assert out.iloc[0] == pd.Timestamp("2018-01-05 12:30:45")
    assert pd.isna(out.iloc[1])
    assert report.bad_values == {}


@pytest.mark.parametrize("bad", ["not-a-date", "2018-13-45 00:00:00", "05/01/2018", "2018-01-05",
                                 "1900-01-01 00:00:00", "2500-01-01 00:00:00"])
def test_malformed_or_impossible_timestamps_are_nulled_and_reported(bad):
    out, report = clean(["2018-01-05 12:30:45", bad], "timestamp")
    assert pd.isna(out.iloc[1])
    assert report.bad_values["a"]["count"] == 1
    assert report.bad_values["a"]["examples"] == [bad]


def test_bad_values_above_threshold_fail_the_run():
    values = ["2018-01-05 12:30:45"] * 9 + ["garbage"]  # 10% bad
    with pytest.raises(transform.TransformError, match="exceeds the 1.0% threshold"):
        clean(values, "timestamp", max_bad_pct=1.0)
    clean(values, "timestamp", max_bad_pct=10.0)  # exactly at the limit is tolerated


# --------------------------------------------------------------------------- numbers
def test_int_accepts_whole_numbers_only():
    out, report = clean(["225", " 7 ", "12.0", "12.5", "abc", "inf", ""], "int")
    assert out.iloc[:3].tolist() == [225, 7, 12]
    assert out.iloc[3:6].isna().all() and out.iloc[6] is pd.NA
    assert report.bad_values["a"]["count"] == 3  # 12.5, abc, inf


def test_decimal_keeps_original_text_and_flags_non_numbers():
    out, report = clean(["58.90", " 0.85 ", "abc", "inf", ""], "decimal")
    assert out.iloc[0] == "58.90" and out.iloc[1] == "0.85"
    assert out.iloc[2:].isna().all()
    assert report.bad_values["a"]["count"] == 2  # abc, inf


# --------------------------------------------------------------------------- extract
def test_bom_file_is_read_with_correct_first_column(tmp_path):
    write_dataset(tmp_path, tiny_data())  # translation file is written with a BOM
    raw = (tmp_path / TRANSLATION).read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    df, n = extract.read_table(tmp_path, config.TABLE_SPECS["product_category_translation"])
    assert list(df.columns)[0] == "product_category_name"
    assert n == 2 and df.iloc[0, 0] == "beleza_saude"


def test_missing_file_raises(tmp_path):
    with pytest.raises(extract.ExtractError, match="not found"):
        extract.read_table(tmp_path, config.TABLE_SPECS["orders"])


def test_missing_column_raises_with_column_name(tmp_path):
    data = tiny_data()
    header, rows = data[ORDERS]
    idx = header.index("order_estimated_delivery_date")
    data[ORDERS] = ([h for i, h in enumerate(header) if i != idx], [[v for i, v in enumerate(r) if i != idx] for r in rows])
    write_dataset(tmp_path, data)
    with pytest.raises(extract.ExtractError, match="order_estimated_delivery_date"):
        extract.read_table(tmp_path, config.TABLE_SPECS["orders"])


def test_extra_column_is_ignored(tmp_path):
    data = tiny_data()
    header, rows = data[ORDERS]
    data[ORDERS] = (header + ["surprise"], [r + ["x"] for r in rows])
    write_dataset(tmp_path, data)
    df, n = extract.read_table(tmp_path, config.TABLE_SPECS["orders"])
    assert "surprise" not in df.columns and n == 7


def test_header_only_file_raises(tmp_path):
    data = tiny_data()
    data[ORDERS] = (data[ORDERS][0], [])
    write_dataset(tmp_path, data)
    with pytest.raises(extract.ExtractError, match="no data rows"):
        extract.read_table(tmp_path, config.TABLE_SPECS["orders"])


def test_zero_byte_file_raises(tmp_path):
    write_dataset(tmp_path, tiny_data())
    (tmp_path / ORDERS).write_bytes(b"")
    with pytest.raises(extract.ExtractError, match="empty"):
        extract.read_table(tmp_path, config.TABLE_SPECS["orders"])


def test_ragged_row_raises(tmp_path):
    write_dataset(tmp_path, tiny_data())
    with open(tmp_path / ITEMS, "a", encoding="utf-8") as fh:
        fh.write("o9,1,p1\n")  # 3 fields instead of 7
    with pytest.raises(extract.ExtractError, match="do not have 7 fields"):
        extract.read_table(tmp_path, config.TABLE_SPECS["order_items"])


def test_invalid_utf8_raises(tmp_path):
    write_dataset(tmp_path, tiny_data())
    with open(tmp_path / ORDERS, "ab") as fh:
        fh.write(b"o9,c1,delivered,\xff\xfe,,,,\n")
    with pytest.raises(extract.ExtractError, match="UTF-8"):
        extract.read_table(tmp_path, config.TABLE_SPECS["orders"])


def test_blank_lines_are_ignored_consistently(tmp_path):
    write_dataset(tmp_path, tiny_data())
    with open(tmp_path / ORDERS, "a", encoding="utf-8") as fh:
        fh.write("\n\n")
    df, n = extract.read_table(tmp_path, config.TABLE_SPECS["orders"])
    assert len(df) == n == 7


def test_quoted_commas_and_newlines_are_handled(tmp_path):
    data = tiny_data()
    set_value(data, "olist_customers_dataset.csv", 0, "customer_city", 'sao, "paulo"\nzona sul')
    write_dataset(tmp_path, data)
    df, n = extract.read_table(tmp_path, config.TABLE_SPECS["customers"])
    assert n == len(df) == 5
    assert df.loc[0, "customer_city"] == 'sao, "paulo"\nzona sul'
