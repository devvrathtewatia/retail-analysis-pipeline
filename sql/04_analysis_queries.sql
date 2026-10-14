-- ANALYSIS QUERIES
-- Run them all with:  python -m src.run_analysis      (results -> docs/sample_query_results.md)
-- Each block starts with "-- name:" and "-- question:" lines and ends with a semicolon.
--
-- METRIC DEFINITIONS (full text in README)
--   eligible order  = status 'delivered' with delivery + estimated timestamps, delivered_at >= purchased_at
--   late order      = eligible AND delivered_at > estimated_delivery_at   (full timestamp comparison)
--   late (calendar) = eligible AND delivered date > estimated date        (sensitivity variant)
--   late rate       = late orders / eligible orders, counted at ORDER grain (analytics.fact_orders),
--                     or at (order, seller)/(order, category) grain via the views - never from raw item rows.
--   low_volume      = fewer than 100 eligible (or total) orders in the period; do not over-read these rows.

-- name: q01_overall_late_rate
-- question: What percentage of eligible orders arrived after the estimated delivery date?
SELECT COUNT(*)                                                          AS eligible_orders,
       COUNT(*) FILTER (WHERE is_late)                                   AS late_orders,
       ROUND(100.0 * COUNT(*) FILTER (WHERE is_late) / NULLIF(COUNT(*), 0), 2)
                                                                         AS late_pct_timestamp,
       COUNT(*) FILTER (WHERE is_late_calendar_day)                      AS late_orders_calendar_day,
       ROUND(100.0 * COUNT(*) FILTER (WHERE is_late_calendar_day) / NULLIF(COUNT(*), 0), 2)
                                                                         AS late_pct_calendar_day
FROM analytics.fact_orders
WHERE is_delivery_eligible;

-- name: q02_late_rate_by_delivery_month
-- question: How does the late-delivery rate change month by month (by month of delivery)?
WITH monthly AS (
    SELECT delivery_month,
           COUNT(*)                        AS delivered_orders,
           COUNT(*) FILTER (WHERE is_late) AS late_orders,
           ROUND(100.0 * COUNT(*) FILTER (WHERE is_late) / NULLIF(COUNT(*), 0), 2) AS late_pct
    FROM analytics.fact_orders
    WHERE is_delivery_eligible
    GROUP BY delivery_month
)
SELECT to_char(delivery_month, 'YYYY-MM')                                  AS delivery_month,
       delivered_orders,
       late_orders,
       late_pct,
       ROUND(late_pct - LAG(late_pct) OVER (ORDER BY delivery_month), 2)   AS change_vs_prev_month_pp,
       (delivered_orders < 100)                                            AS low_volume
FROM monthly
ORDER BY delivery_month;

-- name: q03_late_rate_by_seller
-- question: Which sellers have the highest late-delivery rates? (single-seller orders only, >= 50 eligible orders)
-- note: orders shipped by several sellers are excluded because one delivery date cannot be blamed on one seller.
SELECT seller_id,
       COUNT(*)                        AS eligible_orders,
       COUNT(*) FILTER (WHERE is_late) AS late_orders,
       ROUND(100.0 * COUNT(*) FILTER (WHERE is_late) / COUNT(*), 2) AS late_pct,
       d.state                         AS seller_state
FROM analytics.v_order_sellers
JOIN analytics.dim_sellers d USING (seller_id)
WHERE is_delivery_eligible
  AND seller_count = 1
GROUP BY seller_id, d.state
HAVING COUNT(*) >= 50
ORDER BY late_pct DESC, eligible_orders DESC, seller_id
LIMIT 15;

-- name: q04_late_rate_by_category
-- question: Which product categories have the highest late-delivery rates? (>= 200 eligible orders)
-- note: an order containing items of two categories is counted once in each category.
SELECT category_name_en,
       COUNT(*)                        AS eligible_orders,
       COUNT(*) FILTER (WHERE is_late) AS late_orders,
       ROUND(100.0 * COUNT(*) FILTER (WHERE is_late) / COUNT(*), 2) AS late_pct
FROM analytics.v_order_categories
WHERE is_delivery_eligible
GROUP BY category_name_en
HAVING COUNT(*) >= 200
ORDER BY late_pct DESC, eligible_orders DESC, category_name_en
LIMIT 15;

