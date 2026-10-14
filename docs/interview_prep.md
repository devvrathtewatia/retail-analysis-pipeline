# Interview preparation

Everything here is true of this repository. If you change the project, change this file. Rehearse in your own words: you must be able to explain every line.

## 60-second version

"I built a small data pipeline on the public Olist Brazilian e-commerce dataset. It reads six CSV files, validates and cleans them in Python, loads them into PostgreSQL staging tables, and builds an analytics model with SQL: one row per order for delivery metrics, plus item, product, seller and customer tables. The load is one transaction with a full refresh, so reruns don't duplicate data and a failure rolls back to the last good state. 59 data-quality checks run on every load, and every run is recorded in an audit table. On top of that I wrote 13 SQL queries; for example, 8.1% of the 96,470 eligible orders arrived after the estimated date, and I show how that number changes to 6.8% if you compare calendar days instead of timestamps."

## 3-minute version

1. **Problem.** An online retailer's data lives in several files; the business wants to understand late deliveries, cancellations and seller performance, and needs numbers it can trust.
2. **Pipeline.** Extract (check file, columns, encoding, row counts) → transform in pandas (trim, standardise NULLs, strict date and number parsing) → `COPY` into staging → SQL transforms into analytics tables → checks → commit.
3. **Model.** Staging mirrors the source. Analytics has `fact_orders` (grain: order), `fact_order_items` (grain: order item), three dimensions, and views at (order, seller) and (order, category) grain, so that a rate is never computed from item rows with duplicated orders.
4. **Reliability.** One transaction including the DDL; TRUNCATE+COPY full refresh; CRITICAL checks (duplicate keys, orphans, row counts, metric rules) abort and roll back; WARN checks (impossible dates) flag but keep rows; an advisory lock blocks concurrent runs; audit tables record success and failure; exit code 1 on failure.
5. **Testing.** 63 tests: unit tests for parsing, integration tests on a throwaway database for the normal run, second run, missing file, missing column, bad dates, duplicates, orphans, concurrent run; and a test that recomputes the headline numbers from the raw CSVs with independent pandas code. I also broke the code on purpose five ways; four were caught immediately and the fifth exposed a gap in my test data, which I fixed.
6. **Findings and honesty.** Late rate 8.1%; monthly volatility; slowest regions; sellers with 20–30% late rates. I do not claim causes because the data can't show them. Limitations: historical public data, no timezone, in-progress orders excluded.

## What I found that I would mention

* The translation CSV has a **byte-order mark** that silently breaks the first column name unless read as `utf-8-sig`.
* The estimated delivery date is always **midnight**, which makes "late" definition-dependent: 8.11% vs 6.77%. 1,292 "late" orders arrived on the promised day.
* 203 orders have impossible/inconsistent dates; I flagged them rather than deleting them, and documented which stay in the metrics.
* Zip prefixes need **text**, not integers (leading zeros).

## Likely questions and short answers

**Primary key vs foreign key?** A primary key uniquely identifies a row (`fact_orders.order_id`; composite `(order_id, order_item_id)` for items). A foreign key says a value must exist in another table (`fact_orders.customer_id → dim_customers`). Staging deliberately has neither so it can hold bad data for the checks to report; analytics enforces them.

**What is the grain of each table, and why does it matter?** Grain = what one row represents. Computing "late orders" from the item table would count an order once per item. I aggregate to one row per order first, and use (order, seller) / (order, category) views for those breakdowns.

**Which join type did you use and why?** `LEFT JOIN` from orders to pre-aggregated items so orders with no items (775) are kept; inner joins in the views, where an order must have items to belong to a seller/category. Joining items directly to orders before aggregating would duplicate orders (I tested that mutation).

**Which indexes and why?** Primary keys create indexes. I added indexes on `customer_id`, `purchase_month`, a partial index on `delivery_month` for eligible orders, and on item `seller_id` / `product_id` because the queries filter and group on them. I didn't index staging (it is rewritten every run).

**ETL or ELT?** Mostly ELT-style: light cleaning and typing in Python, then loading raw-ish data into staging and doing the modelling in SQL inside the database.

**What is idempotency here?** Running twice gives the same state. Full refresh: TRUNCATE then COPY then rebuild. Verified by equal row counts, key counts, and an md5 fingerprint of `fact_orders` before and after the second run.

**What if the load fails halfway?** Everything runs in a single transaction, so PostgreSQL rolls back; previous data remains. Even a failed first run leaves no tables. The failure is recorded in `audit.pipeline_runs` over a separate connection, logged, and returns exit code 1.

**Why truncate rather than upsert?** The dataset is small and static; a full refresh is simpler and easy to reason about. For larger or incremental data I'd use staging + `INSERT ... ON CONFLICT` upserts keyed on primary keys.

**How do you decide what to delete?** I don't delete. Rows are flagged with `dq_*` columns and excluded from specific metrics by documented eligibility rules, so the impact is visible.

**Why a CRITICAL / WARN split?** Duplicate keys or orphans make results wrong, so they must stop the run. Impossible timestamps exist in the real data and are worth reporting, but stopping the pipeline on them would make it unusable.

**How do you know the numbers are right?** Hand-computed expectations on a 7-order dataset, plus an independent pandas recomputation on the real data, plus mutation tests that prove tests fail when the logic is wrong.

**What would you do next?** dbt tests for the same checks, incremental upserts, a scheduler (cron/Airflow) only if the data became recurring, a small dashboard, and statistical testing before ranking sellers. I deliberately did not add them in v1.

## Resume bullet (measured facts only)

> Built a Python–PostgreSQL ETL pipeline loading 347,649 records from six e-commerce CSVs (public Olist dataset), modelling order-level and item-level data into staging and analytics layers with 59 automated data-quality checks and a transactional, rerun-safe full refresh; wrote 13 SQL analyses of delivery performance, cancellations and seller metrics.

State plainly that it is a portfolio project on public data. Do **not** add performance gains, savings, cloud deployment or record counts you did not measure.
