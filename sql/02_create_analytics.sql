-- ANALYTICS: analysis-ready model built from staging by 03_transform.sql.
--
-- TABLE GRAINS (one row per ...)
--   dim_customers      customer_id      (an order-level customer key; one real person can have
--                                        several customer_ids, see customer_unique_id)
--   dim_sellers        seller_id
--   dim_products       product_id
--   fact_orders        order_id         <- the table for ALL order-level delivery metrics
--   fact_order_items   (order_id, order_item_id)
--
-- VIEWS (derived, never stored)
--   v_order_sellers     (order_id, seller_id)     for seller metrics, avoids double counting
--   v_order_categories  (order_id, category)      for category metrics, avoids double counting
--   v_dq_flagged_orders order_id with >= 1 impossible/inconsistent-data flag
--
-- DELIVERY-METRIC ELIGIBILITY (fact_orders.is_delivery_eligible)
--   order_status = 'delivered'
--   AND delivered_at, estimated_delivery_at are both present
--   AND delivered_at >= purchased_at   (excludes impossible sequences)
--   is_late / is_late_calendar_day / delivery_days / days_late are NULL (not applicable)
--   for non-eligible orders, so AVG()/FILTER over all rows can never mix in ineligible ones.

CREATE SCHEMA IF NOT EXISTS analytics;

CREATE TABLE IF NOT EXISTS analytics.dim_customers (
    customer_id         text PRIMARY KEY,
    customer_unique_id  text NOT NULL,
    zip_code_prefix     text,
    city                text,
    state               text
);

CREATE TABLE IF NOT EXISTS analytics.dim_sellers (
    seller_id        text PRIMARY KEY,
    zip_code_prefix  text,
    city             text,
    state            text
);

CREATE TABLE IF NOT EXISTS analytics.dim_products (
    product_id          text PRIMARY KEY,
    category_name_pt    text,                 -- as in source; NULL if source has none
    category_name_en    text NOT NULL,        -- English; falls back to Portuguese, then 'unknown'
    name_length         integer,              -- source column: product_name_lenght
    description_length  integer,              -- source column: product_description_lenght
    photos_qty          integer,
    weight_g            integer,
    length_cm           integer,
    height_cm           integer,
    width_cm            integer
);

CREATE TABLE IF NOT EXISTS analytics.fact_orders (
    order_id                text PRIMARY KEY,
    customer_id             text      NOT NULL REFERENCES analytics.dim_customers (customer_id),
    order_status            text      NOT NULL,
    purchased_at            timestamp NOT NULL,
    approved_at             timestamp,
    carrier_delivered_at    timestamp,
    delivered_at            timestamp,
    estimated_delivery_at   timestamp,
    purchase_month          date      NOT NULL,   -- first day of month of purchase
    delivery_month          date,                 -- first day of month of delivery

    item_count              integer        NOT NULL,   -- 0 for orders without item rows
    seller_count            integer        NOT NULL,
    items_value             numeric(12, 2) NOT NULL,   -- sum of item prices
    freight_value           numeric(12, 2) NOT NULL,   -- sum of item freight

    has_items               boolean NOT NULL,
    is_canceled             boolean NOT NULL,          -- order_status = 'canceled'
    is_delivery_eligible    boolean NOT NULL,

    -- NULL = not applicable (order is not delivery-eligible)
    is_late                 boolean,   -- delivered_at > estimated_delivery_at (full timestamps)
    is_late_calendar_day    boolean,   -- delivered date > estimated date (ignores time of day)
    delivery_days           numeric(10, 4),  -- purchase -> customer delivery, in days
    estimated_delivery_days numeric(10, 4),  -- purchase -> estimated delivery, in days
    days_late               numeric(10, 4),  -- delivered - estimated; negative = early

    -- Data-quality flags (informational; flagged rows are kept, never deleted)
    dq_approved_before_purchase        boolean NOT NULL,
    dq_carrier_before_purchase         boolean NOT NULL,
    dq_delivered_before_carrier        boolean NOT NULL,
    dq_delivered_before_purchase       boolean NOT NULL,
    dq_estimated_before_purchase       boolean NOT NULL,
    dq_delivered_status_missing_date   boolean NOT NULL,  -- status 'delivered' but no delivery date
    dq_delivery_date_on_non_delivered  boolean NOT NULL,  -- delivery date but status not 'delivered'
    dq_carrier_before_approval         boolean NOT NULL,  -- suspicious, plausible; not in has_dq_flag
    dq_no_items                        boolean NOT NULL,  -- legitimate for canceled; not in has_dq_flag
    has_dq_flag                        boolean NOT NULL   -- any of the first seven dq_ flags
);