-- name: q05_delivery_time_summary
-- question: How long does delivery take from purchase to customer delivery (days)?
SELECT COUNT(*)                                                                    AS eligible_orders,
       ROUND(AVG(delivery_days), 2)                                                AS avg_days,
       ROUND((percentile_cont(0.5)  WITHIN GROUP (ORDER BY delivery_days))::numeric, 2) AS median_days,
       ROUND((percentile_cont(0.9)  WITHIN GROUP (ORDER BY delivery_days))::numeric, 2) AS p90_days,
       ROUND((percentile_cont(0.95) WITHIN GROUP (ORDER BY delivery_days))::numeric, 2) AS p95_days,
       ROUND(MAX(delivery_days), 1)                                                AS max_days,
       ROUND(AVG(estimated_delivery_days), 2)                                      AS avg_estimated_days
FROM analytics.fact_orders
WHERE is_delivery_eligible;

-- name: q06_delivery_time_by_customer_state
-- question: How do delivery time and late rate differ by customer state? (>= 300 eligible orders)
SELECT c.state                                       AS customer_state,
       COUNT(*)                                      AS eligible_orders,
       ROUND(AVG(o.delivery_days), 2)                AS avg_delivery_days,
       ROUND(AVG(o.estimated_delivery_days), 2)      AS avg_estimated_days,
       ROUND(100.0 * COUNT(*) FILTER (WHERE o.is_late) / COUNT(*), 2) AS late_pct
FROM analytics.fact_orders o
JOIN analytics.dim_customers c USING (customer_id)
WHERE o.is_delivery_eligible
GROUP BY c.state
HAVING COUNT(*) >= 300
ORDER BY avg_delivery_days DESC, customer_state;

-- name: q07_cancellation_rate_by_purchase_month
-- question: What is the cancellation rate over time (by month of purchase, all orders)?
SELECT to_char(purchase_month, 'YYYY-MM')                              AS purchase_month,
       COUNT(*)                                                        AS orders,
       COUNT(*) FILTER (WHERE is_canceled)                             AS canceled_orders,
       ROUND(100.0 * COUNT(*) FILTER (WHERE is_canceled) / COUNT(*), 2) AS canceled_pct,
       (COUNT(*) < 100)                                                AS low_volume
FROM analytics.fact_orders
GROUP BY purchase_month
ORDER BY purchase_month;

-- name: q08_order_status_distribution
-- question: How are orders distributed across statuses?
SELECT order_status,
       COUNT(*)                                          AS orders,
       ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 2) AS pct_of_orders
FROM analytics.fact_orders
GROUP BY order_status
ORDER BY orders DESC, order_status;

-- name: q09_how_late_are_late_orders
-- question: When orders are late (full-timestamp rule), how many calendar days late are they?
-- note: the estimate is always midnight, so "0 days" = delivered later on the estimated date itself.
WITH late AS (
    SELECT (delivered_at::date - estimated_delivery_at::date) AS calendar_days_late
    FROM analytics.fact_orders
    WHERE is_late
)
SELECT CASE
           WHEN calendar_days_late = 0  THEN '1: 0 days (arrived on the estimated date, after 00:00)'
           WHEN calendar_days_late <= 3  THEN '2: 1-3 calendar days late'
           WHEN calendar_days_late <= 7  THEN '3: 4-7 calendar days late'
           WHEN calendar_days_late <= 14 THEN '4: 8-14 calendar days late'
           ELSE                               '5: more than 14 calendar days late'
       END                                                  AS lateness_bucket,
       COUNT(*)                                             AS late_orders,
       ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 2)   AS pct_of_late_orders
FROM late
GROUP BY 1
ORDER BY 1;

-- name: q10_late_rate_single_vs_multi_seller
-- question: Are orders fulfilled by several sellers more likely to be late than single-seller orders?
SELECT CASE WHEN seller_count = 1 THEN 'single seller' ELSE 'multiple sellers' END AS order_type,
       COUNT(*)                                                      AS eligible_orders,
       ROUND(100.0 * COUNT(*) FILTER (WHERE is_late) / COUNT(*), 2)  AS late_pct,
       ROUND(AVG(delivery_days), 2)                                  AS avg_delivery_days
FROM analytics.fact_orders
WHERE is_delivery_eligible
  AND seller_count >= 1
GROUP BY 1
ORDER BY 1 DESC;

