"""End-to-end tests of the pipeline against a real (throwaway) PostgreSQL database."""
from __future__ import annotations

import psycopg2
import pytest

from src import config, run_pipeline
from src.run_analysis import parse_queries, run_queries

from .conftest import (
    CUSTOMERS, EXPECTED, ITEMS, ORDERS, PRODUCTS, TRANSLATION,
    query, scalar, set_value, tiny_data, write_dataset,
)


def run(params, data_dir, **kw) -> int:
    return run_pipeline.run(data_dir, params, **kw)


def counts(params) -> dict:
    sql = """
        SELECT 'stg_orders', COUNT(*) FROM staging.orders
        UNION ALL SELECT 'stg_items', COUNT(*) FROM staging.order_items
        UNION ALL SELECT 'stg_products', COUNT(*) FROM staging.products
        UNION ALL SELECT 'fact_orders', COUNT(*) FROM analytics.fact_orders
        UNION ALL SELECT 'fact_items', COUNT(*) FROM analytics.fact_order_items
        UNION ALL SELECT 'dim_customers', COUNT(*) FROM analytics.dim_customers
        UNION ALL SELECT 'dim_products', COUNT(*) FROM analytics.dim_products
        UNION ALL SELECT 'dim_sellers', COUNT(*) FROM analytics.dim_sellers"""
    return dict(query(params, sql))


def last_run(params):
    return query(params, "SELECT status, failed_stage, error_message, details "
                         "FROM audit.pipeline_runs ORDER BY run_id DESC LIMIT 1")[0]


# --------------------------------------------------------------------------- happy path
def test_normal_run_produces_expected_numbers(clean_db, tiny_dir):
    assert run(clean_db, tiny_dir) == 0

    assert counts(clean_db) == {
        "stg_orders": 7, "stg_items": 8, "stg_products": 3,
        "fact_orders": 7, "fact_items": 8,
        "dim_customers": 5, "dim_products": 3, "dim_sellers": 3,
    }
    row = query(clean_db, """
        SELECT COUNT(*) FILTER (WHERE is_delivery_eligible), COUNT(*) FILTER (WHERE is_late),
               COUNT(*) FILTER (WHERE is_late_calendar_day), COUNT(*) FILTER (WHERE is_canceled),
               COUNT(*) FILTER (WHERE has_dq_flag), SUM(items_value), SUM(item_count)
        FROM analytics.fact_orders""")[0]
    assert row[:5] == (EXPECTED["eligible"], EXPECTED["late"], EXPECTED["late_calendar"],
                       EXPECTED["canceled"], EXPECTED["has_dq_flag"])
    assert float(row[5]) == pytest.approx(209.74) and row[6] == 8

    status, stage, error, details = last_run(clean_db)
    assert (status, stage, error) == ("SUCCESS", None, None)
    assert details["dq_summary"]["fail"] == 0
    assert details["tables"]["orders"]["source_rows"] == 7
    assert scalar(clean_db, "SELECT COUNT(*) FROM audit.dq_results WHERE status = 'FAIL'") == 0


def test_metric_values_for_each_order(clean_db, tiny_dir):
    assert run(clean_db, tiny_dir) == 0
    rows = {r[0]: r[1:] for r in query(clean_db, """
        SELECT order_id, is_delivery_eligible, is_late, is_late_calendar_day,
               ROUND(delivery_days, 4)::float8, ROUND(days_late, 4)::float8, item_count, seller_count
        FROM analytics.fact_orders ORDER BY order_id""")}
    # eligible, on time: 2018-01-01 10:00 -> 2018-01-05 12:00 = 4 days 2 h; est 2018-01-10
    assert rows["o1"] == (True, False, False, pytest.approx(4.0833, abs=1e-4), pytest.approx(-4.5, abs=1e-4), 2, 1)
    # late both ways: delivered 2018-01-12 09:00 vs est 2018-01-08 00:00 = 4.375 days late
    assert rows["o2"][:3] == (True, True, True) and rows["o2"][4] == pytest.approx(4.375)
    # same calendar day as estimate -> late by timestamp, on time by calendar day
    assert rows["o3"][:3] == (True, True, False) and rows["o3"][4] == pytest.approx(0.625)
    assert rows["o3"][5:] == (2, 2)  # two items, two sellers
    # not applicable -> NULL, not false
    for oid in ("o4", "o5", "o7"):
        assert rows[oid][:3] == (False, None, None) and rows[oid][3] is None
    assert rows["o4"][5:] == (0, 0)  # no items


