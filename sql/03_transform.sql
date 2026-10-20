
-- ============================================================
-- 03_transform.sql
-- Rebuild the analytics model from staging tables.
--
-- WARNING: This performs a full refresh and empties all five
-- analytics tables before repopulating them.
-- Run inside a transaction so a failure can be rolled back.
-- ============================================================


-- BLOCK 1: Clear the five analytics tables
-- They must be truncated together because of foreign keys.

TRUNCATE TABLE
    analytics.fact_order_items,
    analytics.fact_orders,
    analytics.dim_products,
    analytics.dim_sellers,
    analytics.dim_customers;


-- BLOCK 2: Populate the dimension tables
-- One row per customer, seller, and product.

INSERT INTO analytics.dim_customers (
    customer_id,
    customer_unique_id,
    zip_code_prefix,
    city,
    state
)
SELECT
    customer_id,
    customer_unique_id,
    customer_zip_code_prefix,
    customer_city,
    customer_state
FROM staging.customers;


INSERT INTO analytics.dim_sellers (
    seller_id,
    zip_code_prefix,
    city,
    state
)
SELECT
    seller_id,
    seller_zip_code_prefix,
    seller_city,
    seller_state
FROM staging.sellers;


-- Prefer the English category translation.
-- If unavailable, use the Portuguese name, then 'unknown'.

INSERT INTO analytics.dim_products (
    product_id,
    category_name_pt,
    category_name_en,
    name_length,
    description_length,
    photos_qty,
    weight_g,
    length_cm,
    height_cm,
    width_cm
)
SELECT
    p.product_id,
    p.product_category_name,
    COALESCE(
        t.product_category_name_english,
        p.product_category_name,
        'unknown'
    ),
    p.product_name_lenght,
    p.product_description_lenght,
    p.product_photos_qty,
    p.product_weight_g,
    p.product_length_cm,
    p.product_height_cm,
    p.product_width_cm
FROM staging.products p
LEFT JOIN staging.product_category_translation t
    ON t.product_category_name = p.product_category_name;


-- BLOCK 3: Build order-level metrics
-- Grain: one row per order_id.
-- Aggregate items BEFORE joining to orders to prevent
-- multiple item rows from duplicating an order.

WITH item_agg AS (
    SELECT
        order_id,
        COUNT(*) AS item_count,
        COUNT(DISTINCT seller_id) AS seller_count,
        SUM(price::numeric) AS items_value,
        SUM(freight_value::numeric) AS freight_value
    FROM staging.order_items
    GROUP BY order_id
),
base AS (
    SELECT
        o.order_id,
        o.customer_id,
        o.order_status,

        o.order_purchase_timestamp AS purchased_at,
        o.order_approved_at AS approved_at,
        o.order_delivered_carrier_date AS carrier_delivered_at,
        o.order_delivered_customer_date AS delivered_at,
        o.order_estimated_delivery_date AS estimated_delivery_at,

        COALESCE(a.item_count, 0)::integer AS item_count,
        COALESCE(a.seller_count, 0)::integer AS seller_count,
        COALESCE(a.items_value, 0)::numeric AS items_value,
        COALESCE(a.freight_value, 0)::numeric AS freight_value,

        (
            o.order_status = 'delivered'
            AND o.order_delivered_customer_date IS NOT NULL
            AND o.order_estimated_delivery_date IS NOT NULL
            AND o.order_purchase_timestamp IS NOT NULL
            AND o.order_delivered_customer_date
                >= o.order_purchase_timestamp
        ) AS is_eligible

    FROM staging.orders o
    LEFT JOIN item_agg a
        ON a.order_id = o.order_id
)

