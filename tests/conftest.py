"""Shared test fixtures.

Integration tests need a running PostgreSQL server. They use a SEPARATE database
(default ``retail_pipeline_test``, override with TEST_DB_NAME) which is created on demand
and wiped before every test. If the server is unreachable they are skipped, not failed.

The tiny dataset below is small enough to compute every expected number by hand:

 order | status    | what it exercises
 ------+-----------+--------------------------------------------------------------------
 o1    | delivered | on time; 2 items, 1 seller                          -> eligible
 o2    | delivered | late (timestamp AND calendar day)                   -> eligible, late
 o3    | delivered | delivered on the estimated date at 15:00            -> eligible, late by
       |           | timestamp but NOT by calendar day; 2 sellers, 2 categories
 o4    | canceled  | no items, but a delivery date (as in the real data) -> flagged, NOT eligible
 o5    | delivered | status delivered but delivery date missing          -> flagged, not eligible
 o6    | delivered | carrier pick-up before purchase (and before approval) -> flagged, eligible
 o7    | delivered | delivered before purchase and before carrier        -> flagged, not eligible

 Expected: 7 orders, 8 items, 4 eligible (o1,o2,o3,o6), 2 late (o2,o3),
           1 late by calendar day (o2), 1 canceled, 4 with has_dq_flag (o4,o5,o6,o7).
"""
from __future__ import annotations

import copy
import csv
import os
from pathlib import Path

import psycopg2
import pytest

from src import config

ORDERS = "olist_orders_dataset.csv"
ITEMS = "olist_order_items_dataset.csv"
PRODUCTS = "olist_products_dataset.csv"
CUSTOMERS = "olist_customers_dataset.csv"
SELLERS = "olist_sellers_dataset.csv"
TRANSLATION = "product_category_name_translation.csv"

TINY = {
    ORDERS: (
        ["order_id", "customer_id", "order_status", "order_purchase_timestamp", "order_approved_at",
         "order_delivered_carrier_date", "order_delivered_customer_date", "order_estimated_delivery_date"],
        [
            ["o1", "c1", "delivered", "2018-01-01 10:00:00", "2018-01-01 10:30:00", "2018-01-02 09:00:00", "2018-01-05 12:00:00", "2018-01-10 00:00:00"],
            ["o2", "c2", "delivered", "2018-01-02 10:00:00", "2018-01-02 11:00:00", "2018-01-03 10:00:00", "2018-01-12 09:00:00", "2018-01-08 00:00:00"],
            ["o3", "c3", "delivered", "2018-02-01 08:00:00", "2018-02-01 08:10:00", "2018-02-02 08:00:00", "2018-02-10 15:00:00", "2018-02-10 00:00:00"],
            ["o4", "c4", "canceled", "2018-02-03 09:00:00", "", "", "2018-02-12 09:00:00", "2018-02-20 00:00:00"],
            ["o5", "c5", "delivered", "2018-02-05 09:00:00", "2018-02-05 09:10:00", "2018-02-06 09:00:00", "", "2018-02-15 00:00:00"],
            ["o6", "c1", "delivered", "2018-02-07 09:00:00", "2018-02-07 09:05:00", "2018-02-05 09:00:00", "2018-02-09 09:00:00", "2018-02-20 00:00:00"],
            ["o7", "c2", "delivered", "2018-02-08 09:00:00", "2018-02-08 09:05:00", "2018-02-09 09:00:00", "2018-02-06 09:00:00", "2018-02-18 00:00:00"],
        ],
    ),
    ITEMS: (
        ["order_id", "order_item_id", "product_id", "seller_id", "shipping_limit_date", "price", "freight_value"],
        [
            ["o1", "1", "p1", "s1", "2018-01-03 10:00:00", "10.00", "2.00"],
            ["o1", "2", "p2", "s1", "2018-01-03 10:00:00", "20.00", "3.50"],
            ["o2", "1", "p1", "s2", "2018-01-04 10:00:00", "15.50", "1.00"],
            ["o3", "1", "p1", "s1", "2018-02-03 10:00:00", "5.00", "0.00"],
            ["o3", "2", "p3", "s2", "2018-02-03 10:00:00", "7.25", "1.25"],
            ["o5", "1", "p2", "s3", "2018-02-07 10:00:00", "99.99", "9.99"],
            ["o6", "1", "p3", "s3", "2018-02-08 10:00:00", "12.00", "2.00"],
            ["o7", "1", "p1", "s3", "2018-02-10 10:00:00", "40.00", "4.00"],
        ],
    ),
    PRODUCTS: (
        ["product_id", "product_category_name", "product_name_lenght", "product_description_lenght",
         "product_photos_qty", "product_weight_g", "product_length_cm", "product_height_cm", "product_width_cm"],
        [
            ["p1", "beleza_saude", "40", "287", "1", "225", "16", "10", "14"],
            ["p2", "pc_gamer", "44", "276", "2", "1000", "30", "18", "20"],
            ["p3", "", "", "", "", "500", "20", "10", "10"],
        ],
    ),
    CUSTOMERS: (
        ["customer_id", "customer_unique_id", "customer_zip_code_prefix", "customer_city", "customer_state"],
        [
            ["c1", "u1", "01001", "sao paulo", "SP"],
            ["c2", "u2", "20040", "rio de janeiro", "RJ"],
            ["c3", "u3", "09790", "sao bernardo do campo", "SP"],
            ["c4", "u4", "30110", "belo horizonte", "MG"],
            ["c5", "u5", "01310", "sao paulo", "SP"],
        ],
    ),
    SELLERS: (
        ["seller_id", "seller_zip_code_prefix", "seller_city", "seller_state"],
        [["s1", "13023", "campinas", "SP"], ["s2", "13844", "mogi guacu", "SP"], ["s3", "04101", "sao paulo", "SP"]],
    ),
    TRANSLATION: (
        ["product_category_name", "product_category_name_english"],
        [["beleza_saude", "health_beauty"], ["informatica_acessorios", "computers_accessories"]],
    ),
}

