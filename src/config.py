"""Central configuration: paths, database settings and the table specifications.

Credentials are read from environment variables (optionally via a local, git-ignored
``.env`` file). Nothing secret lives in the code.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

SQL_DIR = PROJECT_ROOT / "sql"
LOG_DIR = PROJECT_ROOT / "logs"
DOCS_DIR = PROJECT_ROOT / "docs"

# Value that both psycopg2 COPY and pandas.to_csv use to represent NULL.
COPY_NULL = r"\N"

# After trimming and lower-casing, these text values are treated as missing.
# Deliberately short: tokens such as "na" or "none" could be legitimate values.
NULL_TOKENS = frozenset({"", "null", "nan"})

# Timestamps in the source are 'YYYY-MM-DD HH:MM:SS' with no timezone information.
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
# Anything outside this window is treated as an impossible date.
MIN_VALID_TIMESTAMP = "2000-01-01"
MAX_VALID_TIMESTAMP = "2100-01-01"

# Key used for the Postgres advisory lock that prevents two simultaneous runs.
ADVISORY_LOCK_KEY = 727_274


class ConfigError(RuntimeError):
    """Raised when required configuration is missing."""


def get_db_params(db_name: str | None = None) -> dict:
    """Build psycopg2 connection keyword arguments from environment variables.

    ``db_name`` overrides ``DB_NAME`` (used by the tests to target a separate database).
    """
    required = ["DB_USER", "DB_PASSWORD"] + ([] if db_name else ["DB_NAME"])
    missing = [v for v in required if not os.getenv(v)]
    if missing:
        raise ConfigError(
            f"Missing environment variable(s): {', '.join(missing)}. "
            "Copy .env.example to .env and fill it in."
        )
    return {
        "host": os.getenv("DB_HOST", "localhost"),
        "port": int(os.getenv("DB_PORT", "5432")),
        "dbname": db_name or os.environ["DB_NAME"],
        "user": os.environ["DB_USER"],
        "password": os.environ["DB_PASSWORD"],
        "connect_timeout": 10,
    }


def get_raw_data_dir() -> Path:
    raw = os.getenv("RAW_DATA_DIR")
    path = Path(raw) if raw else PROJECT_ROOT / "data" / "raw"
    return path if path.is_absolute() else PROJECT_ROOT / path


def get_max_bad_value_pct() -> float:
    """Max tolerated share (%) of unparseable values in any single column."""
    return float(os.getenv("MAX_BAD_VALUE_PCT", "1.0"))


@dataclass(frozen=True)
class TableSpec:
    """Describes one source CSV and its staging table.

    ``columns`` maps column name -> kind, in staging column order. Kinds:
    ``text``, ``int``, ``decimal``, ``timestamp``.
    """

    name: str  # staging table name (schema "staging")
    filename: str
    columns: dict
    primary_key: tuple
    required: tuple = ()  # columns that must never be NULL (key columns)

    @property
    def column_names(self) -> list:
        return list(self.columns)

    @property
    def not_null_columns(self) -> list:
        """Required columns plus the primary key, without duplicates, in order."""
        seen, out = set(), []
        for c in (*self.primary_key, *self.required):
            if c not in seen:
                seen.add(c)
                out.append(c)
        return out


TABLE_SPECS: dict = {
    "orders": TableSpec(
        name="orders",
        filename="olist_orders_dataset.csv",
        columns={
            "order_id": "text",
            "customer_id": "text",
            "order_status": "text",
            "order_purchase_timestamp": "timestamp",
            "order_approved_at": "timestamp",
            "order_delivered_carrier_date": "timestamp",
            "order_delivered_customer_date": "timestamp",
            "order_estimated_delivery_date": "timestamp",
        },
        primary_key=("order_id",),
        required=("customer_id", "order_status", "order_purchase_timestamp"),
    ),
    "order_items": TableSpec(
        name="order_items",
        filename="olist_order_items_dataset.csv",
        columns={
            "order_id": "text",
            "order_item_id": "int",
            "product_id": "text",
            "seller_id": "text",
            "shipping_limit_date": "timestamp",
            "price": "decimal",
            "freight_value": "decimal",
        },
        primary_key=("order_id", "order_item_id"),
        required=("product_id", "seller_id", "price", "freight_value"),
    ),
    "products": TableSpec(
        name="products",
        filename="olist_products_dataset.csv",
        columns={
            "product_id": "text",
            "product_category_name": "text",
            # The source misspells "length" as "lenght"; staging keeps source names.
            "product_name_lenght": "int",
            "product_description_lenght": "int",
            "product_photos_qty": "int",
            "product_weight_g": "int",
            "product_length_cm": "int",
            "product_height_cm": "int",
            "product_width_cm": "int",
        },
        primary_key=("product_id",),
    ),
    "customers": TableSpec(
        name="customers",
        filename="olist_customers_dataset.csv",
        columns={
            "customer_id": "text",
            "customer_unique_id": "text",
            # Text, not int: prefixes have leading zeros (e.g. "09790").
            "customer_zip_code_prefix": "text",
            "customer_city": "text",
            "customer_state": "text",
        },
        primary_key=("customer_id",),
        required=("customer_unique_id",),
    ),
    "sellers": TableSpec(
        name="sellers",
        filename="olist_sellers_dataset.csv",
        columns={
            "seller_id": "text",
            "seller_zip_code_prefix": "text",
            "seller_city": "text",
            "seller_state": "text",
        },
        primary_key=("seller_id",),
    ),
    "product_category_translation": TableSpec(
        name="product_category_translation",
        filename="product_category_name_translation.csv",
        columns={
            "product_category_name": "text",
            "product_category_name_english": "text",
        },
        primary_key=("product_category_name",),
        required=("product_category_name_english",),
    ),
}
