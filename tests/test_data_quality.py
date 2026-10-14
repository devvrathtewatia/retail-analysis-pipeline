"""Tests of the data-quality checks themselves, and of the real data against an
independent calculation.

A check that can never fail is worthless, so several tests below corrupt the analytics
layer on purpose and assert that the matching check catches it.
"""
from __future__ import annotations

import psycopg2
import pytest

from src import config, quality, run_pipeline

from .conftest import TINY, query, scalar


def results_by_name(params, stage=None) -> dict:
    conn = psycopg2.connect(**params)
    try:
        with conn.cursor() as cur:
            out = {}
            for st in ([stage] if stage else ["staging", "analytics"]):
                out.update({r.name: r for r in quality.run_checks(cur, st)})
            return out
    finally:
        conn.close()


@pytest.fixture
def loaded(clean_db, tiny_dir):
    assert run_pipeline.run(tiny_dir, clean_db) == 0
    return clean_db


# --------------------------------------------------------------------------- registry sanity
def test_check_names_are_unique_and_valid():
    names = [c.name for c in quality.ALL_CHECKS]
    assert len(names) == len(set(names))
    assert all(c.severity in (quality.CRITICAL, quality.WARN) for c in quality.ALL_CHECKS)
    assert all(c.stage in ("staging", "analytics") for c in quality.ALL_CHECKS)
    assert len(quality.ALL_CHECKS) >= 5  # handbook minimum; we have far more


def test_every_check_runs_and_critical_checks_pass_on_clean_data(loaded):
    res = results_by_name(loaded)
    assert len(res) == len(quality.ALL_CHECKS)
    assert [n for n, r in res.items() if r.status == "FAIL"] == []


def test_warnings_match_the_known_issues_in_the_tiny_dataset(loaded):
    res = results_by_name(loaded)
    warn = {n: r.violations for n, r in res.items() if r.status == "WARN"}
    assert warn == {
        "products_missing_category": 1,                # p3
        "categories_missing_translation": 1,           # pc_gamer
        "flag_carrier_before_purchase": 1,             # o6
        "flag_carrier_before_approval": 1,             # o6
        "flag_delivered_before_carrier": 1,            # o7
        "flag_delivered_before_purchase": 1,           # o7
        "flag_delivered_status_missing_date": 1,       # o5
        "flag_no_items": 1,                            # o4
        "flag_delivery_date_on_non_delivered": 1,      # o4 (canceled but has a delivery date)
    }


# --------------------------------------------------------------------------- checks can fail
def _corrupt(params, sql):
    conn = psycopg2.connect(**params)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.close()


@pytest.mark.parametrize("corruption, expected_failing_check", [
    # late flag set on an order that is not eligible
    ("UPDATE analytics.fact_orders SET is_late = true WHERE order_id = 'o4'",
     "late_flag_only_on_eligible_orders"),
    # an order loses its late flag although eligible
    ("UPDATE analytics.fact_orders SET is_late = NULL WHERE order_id = 'o2'",
     "late_flag_only_on_eligible_orders"),
    # a row silently disappears from the fact table
    ("DELETE FROM analytics.fact_order_items WHERE order_id = 'o1' AND order_item_id = 2",
     "rowcount_staging_order_items_equals_analytics_fact_order_items"),
    # an item is double counted in the order total
    ("UPDATE analytics.fact_orders SET items_value = items_value * 2 WHERE order_id = 'o1'",
     "order_totals_reconcile_to_items"),
    # eligible order with a negative duration
    ("UPDATE analytics.fact_orders SET delivery_days = -1 WHERE order_id = 'o1'",
     "eligible_orders_delivery_days_non_negative"),
    # calendar-day lateness without timestamp lateness is impossible
    ("UPDATE analytics.fact_orders SET is_late = false WHERE order_id = 'o2'",
     "calendar_day_late_subset_of_timestamp_late"),
    # an eligible order loses its delivery timestamp
    ("UPDATE analytics.fact_orders SET delivered_at = NULL WHERE order_id = 'o1'",
     "eligible_orders_have_required_timestamps"),
])
def test_analytics_checks_detect_corruption(loaded, corruption, expected_failing_check):
    assert results_by_name(loaded, "analytics")[expected_failing_check].status == "PASS"
    _corrupt(loaded, corruption)
    assert results_by_name(loaded, "analytics")[expected_failing_check].status == "FAIL"