EXPECTED = {
    "orders": 7, "items": 8, "eligible": 4, "late": 2, "late_calendar": 1,
    "canceled": 1, "has_dq_flag": 4,
}


def tiny_data() -> dict:
    """A fresh, mutable deep copy of the tiny dataset."""
    return copy.deepcopy(TINY)


def set_value(data: dict, filename: str, row: int, column: str, value: str) -> None:
    header, rows = data[filename]
    rows[row][header.index(column)] = value


def write_dataset(directory: Path, data: dict, bom_files: tuple = (TRANSLATION,)) -> Path:
    """Write the dataset as CSV files. ``bom_files`` get a UTF-8 BOM, like the real data."""
    directory.mkdir(parents=True, exist_ok=True)
    for filename, (header, rows) in data.items():
        enc = "utf-8-sig" if filename in bom_files else "utf-8"
        with open(directory / filename, "w", encoding=enc, newline="") as fh:
            writer = csv.writer(fh, quoting=csv.QUOTE_MINIMAL)
            writer.writerow(header)
            writer.writerows(rows)
    return directory


@pytest.fixture
def tiny_dir(tmp_path) -> Path:
    return write_dataset(tmp_path / "raw", tiny_data())


# --------------------------------------------------------------------------- database
@pytest.fixture(scope="session")
def db_params() -> dict:
    """Connection parameters for the dedicated TEST database (created if missing)."""
    test_name = os.getenv("TEST_DB_NAME", "retail_pipeline_test")
    try:
        main = config.get_db_params()
        test = config.get_db_params(test_name)
    except config.ConfigError as exc:
        pytest.skip(f"database not configured: {exc}")
    if test["dbname"] == main["dbname"]:
        pytest.fail("TEST_DB_NAME must differ from DB_NAME: tests wipe their database!")
    try:
        admin = psycopg2.connect(**main)
    except psycopg2.Error as exc:
        pytest.skip(f"PostgreSQL not reachable: {exc}")
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (test_name,))
        if cur.fetchone() is None:
            cur.execute(f'CREATE DATABASE "{test_name}"')
    admin.close()
    return test


@pytest.fixture
def clean_db(db_params) -> dict:
    """Test database with no pipeline schemas, wiped before the test."""
    conn = psycopg2.connect(**db_params)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS analytics, staging, audit CASCADE")
    conn.close()
    return db_params


def query(params: dict, sql: str, args=None) -> list:
    conn = psycopg2.connect(**params)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall() if cur.description else []
    finally:
        conn.close()


def scalar(params: dict, sql: str, args=None):
    return query(params, sql, args)[0][0]
