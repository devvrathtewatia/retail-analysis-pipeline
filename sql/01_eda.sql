-- Ensure that every order_id is present
SELECT
    COUNT(*) AS total_rows,
    COUNT(order_id) AS non_null_order_ids,
    COUNT(DISTINCT order_id) AS unique_order_ids,
    COUNT(*) - COUNT(order_id) AS missing_order_ids,
    COUNT(order_id) - COUNT(DISTINCT order_id)
        AS duplicate_id_excess
FROM staging.orders;
select * from staging.orders limit 5;

-- Ensure no duplicate order_id and order_item_id
SELECT
    COUNT(*) AS total_item_rows,
    COUNT(*) FILTER (
        WHERE order_id IS NULL
    ) AS missing_order_ids,
    COUNT(*) FILTER (
        WHERE order_item_id IS NULL
    ) AS missing_item_numbers,
    COUNT(*) - COUNT(
        DISTINCT (order_id, order_item_id)
    ) AS duplicate_order_item_keys
FROM staging.order_items;

-- Testing every order is matched to a customer
SELECT
    COUNT(*) AS total_orders,
    COUNT(*) FILTER (
        WHERE o.customer_id IS NULL
    ) AS missing_customer_ids,
    COUNT(*) FILTER (
        WHERE o.customer_id IS NOT NULL
          AND c.customer_id IS NULL
    ) AS unmatched_customer_ids
FROM staging.orders o
LEFT JOIN staging.customers c
    ON o.customer_id = c.customer_id;

-- Testing the seller id of each order
-- Check 4: Seller reference integrity
SELECT
    COUNT(*) AS total_order_items,
    COUNT(*) FILTER (
        WHERE oi.seller_id IS NULL
    ) AS missing_seller_ids,
    COUNT(*) FILTER (
        WHERE oi.seller_id IS NOT NULL
          AND s.seller_id IS NULL
    ) AS unmatched_seller_ids
FROM staging.order_items oi
LEFT JOIN staging.sellers s
    ON oi.seller_id = s.seller_id;

-- Testing that every order item maps to a product in our product lists
-- Check 5: Product reference integrity
SELECT
    COUNT(*) AS total_order_items,

    COUNT(*) FILTER (
        WHERE oi.product_id IS NULL
    ) AS missing_product_ids,

    COUNT(*) FILTER (
        WHERE oi.product_id IS NOT NULL
          AND p.product_id IS NULL
    ) AS unmatched_product_ids

FROM staging.order_items oi

LEFT JOIN staging.products p
    ON oi.product_id = p.product_id;

-- Testing the timestamp if they are missing or logically possible
SELECT
    COUNT(*) AS total_orders,

    COUNT(*) FILTER (
        WHERE order_delivered_customer_date
              < order_purchase_timestamp
    ) AS delivered_before_purchase,

    COUNT(*) FILTER (
        WHERE order_estimated_delivery_date
              < order_purchase_timestamp
    ) AS estimate_before_purchase,

    COUNT(*) FILTER (
        WHERE order_delivered_customer_date
              < order_purchase_timestamp
           OR order_estimated_delivery_date
              < order_purchase_timestamp
    ) AS orders_with_impossible_dates

FROM staging.orders;

-- Check 7: Price and freight validation
select * from staging.order_items limit 5;
-- Check 7: Price and freight validation
SELECT
    COUNT(*) AS total_order_items,

    COUNT(*) FILTER (
        WHERE NULLIF(TRIM(price), '') IS NULL
    ) AS missing_prices,

    COUNT(*) FILTER (
        WHERE NULLIF(TRIM(price), '') IS NOT NULL
          AND price::numeric <= 0
    ) AS non_positive_prices,

    COUNT(*) FILTER (
        WHERE NULLIF(TRIM(freight_value), '') IS NULL
    ) AS missing_freight_values,

    COUNT(*) FILTER (
        WHERE NULLIF(TRIM(freight_value), '') IS NOT NULL
          AND freight_value::numeric < 0
    ) AS negative_freight_values

FROM staging.order_items;

-- Check 8: Missing product categories
SELECT
    COUNT(*) AS total_products,

    COUNT(*) FILTER (
        WHERE product_category_name IS NULL
           OR TRIM(product_category_name) = ''
    ) AS missing_category_names,

    ROUND(
        100.0 * COUNT(*) FILTER (
            WHERE product_category_name IS NULL
               OR TRIM(product_category_name) = ''
        ) / NULLIF(COUNT(*), 0),
        2
    ) AS missing_category_pct

FROM staging.products;

-- Check 9: Orders without order items
SELECT
    COUNT(*) AS total_orders,

    COUNT(*) FILTER (
        WHERE oi.order_id IS NULL
    ) AS orders_without_items,

    COUNT(*) FILTER (
        WHERE oi.order_id IS NOT NULL
    ) AS orders_with_items

FROM staging.orders o

LEFT JOIN (
    SELECT DISTINCT order_id
    FROM staging.order_items
) oi
    ON o.order_id = oi.order_id;

-- Check 10: Staging row-count verification
SELECT 'orders' AS table_name, COUNT(*) AS row_count
FROM staging.orders

UNION ALL

SELECT 'order_items', COUNT(*)
FROM staging.order_items

UNION ALL

SELECT 'products', COUNT(*)
FROM staging.products

UNION ALL

SELECT 'customers', COUNT(*)
FROM staging.customers

UNION ALL

SELECT 'sellers', COUNT(*)
FROM staging.sellers

UNION ALL

SELECT 'product_category_translation', COUNT(*)
FROM staging.product_category_translation

ORDER BY table_name;
