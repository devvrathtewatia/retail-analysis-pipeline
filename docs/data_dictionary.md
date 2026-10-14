# Data dictionary

Source: *Brazilian E-Commerce Public Dataset by Olist* (Kaggle, `olistbr/brazilian-ecommerce`), 2016-09 to 2018-10.
Six of its nine files are used. Not used: geolocation, order payments, order reviews (the handbook says to add them only if useful; the business questions do not need them).

All numbers below were measured on the files in `data/raw/` (EDA in Session 2, re-verified by the pipeline's own checks).

## 1. Source files

| File | Rows | Grain (one row per) | Primary key |
| --- | ---: | --- | --- |
| `olist_orders_dataset.csv` | 99,441 | order | `order_id` |
| `olist_order_items_dataset.csv` | 112,650 | item within an order | `(order_id, order_item_id)` |
| `olist_products_dataset.csv` | 32,951 | product | `product_id` |
| `olist_customers_dataset.csv` | 99,441 | customer **per order** | `customer_id` |
| `olist_sellers_dataset.csv` | 3,095 | seller | `seller_id` |
| `product_category_name_translation.csv` | 71 | Portuguese category name | `product_category_name` |

### Relationships

```
customers 1 ──< orders 1 ──< order_items >── 1 products
                                   │
                                   └── >── 1 sellers
products >── 0..1 category translation (by Portuguese category name)
```

* One order has **one or more** items (9,803 orders have 2+ items) — except 775 orders that have **no** item rows (603 `unavailable`, 164 `canceled`, 5 `created`, 2 `invoiced`, 1 `shipped`).
* One order can involve **several sellers** (1,278 orders). The order has a single delivery timestamp, so lateness cannot be assigned to one seller in those orders.
* `customer_id` is generated per order; the same real person can appear under several `customer_id`s. `customer_unique_id` identifies the person (99,441 `customer_id`s ↔ 96,096 `customer_unique_id`s).

## 2. Columns and what we learned about them

### orders
| Column | Meaning | Notes found in EDA |
| --- | --- | --- |
| `order_id` | order key | unique, never null |
| `customer_id` | → customers | all values exist in customers |
| `order_status` | lifecycle status | 8 values: delivered 96,478 · shipped 1,107 · canceled 625 · unavailable 609 · invoiced 314 · processing 301 · created 5 · approved 2 |
| `order_purchase_timestamp` | when the customer ordered | never null; range 2016-09-04 → 2018-10-17; **no purchases in 2016-11, one in 2016-12** |
| `order_approved_at` | payment approved | 160 empty (141 canceled, 14 delivered, 5 created) |
| `order_delivered_carrier_date` | handed to carrier | 1,783 empty |
| `order_delivered_customer_date` | received by customer | 2,965 empty: all expected (not yet delivered / canceled) **except 8 `delivered` orders** |
| `order_estimated_delivery_date` | promised date | never null; **always exactly 00:00:00** (a date, not a moment) — this drives the two late-delivery definitions |

Timestamps carry **no timezone**; they are stored as `timestamp` (without time zone).

### order_items
| Column | Notes |
| --- | --- |
| `order_item_id` | 1…21, a sequence number *within* the order |
| `shipping_limit_date` | seller's deadline to hand over to the carrier |
| `price` | 0.85 … 6,735.00; always > 0 |
| `freight_value` | 0.00 … 409.68; **383 items have 0 freight** (kept; plausible free shipping) |

### products
| Column | Notes |
| --- | --- |
| `product_category_name` | Portuguese; **610 products have none** (they also lack name/description/photo fields) |
| `product_name_lenght`, `product_description_lenght` | *sic* — the source misspells "length"; staging keeps the source spelling, analytics renames to `name_length` / `description_length` |
| `product_weight_g` | 2 empty, **4 products weigh 0 g** (kept, suspicious) |
| dimensions | 2 empty |

Two categories used by products are **missing from the translation file**: `pc_gamer` and `portateis_cozinha_e_preparadores_de_alimentos`.

### customers / sellers
`*_zip_code_prefix` is **text** — values like `09790` have a leading zero that an integer type would destroy. All 5 digits. City names are all lower-case; customer cities are pure ASCII, while 3 seller city values contain non-ASCII characters (loaded as-is, UTF-8). 27 state codes for customers.

### Encoding quirk
`product_category_name_translation.csv` begins with a **UTF-8 byte-order mark**. Read with plain `utf-8`, the first column is named `\ufeffproduct_category_name` and silently fails to match. The extractor reads with `utf-8-sig`, and a test proves it.

## 3. Staging layer (`staging.*`)

One table per CSV, same column names and order as the source, typed:
`text` for IDs/zip codes/categories, `timestamp`, `integer`, `numeric(12,2)`. Plus `_loaded_at timestamptz` (when the row was loaded).
No keys or foreign keys — staging is a landing zone that must be able to hold bad data so the checks can report it.

## 4. Analytics layer (`analytics.*`)

| Object | Grain | Rows | Purpose |
| --- | --- | ---: | --- |
| `dim_customers` | `customer_id` | 99,441 | location of the ordering customer |
| `dim_sellers` | `seller_id` | 3,095 | seller location |
| `dim_products` | `product_id` | 32,951 | category (English with fallbacks) and physical attributes |
| `fact_orders` | **`order_id`** | 99,441 | **all order-level metrics** — delivery, cancellation, flags |
| `fact_order_items` | `(order_id, order_item_id)` | 112,650 | item price/freight/seller/product |
| `v_order_sellers` (view) | `(order_id, seller_id)` | — | seller metrics without double counting |
| `v_order_categories` (view) | `(order_id, category)` | — | category metrics without double counting |
| `v_dq_flagged_orders` (view) | `order_id` with ≥1 flag | 203 | readable list of problem orders |

### `fact_orders` derived columns

| Column | Definition |
| --- | --- |
| `is_delivery_eligible` | `order_status='delivered'` **and** delivery + estimated timestamps present **and** `delivered_at >= purchased_at` |
| `is_late` | eligible **and** `delivered_at > estimated_delivery_at` (full timestamps; the handbook's reference rule) |
| `is_late_calendar_day` | eligible **and** delivered *date* > estimated *date* (sensitivity variant) |
| `delivery_days` | `delivered_at − purchased_at` in days |
| `estimated_delivery_days` | `estimated_delivery_at − purchased_at` in days |
| `days_late` | `delivered_at − estimated_delivery_at` in days (negative = early) |
| `is_late`, `is_late_calendar_day`, `delivery_days`, `estimated_delivery_days`, `days_late` | **NULL when the order is not eligible** (not applicable ≠ "not late") |
| `is_canceled` | `order_status = 'canceled'` |
| `item_count`, `seller_count`, `items_value`, `freight_value` | order totals from the items (0 when no items) |
| `purchase_month`, `delivery_month` | first day of the month |
| `dq_*` flags / `has_dq_flag` | see below |

### Data-quality flags (rows are flagged, never deleted)

| Flag | Meaning | Count | In `has_dq_flag`? |
| --- | --- | ---: | :-: |
| `dq_approved_before_purchase` | approved before purchased | 0 | yes |
| `dq_carrier_before_purchase` | handed to carrier before purchase | 166 | yes |
| `dq_delivered_before_carrier` | customer delivery before carrier pick-up | 23 | yes |
| `dq_delivered_before_purchase` | delivered before purchase | 0 | yes |
| `dq_estimated_before_purchase` | estimate before purchase | 0 | yes |
| `dq_delivered_status_missing_date` | status `delivered`, no delivery date | 8 | yes |
| `dq_delivery_date_on_non_delivered` | delivery date but status ≠ `delivered` | 6 (all `canceled`) | yes |
| `dq_carrier_before_approval` | carrier pick-up before payment approval | 1,359 | no — suspicious but plausible |
| `dq_no_items` | order has no item rows | 775 | no — normal for canceled/unavailable |
| `has_dq_flag` | any of the first seven | **203** distinct orders | — |

Impact on metrics: the 8 + 6 inconsistent-status orders are **excluded** from delivery metrics by the eligibility rule. The 166 `carrier_before_purchase` and 23 `delivered_before_carrier` orders (189 distinct; 188 of them eligible) **stay eligible**, because late/delivery-time use only the purchase, delivery and estimate timestamps, which are in valid order for them. Their carrier timestamp is simply unreliable.
