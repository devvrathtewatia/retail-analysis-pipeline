DROP TABLE IF EXISTS
    analytics.exploratory_order_status,
    analytics.exploratory_delivery_performance;
-- ANALYTICS: analysis-ready model built from staging.
-- Tables describe customers, sellers, products, orders and order items.

CREATE SCHEMA IF NOT EXISTS analytics;
-- One row per customer_id
CREATE TABLE IF NOT EXISTS analytics.dim_customers (
    customer_id         TEXT PRIMARY KEY,
    customer_unique_id  TEXT NOT NULL,
    zip_code_prefix     TEXT,
    city                TEXT,
    state               TEXT
);
-- One row per seller_id
CREATE TABLE IF NOT EXISTS analytics.dim_sellers (
    seller_id        TEXT PRIMARY KEY,
    zip_code_prefix  TEXT,
    city             TEXT,
    state            TEXT
);
-- ============================================================
-- BLOCK 4: Products dimension
-- Grain: one row per product_id
-- ============================================================
CREATE TABLE IF NOT EXISTS analytics.dim_products (
    product_id          TEXT PRIMARY KEY,
    category_name_pt    TEXT,
    category_name_en    TEXT NOT NULL,
    name_length         INTEGER,
    description_length  INTEGER,
    photos_qty          INTEGER,
    weight_g            INTEGER,
    length_cm           INTEGER,
    height_cm           INTEGER,
    width_cm            INTEGER
);


-- ============================================================
-- BLOCK 5: Orders fact table
-- Grain: one row per order_id
-- ============================================================
CREATE TABLE IF NOT EXISTS analytics.fact_orders (
    order_id                       TEXT PRIMARY KEY,
    customer_id                    TEXT NOT NULL REFERENCES analytics.dim_customers(customer_id),
    order_status                   TEXT NOT NULL,

    purchased_at                   TIMESTAMP NOT NULL,
    approved_at                    TIMESTAMP,
    carrier_delivered_at           TIMESTAMP,
    delivered_at                   TIMESTAMP,
    estimated_delivery_at          TIMESTAMP,

    purchase_month                 DATE NOT NULL,
    delivery_month                 DATE,

    item_count                     INTEGER NOT NULL,
    seller_count                   INTEGER NOT NULL,
    items_value                    NUMERIC NOT NULL,
    freight_value                  NUMERIC NOT NULL,

    has_items                      BOOLEAN NOT NULL,
    is_canceled                    BOOLEAN NOT NULL,
    is_delivery_eligible           BOOLEAN NOT NULL,

    is_late                        BOOLEAN,
    is_late_calendar_day            BOOLEAN,
    delivery_days                  NUMERIC,
    estimated_delivery_days        NUMERIC,
    days_late                      NUMERIC,

    dq_approved_before_purchase    BOOLEAN NOT NULL,
    dq_carrier_before_purchase     BOOLEAN NOT NULL,
    dq_delivered_before_carrier    BOOLEAN NOT NULL,
    dq_delivered_before_purchase   BOOLEAN NOT NULL,
    dq_estimated_before_purchase   BOOLEAN NOT NULL,
    dq_delivered_status_missing_date BOOLEAN NOT NULL,
    dq_delivery_date_on_non_delivered BOOLEAN NOT NULL,
    dq_carrier_before_approval     BOOLEAN NOT NULL,
    dq_no_items                    BOOLEAN NOT NULL,
    has_dq_flag                    BOOLEAN NOT NULL
);


-- ============================================================
-- BLOCK 6: Order items fact table
-- Grain: one row per (order_id, order_item_id)
-- ============================================================
CREATE TABLE IF NOT EXISTS analytics.fact_order_items (
    order_id         TEXT NOT NULL REFERENCES analytics.fact_orders(order_id),
    order_item_id    INTEGER NOT NULL,
    product_id       TEXT NOT NULL REFERENCES analytics.dim_products(product_id),
    seller_id        TEXT NOT NULL REFERENCES analytics.dim_sellers(seller_id),
    shipping_limit_at TIMESTAMP,
    price            NUMERIC NOT NULL CHECK (price > 0),
    freight_value    NUMERIC NOT NULL CHECK (freight_value >= 0),

    PRIMARY KEY (order_id, order_item_id)
);
-- ============================================================
-- BLOCK 7: Indexes for common filters and joins
-- ============================================================