def test_category_fallbacks(clean_db, tiny_dir):
    assert run(clean_db, tiny_dir) == 0
    cats = dict(query(clean_db, "SELECT product_id, category_name_en FROM analytics.dim_products"))
    assert cats == {"p1": "health_beauty",  # translated
                    "p2": "pc_gamer",        # no translation -> Portuguese name
                    "p3": "unknown"}         # no category at all


def test_zip_prefix_keeps_leading_zeros(clean_db, tiny_dir):
    assert run(clean_db, tiny_dir) == 0
    assert scalar(clean_db, "SELECT zip_code_prefix FROM analytics.dim_customers WHERE customer_id = 'c1'") == "01001"


def test_views_do_not_double_count(clean_db, tiny_dir):
    assert run(clean_db, tiny_dir) == 0
    # o1 has two items from the same seller -> ONE (order, seller) row with item_count 2
    assert query(clean_db, "SELECT item_count FROM analytics.v_order_sellers "
                           "WHERE order_id = 'o1' AND seller_id = 's1'") == [(2,)]
    assert scalar(clean_db, "SELECT COUNT(*) FROM analytics.v_order_sellers WHERE order_id = 'o1'") == 1
    # o3 spans two sellers and two categories -> two rows in each view
    assert scalar(clean_db, "SELECT COUNT(*) FROM analytics.v_order_sellers WHERE order_id = 'o3'") == 2
    assert scalar(clean_db, "SELECT COUNT(*) FROM analytics.v_order_categories WHERE order_id = 'o3'") == 2
    # o1's two items are in different categories (health_beauty, pc_gamer)
    assert scalar(clean_db, "SELECT COUNT(*) FROM analytics.v_order_categories WHERE order_id = 'o1'") == 2


# --------------------------------------------------------------------------- reruns
def test_second_run_does_not_duplicate_anything(clean_db, tiny_dir):
    assert run(clean_db, tiny_dir) == 0
    first = counts(clean_db)
    assert run(clean_db, tiny_dir) == 0
    assert counts(clean_db) == first
    # key counts equal row counts => no duplicate keys at the table grain
    assert scalar(clean_db, "SELECT COUNT(DISTINCT order_id) FROM staging.orders") == 7
    assert scalar(clean_db, "SELECT COUNT(DISTINCT (order_id, order_item_id)) FROM staging.order_items") == 8
    assert scalar(clean_db, "SELECT COUNT(DISTINCT order_id) FROM analytics.fact_orders") == 7
    runs = query(clean_db, "SELECT status FROM audit.pipeline_runs ORDER BY run_id")
    assert runs == [("SUCCESS",), ("SUCCESS",)]


def test_rerun_with_changed_input_replaces_data(clean_db, tmp_path):
    data = tiny_data()
    assert run(clean_db, write_dataset(tmp_path / "a", data)) == 0
    set_value(data, ORDERS, 1, "order_delivered_customer_date", "2018-01-07 09:00:00")  # o2 now on time
    assert run(clean_db, write_dataset(tmp_path / "b", data)) == 0
    assert scalar(clean_db, "SELECT COUNT(*) FROM analytics.fact_orders WHERE is_late") == 1  # only o3
    assert scalar(clean_db, "SELECT COUNT(*) FROM analytics.fact_orders") == 7  # not 14


def test_rebuild_schema_flag(clean_db, tiny_dir):
    assert run(clean_db, tiny_dir) == 0
    assert run(clean_db, tiny_dir, rebuild_schema=True) == 0
    assert counts(clean_db)["fact_orders"] == 7
    assert scalar(clean_db, "SELECT COUNT(*) FROM audit.pipeline_runs") == 2  # audit history survives


# --------------------------------------------------------------------------- input failures
def test_missing_input_file_fails_and_keeps_previous_data(clean_db, tmp_path):
    good = write_dataset(tmp_path / "good", tiny_data())
    assert run(clean_db, good) == 0
    before = counts(clean_db)

    (good / ITEMS).unlink()
    assert run(clean_db, good) == 1

    status, stage, error, _ = last_run(clean_db)
    assert status == "FAILED" and stage == "extract" and ITEMS in error
    assert counts(clean_db) == before  # previous good data untouched


