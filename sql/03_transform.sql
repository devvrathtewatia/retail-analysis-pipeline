-- Rebuild the analytics tables from staging (full refresh).
-- Runs inside the pipeline's single transaction: either everything below is applied or
-- nothing is. TRUNCATE of all five tables in ONE statement is required because of the
-- foreign keys between them.

TRUNCATE analytics.fact_order_items,
         analytics.fact_orders,
         analytics.dim_products,
         analytics.dim_sellers,
         analytics.dim_customers;

-- ---------------------------------------------------------------- dimensions
INSERT INTO analytics.dim_customers (customer_id, customer_unique_id, zip_code_prefix, city, state)
SELECT customer_id, customer_unique_id, customer_zip_code_prefix, customer_city, customer_state
FROM staging.customers;

INSERT INTO analytics.dim_sellers (seller_id, zip_code_prefix, city, state)
SELECT seller_id, seller_zip_code_prefix, seller_city, seller_state
FROM staging.sellers;

-- English category: translation if available, else the Portuguese source name, else 'unknown'.
-- (Translation is unique per category, enforced by a staging check, so this join cannot
--  multiply product rows.)
INSERT INTO analytics.dim_products (
    product_id, category_name_pt, category_name_en, name_length, description_length,
    photos_qty, weight_g, length_cm, height_cm, width_cm
)
SELECT p.product_id,
       p.product_category_name,
       COALESCE(t.product_category_name_english, p.product_category_name, 'unknown'),
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

-- ---------------------------------------------------------------- fact_orders (grain: order)
-- Items are pre-aggregated to ONE row per order before joining, so the join cannot
-- duplicate orders.
WITH item_agg AS (
    SELECT order_id,
           COUNT(*)                   AS item_count,
           COUNT(DISTINCT seller_id)  AS seller_count,
           SUM(price)                 AS items_value,
           SUM(freight_value)         AS freight_value
    FROM staging.order_items
    GROUP BY order_id
),
base AS (
    SELECT o.order_id,
           o.customer_id,
           o.order_status,
           o.order_purchase_timestamp       AS purchased_at,
           o.order_approved_at              AS approved_at,
           o.order_delivered_carrier_date   AS carrier_delivered_at,
           o.order_delivered_customer_date  AS delivered_at,
           o.order_estimated_delivery_date  AS estimated_delivery_at,
           COALESCE(a.item_count, 0)        AS item_count,
           COALESCE(a.seller_count, 0)      AS seller_count,
           COALESCE(a.items_value, 0)       AS items_value,
           COALESCE(a.freight_value, 0)     AS freight_value,
           (    o.order_status = 'delivered'
            AND o.order_delivered_customer_date IS NOT NULL
            AND o.order_estimated_delivery_date IS NOT NULL
            AND o.order_delivered_customer_date >= o.order_purchase_timestamp
           )                                AS is_eligible
    FROM staging.orders o
    LEFT JOIN item_agg a ON a.order_id = o.order_id
)
INSERT INTO analytics.fact_orders (
    order_id, customer_id, order_status, purchased_at, approved_at, carrier_delivered_at,
    delivered_at, estimated_delivery_at, purchase_month, delivery_month,
    item_count, seller_count, items_value, freight_value,
    has_items, is_canceled, is_delivery_eligible,
    is_late, is_late_calendar_day, delivery_days, estimated_delivery_days, days_late,
    dq_approved_before_purchase, dq_carrier_before_purchase, dq_delivered_before_carrier,
    dq_delivered_before_purchase, dq_estimated_before_purchase,
    dq_delivered_status_missing_date, dq_delivery_date_on_non_delivered,
    dq_carrier_before_approval, dq_no_items, has_dq_flag
)
SELECT
    order_id, customer_id, order_status, purchased_at, approved_at, carrier_delivered_at,
    delivered_at, estimated_delivery_at,
    date_trunc('month', purchased_at)::date,
    date_trunc('month', delivered_at)::date,
    item_count, seller_count, items_value, freight_value,
    item_count > 0,
    order_status = 'canceled',
    is_eligible,
    -- delivery metrics: only for eligible orders, NULL otherwise
    CASE WHEN is_eligible THEN delivered_at > estimated_delivery_at END,
    CASE WHEN is_eligible THEN delivered_at::date > estimated_delivery_at::date END,
    CASE WHEN is_eligible THEN ROUND(EXTRACT(EPOCH FROM (delivered_at - purchased_at)) / 86400.0, 4) END,
    CASE WHEN is_eligible THEN ROUND(EXTRACT(EPOCH FROM (estimated_delivery_at - purchased_at)) / 86400.0, 4) END,
    CASE WHEN is_eligible THEN ROUND(EXTRACT(EPOCH FROM (delivered_at - estimated_delivery_at)) / 86400.0, 4) END,
    -- data-quality flags (a comparison involving NULL is NULL, so COALESCE to false)
    COALESCE(approved_at < purchased_at, false),
    COALESCE(carrier_delivered_at < purchased_at, false),
    COALESCE(delivered_at < carrier_delivered_at, false),
    COALESCE(delivered_at < purchased_at, false),
    COALESCE(estimated_delivery_at < purchased_at, false),
    (order_status = 'delivered' AND delivered_at IS NULL),
    (order_status <> 'delivered' AND delivered_at IS NOT NULL),
    COALESCE(carrier_delivered_at < approved_at, false),
    item_count = 0,
    (   COALESCE(approved_at < purchased_at, false)
     OR COALESCE(carrier_delivered_at < purchased_at, false)
     OR COALESCE(delivered_at < carrier_delivered_at, false)
     OR COALESCE(delivered_at < purchased_at, false)
     OR COALESCE(estimated_delivery_at < purchased_at, false)
     OR (order_status = 'delivered' AND delivered_at IS NULL)
     OR (order_status <> 'delivered' AND delivered_at IS NOT NULL)
    )
FROM base;

-- ---------------------------------------------------------------- fact_order_items (grain: order item)
INSERT INTO analytics.fact_order_items (
    order_id, order_item_id, product_id, seller_id, shipping_limit_at, price, freight_value
)
SELECT order_id, order_item_id, product_id, seller_id, shipping_limit_date, price, freight_value
FROM staging.order_items;