CREATE INDEX IF NOT EXISTS ix_fact_orders_customer
    ON analytics.fact_orders(customer_id);

CREATE INDEX IF NOT EXISTS ix_fact_orders_purchase_mon
    ON analytics.fact_orders(purchase_month);

CREATE INDEX IF NOT EXISTS ix_fact_orders_delivery_mon
    ON analytics.fact_orders(delivery_month)
    WHERE is_delivery_eligible;

CREATE INDEX IF NOT EXISTS ix_fact_items_seller
    ON analytics.fact_order_items(seller_id);

CREATE INDEX IF NOT EXISTS ix_fact_items_product
    ON analytics.fact_order_items(product_id);
SELECT pg_get_viewdef(
    'analytics.v_order_sellers'::regclass,
    true
);
-- BLOCK 8: Seller-level order view
-- Grain: one row per (order_id, seller_id)

CREATE OR REPLACE VIEW analytics.v_order_sellers AS
SELECT
    s.order_id,
    s.seller_id,
    s.item_count,
    s.items_value,
    o.seller_count,
    o.purchased_at,
    o.delivered_at,
    o.purchase_month,
    o.delivery_month,
    o.is_delivery_eligible,
    o.is_late,
    o.is_late_calendar_day,
    o.delivery_days
FROM (
    SELECT
        order_id,
        seller_id,
        COUNT(*) AS item_count,
        SUM(price) AS items_value
    FROM analytics.fact_order_items
    GROUP BY order_id, seller_id
) s
JOIN analytics.fact_orders o USING (order_id);
SELECT pg_get_viewdef(
    'analytics.v_order_categories'::regclass,
    true
);

-- ============================================================
-- BLOCK 9: Category-level order view
-- Grain: one row per (order_id, category_name_en)
-- ============================================================

CREATE OR REPLACE VIEW analytics.v_order_categories AS
SELECT
    c.order_id,
    c.category_name_en,
    c.item_count,
    o.is_delivery_eligible,
    o.is_late,
    o.is_late_calendar_day,
    o.delivery_days,
    o.delivery_month
FROM (
    SELECT
        i.order_id,
        p.category_name_en,
        COUNT(*) AS item_count
    FROM analytics.fact_order_items i
    JOIN analytics.dim_products p USING (product_id)
    GROUP BY i.order_id, p.category_name_en
) c
JOIN analytics.fact_orders o USING (order_id);

SELECT pg_get_viewdef(
    'analytics.v_dq_flagged_orders'::regclass,
    true
);

-- ============================================================
-- BLOCK 10: Data-quality flagged orders
-- One row per order with at least one DQ flag
-- ============================================================

CREATE OR REPLACE VIEW analytics.v_dq_flagged_orders AS
SELECT
    order_id,
    order_status,
    purchased_at,
    approved_at,
    carrier_delivered_at,
    delivered_at,
    estimated_delivery_at,

    ARRAY_TO_STRING(
        ARRAY_REMOVE(
            ARRAY[
                CASE WHEN dq_approved_before_purchase
                    THEN 'approved_before_purchase' END,

                CASE WHEN dq_carrier_before_purchase
                    THEN 'carrier_before_purchase' END,

                CASE WHEN dq_delivered_before_carrier
                    THEN 'delivered_before_carrier' END,

                CASE WHEN dq_delivered_before_purchase
                    THEN 'delivered_before_purchase' END,

                CASE WHEN dq_estimated_before_purchase
                    THEN 'estimated_before_purchase' END,

                CASE WHEN dq_delivered_status_missing_date
                    THEN 'delivered_status_missing_date' END,

                CASE WHEN dq_delivery_date_on_non_delivered
                    THEN 'delivery_date_on_non_delivered' END
            ]::TEXT[],
            NULL
        ),
        ', '
    ) AS reasons

FROM analytics.fact_orders
WHERE has_dq_flag;