CREATE TABLE IF NOT EXISTS analytics.fact_order_items (
    order_id               text           NOT NULL REFERENCES analytics.fact_orders (order_id),
    order_item_id          integer        NOT NULL,
    product_id             text           NOT NULL REFERENCES analytics.dim_products (product_id),
    seller_id              text           NOT NULL REFERENCES analytics.dim_sellers (seller_id),
    shipping_limit_at      timestamp,
    price                  numeric(12, 2) NOT NULL CHECK (price > 0),
    freight_value          numeric(12, 2) NOT NULL CHECK (freight_value >= 0),
    PRIMARY KEY (order_id, order_item_id)
);

-- Indexes: only where the analysis queries filter/join/group on them.
CREATE INDEX IF NOT EXISTS ix_fact_orders_customer      ON analytics.fact_orders (customer_id);
CREATE INDEX IF NOT EXISTS ix_fact_orders_purchase_mon  ON analytics.fact_orders (purchase_month);
CREATE INDEX IF NOT EXISTS ix_fact_orders_delivery_mon  ON analytics.fact_orders (delivery_month)
    WHERE is_delivery_eligible;
CREATE INDEX IF NOT EXISTS ix_fact_items_seller         ON analytics.fact_order_items (seller_id);
CREATE INDEX IF NOT EXISTS ix_fact_items_product        ON analytics.fact_order_items (product_id);

-- One row per (order, seller). An order delivered by two sellers appears twice, with the
-- same order-level delivery outcome. Use seller_count = 1 for unambiguous attribution.
CREATE OR REPLACE VIEW analytics.v_order_sellers AS
SELECT s.order_id,
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
    SELECT order_id, seller_id, COUNT(*) AS item_count, SUM(price) AS items_value
    FROM analytics.fact_order_items
    GROUP BY order_id, seller_id
) s
JOIN analytics.fact_orders o USING (order_id);

-- One row per (order, product category). An order with items from two categories appears
-- once under each, so category rates read "share of orders CONTAINING this category".
CREATE OR REPLACE VIEW analytics.v_order_categories AS
SELECT c.order_id,
       c.category_name_en,
       c.item_count,
       o.is_delivery_eligible,
       o.is_late,
       o.is_late_calendar_day,
       o.delivery_days,
       o.delivery_month
FROM (
    SELECT i.order_id, p.category_name_en, COUNT(*) AS item_count
    FROM analytics.fact_order_items i
    JOIN analytics.dim_products p USING (product_id)
    GROUP BY i.order_id, p.category_name_en
) c
JOIN analytics.fact_orders o USING (order_id);

-- Orders with at least one impossible/inconsistent flag, with a readable reason list.
CREATE OR REPLACE VIEW analytics.v_dq_flagged_orders AS
SELECT order_id,
       order_status,
       purchased_at,
       approved_at,
       carrier_delivered_at,
       delivered_at,
       estimated_delivery_at,
       array_to_string(array_remove(ARRAY[
           CASE WHEN dq_approved_before_purchase       THEN 'approved_before_purchase' END,
           CASE WHEN dq_carrier_before_purchase        THEN 'carrier_before_purchase' END,
           CASE WHEN dq_delivered_before_carrier       THEN 'delivered_before_carrier' END,
           CASE WHEN dq_delivered_before_purchase      THEN 'delivered_before_purchase' END,
           CASE WHEN dq_estimated_before_purchase      THEN 'estimated_before_purchase' END,
           CASE WHEN dq_delivered_status_missing_date  THEN 'delivered_status_missing_date' END,
           CASE WHEN dq_delivery_date_on_non_delivered THEN 'delivery_date_on_non_delivered' END
       ], NULL), ', ') AS reasons
FROM analytics.fact_orders
WHERE has_dq_flag;
