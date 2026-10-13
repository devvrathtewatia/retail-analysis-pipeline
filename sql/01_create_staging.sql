-- STAGING: a typed copy of the source files. One staging table per CSV.
--
--   * Source column names and identifiers are preserved (including the source's own
--     "lenght" misspelling) so every column traces back to the raw file.
--   * No primary keys / foreign keys on purpose: staging is a landing zone and must be
--     able to hold bad data so the data-quality checks can SEE and report it. Keys are
--     enforced in the analytics layer.
--   * Timestamps are "timestamp without time zone": the source has no timezone info.
--   * Zip-code prefixes are text because they have leading zeros ("09790").
--   * Every table is fully refreshed on each run (TRUNCATE + COPY inside one transaction).

CREATE SCHEMA IF NOT EXISTS staging;

CREATE TABLE IF NOT EXISTS staging.orders (
    order_id                       text,
    customer_id                    text,
    order_status                   text,
    order_purchase_timestamp       timestamp,
    order_approved_at              timestamp,
    order_delivered_carrier_date   timestamp,
    order_delivered_customer_date  timestamp,
    order_estimated_delivery_date  timestamp,
    _loaded_at                     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS staging.order_items (
    order_id             text,
    order_item_id        integer,
    product_id           text,
    seller_id            text,
    shipping_limit_date  timestamp,
    price                numeric(12, 2),
    freight_value        numeric(12, 2),
    _loaded_at           timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS staging.products (
    product_id                  text,
    product_category_name       text,
    product_name_lenght         integer,
    product_description_lenght  integer,
    product_photos_qty          integer,
    product_weight_g            integer,
    product_length_cm           integer,
    product_height_cm           integer,
    product_width_cm            integer,
    _loaded_at                  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS staging.customers (
    customer_id               text,
    customer_unique_id        text,
    customer_zip_code_prefix  text,
    customer_city             text,
    customer_state            text,
    _loaded_at                timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS staging.sellers (
    seller_id               text,
    seller_zip_code_prefix  text,
    seller_city             text,
    seller_state            text,
    _loaded_at              timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS staging.product_category_translation (
    product_category_name          text,
    product_category_name_english  text,
    _loaded_at                     timestamptz NOT NULL DEFAULT now()
);