def test_missing_column_fails_with_clear_message(clean_db, tmp_path):
    data = tiny_data()
    header, rows = data[PRODUCTS]
    idx = header.index("product_category_name")
    data[PRODUCTS] = ([h for i, h in enumerate(header) if i != idx], [[v for i, v in enumerate(r) if i != idx] for r in rows])
    assert run(clean_db, write_dataset(tmp_path, data)) == 1
    status, stage, error, _ = last_run(clean_db)
    assert (status, stage) == ("FAILED", "extract") and "product_category_name" in error


def test_header_only_input_fails_instead_of_wiping_tables(clean_db, tmp_path):
    good = write_dataset(tmp_path / "good", tiny_data())
    assert run(clean_db, good) == 0
    data = tiny_data()
    data[ORDERS] = (data[ORDERS][0], [])
    assert run(clean_db, write_dataset(tmp_path / "empty", data)) == 1
    assert counts(clean_db)["fact_orders"] == 7


def test_malformed_date_over_threshold_fails(clean_db, tmp_path):
    data = tiny_data()
    set_value(data, ORDERS, 0, "order_delivered_customer_date", "31/12/2018")
    assert run(clean_db, write_dataset(tmp_path, data), max_bad_pct=1.0) == 1
    status, stage, error, _ = last_run(clean_db)
    assert (status, stage) == ("FAILED", "transform")
    assert "order_delivered_customer_date" in error and "31/12/2018" in error


def test_malformed_date_under_threshold_is_nulled_reported_and_flagged(clean_db, tmp_path):
    data = tiny_data()
    set_value(data, ORDERS, 0, "order_delivered_customer_date", "31/12/2018")  # o1
    assert run(clean_db, write_dataset(tmp_path, data), max_bad_pct=50.0) == 0
    status, _, _, details = last_run(clean_db)
    assert status == "SUCCESS"
    bad = details["tables"]["orders"]["bad_values"]["order_delivered_customer_date"]
    assert bad["count"] == 1 and bad["examples"] == ["31/12/2018"]
    # o1 lost its delivery date: no longer eligible, and shows up as inconsistent
    assert query(clean_db, "SELECT is_delivery_eligible, dq_delivered_status_missing_date "
                           "FROM analytics.fact_orders WHERE order_id = 'o1'") == [(False, True)]


# --------------------------------------------------------------------------- data failures
def _expect_quality_failure(params, data_dir, check_name):
    assert run(params, data_dir) == 1
    status, stage, error, _ = last_run(params)
    assert (status, stage) == ("FAILED", "quality")
    assert check_name in error
    fails = [r[0] for r in query(params, "SELECT check_name FROM audit.dq_results WHERE status = 'FAIL'")]
    assert check_name in fails


def test_duplicate_order_id_fails_and_rolls_back(clean_db, tmp_path):
    assert run(clean_db, write_dataset(tmp_path / "good", tiny_data())) == 0
    before = counts(clean_db)

    data = tiny_data()
    data[ORDERS][1].append(list(data[ORDERS][1][0]))  # exact duplicate of o1
    _expect_quality_failure(clean_db, write_dataset(tmp_path / "dup", data), "orders_pk_unique")
    assert counts(clean_db) == before  # nothing was replaced


def test_duplicate_item_key_fails(clean_db, tmp_path):
    data = tiny_data()
    data[ITEMS][1].append(list(data[ITEMS][1][0]))
    _expect_quality_failure(clean_db, write_dataset(tmp_path, data), "order_items_pk_unique")


def test_orphan_product_fails(clean_db, tmp_path):
    data = tiny_data()
    set_value(data, ITEMS, 0, "product_id", "does-not-exist")
    _expect_quality_failure(clean_db, write_dataset(tmp_path, data), "fk_order_items_product_id_in_products")


def test_orphan_customer_fails(clean_db, tmp_path):
    data = tiny_data()
    set_value(data, ORDERS, 0, "customer_id", "ghost")
    _expect_quality_failure(clean_db, write_dataset(tmp_path, data), "fk_orders_customer_id_in_customers")