INSERT INTO analytics.fact_orders (
    order_id,
    customer_id,
    order_status,
    purchased_at,
    approved_at,
    carrier_delivered_at,
    delivered_at,
    estimated_delivery_at,
    purchase_month,
    delivery_month,
    item_count,
    seller_count,
    items_value,
    freight_value,
    has_items,
    is_canceled,
    is_delivery_eligible,
    is_late,
    is_late_calendar_day,
    delivery_days,
    estimated_delivery_days,
    days_late,
    dq_approved_before_purchase,
    dq_carrier_before_purchase,
    dq_delivered_before_carrier,
    dq_delivered_before_purchase,
    dq_estimated_before_purchase,
    dq_delivered_status_missing_date,
    dq_delivery_date_on_non_delivered,
    dq_carrier_before_approval,
    dq_no_items,
    has_dq_flag
)
SELECT
    b.order_id,
    b.customer_id,
    b.order_status,
    b.purchased_at,
    b.approved_at,
    b.carrier_delivered_at,
    b.delivered_at,
    b.estimated_delivery_at,

    DATE_TRUNC('month', b.purchased_at)::date,
    DATE_TRUNC('month', b.delivered_at)::date,

    b.item_count,
    b.seller_count,
    b.items_value,
    b.freight_value,

    (b.item_count > 0),
    (b.order_status = 'canceled'),
    b.is_eligible,

    CASE WHEN b.is_eligible
        THEN b.delivered_at > b.estimated_delivery_at
    END,

    CASE WHEN b.is_eligible
        THEN b.delivered_at::date > b.estimated_delivery_at::date
    END,

    CASE WHEN b.is_eligible
        THEN ROUND(
            (EXTRACT(EPOCH FROM
                (b.delivered_at - b.purchased_at)
            ) / 86400)::numeric,
            4
        )
    END,

    CASE WHEN b.is_eligible
        THEN ROUND(
            (EXTRACT(EPOCH FROM
                (b.estimated_delivery_at - b.purchased_at)
            ) / 86400)::numeric,
            4
        )
    END,

    CASE WHEN b.is_eligible
        THEN ROUND(
            (EXTRACT(EPOCH FROM
                (b.delivered_at - b.estimated_delivery_at)
            ) / 86400)::numeric,
            4
        )
    END,

    COALESCE(
        b.approved_at < b.purchased_at,
        FALSE
    ),

    COALESCE(
        b.carrier_delivered_at < b.purchased_at,
        FALSE
    ),

    COALESCE(
        b.delivered_at < b.carrier_delivered_at,
        FALSE
    ),

    COALESCE(
        b.delivered_at < b.purchased_at,
        FALSE
    ),

    COALESCE(
        b.estimated_delivery_at < b.purchased_at,
        FALSE
    ),

    (
        b.order_status = 'delivered'
        AND b.delivered_at IS NULL
    ),

    (
        b.order_status <> 'delivered'
        AND b.delivered_at IS NOT NULL
    ),

    COALESCE(
        b.carrier_delivered_at < b.approved_at,
        FALSE
    ),

    (b.item_count = 0),

    (
        COALESCE(b.approved_at < b.purchased_at, FALSE)
        OR COALESCE(
            b.carrier_delivered_at < b.purchased_at,
            FALSE
        )
        OR COALESCE(
            b.delivered_at < b.carrier_delivered_at,
            FALSE
        )
        OR COALESCE(
            b.delivered_at < b.purchased_at,
            FALSE
        )
        OR COALESCE(
            b.estimated_delivery_at < b.purchased_at,
            FALSE
        )
        OR (
            b.order_status = 'delivered'
            AND b.delivered_at IS NULL
        )
        OR (
            b.order_status <> 'delivered'
            AND b.delivered_at IS NOT NULL
        )
    )

FROM base b;


-- BLOCK 4: Populate order-item facts
-- Grain: one row per (order_id, order_item_id).

INSERT INTO analytics.fact_order_items (
    order_id,
    order_item_id,
    product_id,
    seller_id,
    shipping_limit_at,
    price,
    freight_value
)
SELECT
    order_id,
    order_item_id,
    product_id,
    seller_id,
    shipping_limit_date,
    price::numeric,
    freight_value::numeric
FROM staging.order_items;