-- name: q11_impossible_or_inconsistent_dates
-- question: Which records have impossible or inconsistent dates? (counts by rule)
SELECT rule, orders
FROM (
    SELECT 'approved before purchase'                 AS rule, COUNT(*) FILTER (WHERE dq_approved_before_purchase)       AS orders, 1 AS ord FROM analytics.fact_orders
    UNION ALL SELECT 'carrier pick-up before purchase',          COUNT(*) FILTER (WHERE dq_carrier_before_purchase),        2 FROM analytics.fact_orders
    UNION ALL SELECT 'delivered before carrier pick-up',         COUNT(*) FILTER (WHERE dq_delivered_before_carrier),       3 FROM analytics.fact_orders
    UNION ALL SELECT 'delivered before purchase',                COUNT(*) FILTER (WHERE dq_delivered_before_purchase),      4 FROM analytics.fact_orders
    UNION ALL SELECT 'estimated delivery before purchase',       COUNT(*) FILTER (WHERE dq_estimated_before_purchase),      5 FROM analytics.fact_orders
    UNION ALL SELECT 'status delivered but no delivery date',    COUNT(*) FILTER (WHERE dq_delivered_status_missing_date),  6 FROM analytics.fact_orders
    UNION ALL SELECT 'delivery date but status not delivered',   COUNT(*) FILTER (WHERE dq_delivery_date_on_non_delivered), 7 FROM analytics.fact_orders
    UNION ALL SELECT 'carrier before approval (suspicious only)',COUNT(*) FILTER (WHERE dq_carrier_before_approval),        8 FROM analytics.fact_orders
    UNION ALL SELECT 'ANY of the first seven rules (has_dq_flag)', COUNT(*) FILTER (WHERE has_dq_flag),                     9 FROM analytics.fact_orders
) r
ORDER BY ord;

-- name: q12_missing_keys_and_duplicate_ids
-- question: Which records have missing keys or duplicate IDs? (source/staging layer, counts by table)
SELECT table_name, null_key_rows, duplicate_key_groups, orphan_rows
FROM (
    SELECT 'orders' AS table_name, 1 AS ord,
           (SELECT COUNT(*) FROM staging.orders WHERE order_id IS NULL OR customer_id IS NULL)             AS null_key_rows,
           (SELECT COUNT(*) FROM (SELECT 1 FROM staging.orders GROUP BY order_id HAVING COUNT(*) > 1) d)   AS duplicate_key_groups,
           (SELECT COUNT(*) FROM staging.orders o WHERE NOT EXISTS (SELECT 1 FROM staging.customers c WHERE c.customer_id = o.customer_id)) AS orphan_rows
    UNION ALL
    SELECT 'order_items', 2,
           (SELECT COUNT(*) FROM staging.order_items WHERE order_id IS NULL OR order_item_id IS NULL OR product_id IS NULL OR seller_id IS NULL),
           (SELECT COUNT(*) FROM (SELECT 1 FROM staging.order_items GROUP BY order_id, order_item_id HAVING COUNT(*) > 1) d),
           (SELECT COUNT(*) FROM staging.order_items i WHERE NOT EXISTS (SELECT 1 FROM staging.orders o WHERE o.order_id = i.order_id)
                                                         OR NOT EXISTS (SELECT 1 FROM staging.products p WHERE p.product_id = i.product_id)
                                                         OR NOT EXISTS (SELECT 1 FROM staging.sellers s WHERE s.seller_id = i.seller_id))
    UNION ALL
    SELECT 'customers', 3,
           (SELECT COUNT(*) FROM staging.customers WHERE customer_id IS NULL OR customer_unique_id IS NULL),
           (SELECT COUNT(*) FROM (SELECT 1 FROM staging.customers GROUP BY customer_id HAVING COUNT(*) > 1) d),
           0
    UNION ALL
    SELECT 'products', 4,
           (SELECT COUNT(*) FROM staging.products WHERE product_id IS NULL),
           (SELECT COUNT(*) FROM (SELECT 1 FROM staging.products GROUP BY product_id HAVING COUNT(*) > 1) d),
           0
    UNION ALL
    SELECT 'sellers', 5,
           (SELECT COUNT(*) FROM staging.sellers WHERE seller_id IS NULL),
           (SELECT COUNT(*) FROM (SELECT 1 FROM staging.sellers GROUP BY seller_id HAVING COUNT(*) > 1) d),
           0
) t
ORDER BY ord;

-- name: q13_sample_of_flagged_orders
-- question: Show examples of the flagged orders (first 10 by purchase time).
SELECT order_id, order_status, purchased_at, carrier_delivered_at, delivered_at, reasons
FROM analytics.v_dq_flagged_orders
ORDER BY purchased_at, order_id
LIMIT 10;