def test_null_required_key_fails(clean_db, tmp_path):
    data = tiny_data()
    set_value(data, ORDERS, 2, "customer_id", "  ")
    _expect_quality_failure(clean_db, write_dataset(tmp_path, data), "orders_customer_id_not_null")


def test_non_positive_price_fails(clean_db, tmp_path):
    data = tiny_data()
    set_value(data, ITEMS, 0, "price", "-5")
    _expect_quality_failure(clean_db, write_dataset(tmp_path, data), "order_items_price_positive")


def test_duplicate_translation_key_fails(clean_db, tmp_path):
    data = tiny_data()
    data[TRANSLATION][1].append(["beleza_saude", "health_beauty_again"])
    _expect_quality_failure(clean_db, write_dataset(tmp_path, data), "product_category_translation_pk_unique")


def test_failed_first_run_leaves_no_partial_tables(clean_db, tmp_path):
    """The DDL is inside the load transaction, so a failed FIRST run creates nothing."""
    data = tiny_data()
    data[ORDERS][1].append(list(data[ORDERS][1][0]))
    assert run(clean_db, write_dataset(tmp_path, data)) == 1
    assert scalar(clean_db, "SELECT to_regclass('staging.orders')") is None
    assert scalar(clean_db, "SELECT to_regclass('analytics.fact_orders')") is None
    assert scalar(clean_db, "SELECT COUNT(*) FROM audit.pipeline_runs WHERE status = 'FAILED'") == 1


# --------------------------------------------------------------------------- environment failures
def test_concurrent_run_is_refused(clean_db, tiny_dir):
    holder = psycopg2.connect(**clean_db)
    holder.autocommit = True
    try:
        with holder.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (config.ADVISORY_LOCK_KEY,))
        assert run(clean_db, tiny_dir) == 1
        status, stage, error, _ = last_run(clean_db)
        assert status == "FAILED" and "already in progress" in error
    finally:
        holder.close()
    assert run(clean_db, tiny_dir) == 0  # lock released -> works again


def test_unreachable_database_returns_failure_not_exception(tiny_dir, db_params):
    bad = dict(db_params, port=1, connect_timeout=2)
    assert run(bad, tiny_dir) == 1


def test_missing_credentials_reported(monkeypatch):
    monkeypatch.delenv("DB_PASSWORD", raising=False)
    monkeypatch.delenv("DB_USER", raising=False)
    with pytest.raises(config.ConfigError, match="DB_USER"):
        config.get_db_params("x")


# --------------------------------------------------------------------------- CLI and analysis
def test_cli_dry_run_needs_no_database(tiny_dir):
    assert run_pipeline.main(["--dry-run", "--data-dir", str(tiny_dir)]) == 0


def test_cli_dry_run_fails_on_bad_input(tmp_path):
    assert run_pipeline.main(["--dry-run", "--data-dir", str(tmp_path)]) == 1


def test_all_analysis_queries_run_and_give_expected_headline(clean_db, tiny_dir):
    assert run(clean_db, tiny_dir) == 0
    queries = parse_queries((config.SQL_DIR / "04_analysis_queries.sql").read_text(encoding="utf-8"))
    assert len(queries) >= 8
    results = {q.name: (cols, rows) for q, cols, rows in run_queries(clean_db, queries)}
    cols, rows = results["q01_overall_late_rate"]
    assert dict(zip(cols, rows[0])) == {
        "eligible_orders": 4, "late_orders": 2, "late_pct_timestamp": pytest.approx(50.0),
        "late_orders_calendar_day": 1, "late_pct_calendar_day": pytest.approx(25.0),
    }


def test_analysis_queries_survive_an_empty_analytics_layer(clean_db, tiny_dir):
    """Division-by-zero guards: queries must not error when nothing is eligible."""
    data = tiny_data()
    for r in data[ORDERS][1]:
        r[2] = "shipped"  # nothing delivered -> nothing eligible
        r[6] = ""
    assert run(clean_db, write_dataset(tiny_dir.parent / "none", data)) == 0
    queries = parse_queries((config.SQL_DIR / "04_analysis_queries.sql").read_text(encoding="utf-8"))
    results = {q.name: rows for q, _, rows in run_queries(clean_db, queries)}
    assert results["q01_overall_late_rate"][0][0] == 0
    assert results["q01_overall_late_rate"][0][2] is None  # rate undefined, not an error
