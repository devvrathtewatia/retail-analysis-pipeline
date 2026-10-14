# Retail Order & Delivery Analytics Pipeline

A Python + PostgreSQL data-engineering project built on the public **Olist Brazilian e-commerce dataset**.
It loads six CSV files, validates and cleans them, builds an analytics model in SQL, checks data quality on every run, and answers business questions about **late deliveries, delivery time, cancellations and seller/category performance**.

> Portfolio project on public, historical (2016–2018), anonymised data. Findings describe that data only.

## Business questions answered

| # | Question | Where |
| - | --- | --- |
| 1 | What % of eligible orders arrived after the estimated delivery date? | `q01` |
| 2 | How does the late rate change month by month? | `q02` |
| 3 | Which sellers have higher late rates? | `q03` |
| 4 | Which product categories have higher late rates? | `q04` |
| 5 | How long does delivery take, and does it vary by state? | `q05`, `q06` |
| 6 | Which records have missing keys, duplicate IDs or impossible dates? | `q11`–`q13`, `audit.dq_results` |
| + | Cancellation rate, how late late orders are, single- vs multi-seller orders | `q07`–`q10` |

## Headline results (from the real data)

| Metric | Value |
| --- | --- |
| Orders / items loaded | 99,441 / 112,650 |
| Eligible delivered orders | 96,470 |
| Late rate, timestamp rule (primary) | **8.11%** (7,826 orders) |
| Late rate, calendar-day rule | 6.77% (6,534 orders) |
| Delivery time (purchase → customer) | mean 12.6 d · median 10.2 d · p90 23.1 d |
| Cancellation rate | 0.63% (625 orders) |
| Orders with impossible/inconsistent dates (flagged, kept) | 203 |
| Duplicate IDs / missing keys / orphan references | 0 / 0 / 0 |

Details and cautious interpretation: [docs/analysis_findings.md](docs/analysis_findings.md). Raw query output: [docs/sample_query_results.md](docs/sample_query_results.md).

## Architecture in one picture

```
CSV files ─▶ extract ─▶ transform ─▶ staging.* ─▶ checks ─▶ analytics.* ─▶ checks ─▶ COMMIT
(read-only)  validate    clean/type   COPY         (rollback on CRITICAL failure)
                                         every run (success or failure) ─▶ audit.*
```

Diagram, ER model and design rationale: [docs/architecture.md](docs/architecture.md).

## Project layout

```
data/raw/                 the CSV files (NOT in Git, see "Get the data")
src/config.py             paths, DB settings, table specifications
src/extract.py            read + validate CSVs (BOM, columns, row counts)
src/transform.py          trim, NULL standardisation, strict date/number parsing
src/load.py               COPY into staging, run SQL files
src/quality.py            59 data-quality checks (CRITICAL / WARN)
src/run_pipeline.py       entry point (one transaction, audit trail)
src/run_analysis.py       runs the analysis queries -> Markdown
sql/00_create_audit.sql   audit tables
sql/01_create_staging.sql staging tables
sql/02_create_analytics.sql  analytics tables + views
sql/03_transform.sql      staging -> analytics
sql/04_analysis_queries.sql  13 business queries
tests/                    63 tests
docs/                     data dictionary, architecture, findings, interview prep
```

## Setup

Requires Python 3.10+ and PostgreSQL 13+ (developed on PostgreSQL 16, Python 3.12).

**1. Create the database** (as a PostgreSQL superuser; choose your own password):
```sql
CREATE USER pipeline_user WITH PASSWORD 'choose-a-password' CREATEDB;
CREATE DATABASE retail_pipeline OWNER pipeline_user;
```
`CREATEDB` lets the tests create their own throwaway database.