def test_late_count_exceeding_eligible_is_detected(loaded):
    """The handbook's explicit rule: late orders can never exceed eligible orders."""
    _corrupt(loaded, "UPDATE analytics.fact_orders SET is_delivery_eligible = false "
                     "WHERE order_id IN ('o1', 'o2', 'o3', 'o6')")  # late flags remain set
    assert results_by_name(loaded, "analytics")["late_orders_not_exceed_eligible"].status == "FAIL"


def test_staging_checks_detect_corruption(loaded):
    _corrupt(loaded, "INSERT INTO staging.orders (order_id, customer_id, order_status, order_purchase_timestamp) "
                     "VALUES ('o1', 'c1', 'delivered', now()), (NULL, 'c1', 'delivered', now())")
    res = results_by_name(loaded, "staging")
    assert res["orders_pk_unique"].violations == 1
    assert res["orders_order_id_not_null"].violations == 1


# --------------------------------------------------------------------------- real data
RAW = config.get_raw_data_dir()
REAL_FILES = [s.filename for s in config.TABLE_SPECS.values()]


@pytest.mark.skipif(not all((RAW / f).is_file() for f in REAL_FILES), reason="raw data files not present")
def test_real_data_load_matches_an_independent_pandas_calculation():
    """Load the REAL files into the test database, then recompute every headline number
    from the CSVs with plain pandas (no shared code with the pipeline) and compare."""
    import numpy as np
    import pandas as pd

    try:
        main = config.get_db_params()
        import os
        test = config.get_db_params(os.getenv("TEST_DB_NAME", "retail_pipeline_test"))
        psycopg2.connect(**main).close()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"database unavailable: {exc}")
    assert test["dbname"] != main["dbname"]
    _corrupt(test, "DROP SCHEMA IF EXISTS analytics, staging, audit CASCADE")

    assert run_pipeline.run(RAW, test) == 0

    rd = lambda f: pd.read_csv(RAW / f, dtype=str, keep_default_na=False, encoding="utf-8-sig")  # noqa: E731
    o, i = rd("olist_orders_dataset.csv"), rd("olist_order_items_dataset.csv")
    for c in ["order_purchase_timestamp", "order_delivered_customer_date", "order_estimated_delivery_date"]:
        o[c] = pd.to_datetime(o[c].replace("", None))
    elig = o[(o.order_status == "delivered") & o.order_delivered_customer_date.notna()
             & o.order_estimated_delivery_date.notna()
             & (o.order_delivered_customer_date >= o.order_purchase_timestamp)]
    late = elig.order_delivered_customer_date > elig.order_estimated_delivery_date
    late_cal = elig.order_delivered_customer_date.dt.normalize() > elig.order_estimated_delivery_date.dt.normalize()
    days = (elig.order_delivered_customer_date - elig.order_purchase_timestamp).dt.total_seconds() / 86400

    got = query(test, """
        SELECT COUNT(*) FILTER (WHERE is_delivery_eligible), COUNT(*) FILTER (WHERE is_late),
               COUNT(*) FILTER (WHERE is_late_calendar_day), COUNT(*) FILTER (WHERE is_canceled),
               AVG(delivery_days)::float8, COUNT(*)
        FROM analytics.fact_orders""")[0]
    assert got[0] == len(elig)
    assert got[1] == int(late.sum())
    assert got[2] == int(late_cal.sum())
    assert got[3] == int((o.order_status == "canceled").sum())
    assert got[4] == pytest.approx(days.mean(), abs=1e-3)
    assert got[5] == len(o)

    # item-level reconciliation: totals must match the raw file to the cent
    assert float(scalar(test, "SELECT SUM(price) FROM analytics.fact_order_items")) == pytest.approx(
        pd.to_numeric(i.price).sum(), abs=0.01)
    assert scalar(test, "SELECT COUNT(*) FROM analytics.fact_order_items") == len(i)

    # NULL accounting: nulls in staging equal empty cells in the CSV
    assert scalar(test, "SELECT COUNT(*) FROM staging.orders WHERE order_delivered_customer_date IS NULL") == int(
        (o.order_delivered_customer_date.isna()).sum())

    # known data issues in the public dataset are flagged, not hidden
    assert scalar(test, "SELECT COUNT(*) FROM analytics.fact_orders WHERE dq_carrier_before_purchase") == 166
    assert scalar(test, "SELECT COUNT(*) FROM analytics.fact_orders WHERE dq_delivered_status_missing_date") == 8
    assert scalar(test, "SELECT COUNT(*) FROM audit.dq_results WHERE status = 'FAIL'") == 0
    assert np.isfinite(days).all()