**2. Install Python dependencies**
```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

**3. Configure credentials**
```bash
cp .env.example .env        # then edit .env; it is git-ignored
```

**4. Get the data.** Download *Brazilian E-Commerce Public Dataset by Olist* from Kaggle (`olistbr/brazilian-ecommerce`) and put the CSVs in `data/raw/`. Only these six are needed:
`olist_orders_dataset.csv`, `olist_order_items_dataset.csv`, `olist_products_dataset.csv`, `olist_customers_dataset.csv`, `olist_sellers_dataset.csv`, `product_category_name_translation.csv`.
The data is not committed to this repository. **Check the dataset's licence on the Kaggle page before redistributing it** (I could not confirm the licence terms from within this build, so do not assume).

## Run

```bash
python -m src.run_pipeline --dry-run     # validate + clean files only, no database
python -m src.run_pipeline               # full run (about 15 seconds)
python -m src.run_analysis               # run the 13 queries, write docs/sample_query_results.md
python -m pytest                         # 63 tests (needs PostgreSQL; DB tests skip if unreachable)
```
Options: `--data-dir PATH`, `--rebuild-schema` (drop and recreate staging/analytics; needed only after editing table definitions), `--max-bad-pct N`, `-v`.
Exit code is 0 on success and 1 on any failure. Logs: console + `logs/pipeline.log`.

Inspect a run:
```sql
SELECT run_id, status, failed_stage, error_message, details->'dq_summary' FROM audit.pipeline_runs ORDER BY run_id DESC;
SELECT check_name, severity, status, violations FROM audit.dq_results WHERE run_id = (SELECT MAX(run_id) FROM audit.pipeline_runs) AND status <> 'PASS';
```

## Metric definitions

* **Eligible order**: `order_status = 'delivered'`, delivery and estimated timestamps present, `delivered ≥ purchased`.
* **Late order**: eligible and `delivered_at > estimated_delivery_at` (full timestamps; the handbook's reference rule). Because estimates are always midnight, `is_late_calendar_day` (delivered *date* > estimated *date*) is provided as a sensitivity variant.
* **Late rate** = late / eligible, always at **order grain**. Seller and category rates use per-(order, seller) and per-(order, category) views so multi-item orders are never double counted; seller rankings use single-seller orders and a minimum of 50 eligible orders (categories: 200).
* **Cancellation rate** = `canceled` orders / all orders, by purchase month. `unavailable` is a separate status and is not counted as canceled.
* **Delivery time** = `delivered − purchased` in days.
* Delivery metrics are NULL (not false) for non-eligible orders.

## Data-quality checks and failure behaviour

53 registered checks (+6 per-table row-count reconciliations = 59 per run):
* **CRITICAL** (run fails, transaction rolled back, previous data kept): duplicate primary keys, NULL required keys, orphan foreign keys, non-positive prices, source-vs-staging and staging-vs-analytics row counts, item totals reconciling to the order totals, "late ≤ eligible", metric columns NULL exactly when not eligible.
* **WARN** (recorded, rows flagged not deleted): impossible timestamp sequences, status/date inconsistencies, missing categories or translations, unusual zip formats, unknown statuses.

On the real data: 51 pass, 8 warn, 0 fail. See the table in [docs/architecture.md](docs/architecture.md) for how each kind of failure behaves.

## Design decisions and limitations

* **Full refresh, not incremental**: small static dataset; simple and provably rerun-safe. Would not scale to large or continuously arriving data.
* **Rows are flagged, never silently deleted.** Which flagged rows stay in the metrics is documented in the data dictionary.
* **Unparseable values** become NULL and are reported, but only below `MAX_BAD_VALUE_PCT` (default 1% per column); above it the run fails.
* **Out of scope on purpose** (handbook): Airflow, Spark, Kafka, Docker, cloud deployment, dashboards, ML.
* A run killed with SIGKILL could leave its audit row as `RUNNING` (the data itself is safe: the transaction rolls back).
* `TRUNCATE` takes an exclusive lock, so readers wait during the (short) load transaction.
* No timezone information in the source; no statistical significance testing in the findings.

## Documentation

* [Data dictionary](docs/data_dictionary.md)
* [Architecture](docs/architecture.md)
* [Analysis findings](docs/analysis_findings.md)
* [Interview preparation](docs/interview_prep.md)
