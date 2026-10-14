# Analysis findings

All figures come from `sql/04_analysis_queries.sql` (full tables in [sample_query_results.md](sample_query_results.md)) and were cross-checked against an independent pandas calculation on the raw CSVs.

**Reading rule:** these are *descriptions*. The dataset has no information about carriers, weather, seller behaviour or how estimates are set, so none of the observations below prove a cause. Where a possible explanation is mentioned it is labelled as a question to investigate, not a conclusion.

## Definitions used
* **Eligible order**: status `delivered`, delivery and estimated timestamps present, delivered ≥ purchased. **96,470** orders (96,478 delivered − 8 with no delivery date).
* **Late (timestamp rule)**: delivered timestamp later than the estimated timestamp — the handbook's reference rule.
* **Late (calendar-day rule)**: delivered *date* later than the estimated *date*.

## 1. Overall late-delivery rate (q01)
**8.11%** (7,826 of 96,470) under the timestamp rule; **6.77%** (6,534) under the calendar-day rule.

The estimate is always midnight, so under the timestamp rule an order delivered at 10:00 *on the promised day* counts as late. That accounts for the whole gap: **1,292 of the 7,826 "late" orders (16.5%) arrived on the estimated date itself** (q09). Which rule is "right" is a business decision; this project reports the handbook's rule as primary and shows the other beside it. To switch, change one column in `sql/03_transform.sql` (or query `is_late_calendar_day`).

## 2. Month by month (q02)
From delivery month 2017-02 to 2018-08 (the months with real volume, 95,861 orders) the late rate averages 8.10%, but moves a lot:
* 2017-03 to 2017-11: between 2.7% and 6.0%.
* Step up in **2017-12 (10.6%)** and **2018-01 (10.1%)**, a dip in 2018-02 (6.6%), then peaks in **2018-03 (15.8%)** and **2018-04 (18.6%)**.
* 2018-05 6.6%, 2018-07 1.7%, 2018-08 10.6%.

The data shows *when* lateness rose, not *why*. Questions this raises: were estimates tightened, was volume unusually high, did a carrier or region have problems in Mar–Apr 2018?

**Do not read the edge months.** 2016-10…2017-01 and 2018-09/10 have tiny volumes (flagged `low_volume`); e.g. 2018-09 shows 100% late but is 56 orders, 2018-10 is 3 orders.

## 3. Sellers (q03)
Among single-seller orders and sellers with ≥ 50 eligible orders, the worst late rates are 30.6% (72 orders), 26.9% (93), 25.4% (63) … down to 18.3% for the 15th. That is roughly two to four times the 8.1% average. 415 sellers qualify, and picking the top 15 of 415 exaggerates extremes, so the very highest rates (25%+, e.g. 22 late of 72) are hard to explain by chance alone while the lower-ranked ones are less certain. The largest of them by volume has 388 orders at 23.2% (seller state MA).

Caveats: lateness is the *whole* chain (seller handling time, carrier, destination), so "seller late rate" is an attribution simplification. 11 of the 15 are in SP, but 59.7% of all sellers are in SP, so that is not by itself informative.

## 4. Categories (q04)
Highest rates (≥ 200 eligible orders): audio 12.9% (348), technical books 10.9% (256), home comfort 10.5% (392). The big categories sit close to the average (health_beauty 9.0% of 8,647; bed_bath_table 8.8% of 9,272). With 71 categories compared at once, one or two will look unusual by chance, so treat the top few as leads. An order with items from two categories counts once in each.

## 5. How long delivery takes (q05, q06)
Purchase → customer delivery: mean **12.6 days**, median **10.2**, 90th percentile **23.1**, 95th **29.3**, maximum 209.6. The mean exceeds the median because a long tail of slow orders pulls it up. The average *promised* time is 23.7 days, i.e. on average orders arrive **11.2 days before** the estimate, and 91.9% are not late.

By customer state (≥ 300 orders): São Paulo is fastest (8.8 days, 40,494 orders = 42% of eligible volume); AL (24.5), PA (23.8), MA (21.6) are slowest, and AL also has the highest late rate (23.9%). Geography clearly matters descriptively, but the data has no distance or carrier variable, so the *mechanism* is not shown.

## 6. Late orders: how late? (q09)
Of the 7,826 late orders: 16.5% arrived on the estimated date (after midnight), 23.9% were 1–3 calendar days late, 23.0% 4–7, 18.9% 8–14, 17.7% more than 14. So **36.6% of late orders were over a week late**.

## 7. Cancellations (q07, q08)
**625 of 99,441 orders (0.63%)** are `canceled`. Monthly rates for months with real volume mostly lie between 0.2% and 1.3%; 2016-10 is higher (7.4%, 24 of 324) and 2018-02 (1.1%) and 2018-08 (1.3%) are the highest recent months. Months 2018-09 (15 of 16) and 2018-10 (4 of 4) are tiny and unrepresentative.

Definition choice: `unavailable` (609 orders, 0.61%) is *not* counted as canceled; counting both gives 1.24%. 1,107 `shipped` and 622 other in-progress orders have no outcome yet and are excluded from delivery metrics.

## 8. Single vs multiple sellers (q10)
Surprising: orders fulfilled by several sellers (1,275 eligible) were late **1.41%** of the time versus **8.20%** for single-seller orders (95,195), and arrived faster on average (9.1 vs 12.6 days) despite longer estimates (25.4 vs 23.7 days). The numbers reproduce independently, so it is not a bug, but the group is small and the data cannot say why; it is a question for the business, not a finding about cause.

## 9. Data quality (q11, q12, q13)
* **No** duplicate IDs, **no** missing keys, **no** orphan references in any table.
* **203** orders carry at least one impossible/inconsistent flag: 166 handed to the carrier before purchase, 23 delivered before carrier pick-up, 8 `delivered` with no delivery date, 6 `canceled` with a delivery date. A further 1,359 have carrier pick-up before payment approval (suspicious but plausible; not counted among the 203).
* 610 products have no category; 2 categories lack an English translation; 775 orders have no item rows (mostly `unavailable`/`canceled`).

## Limitations
1. Public, historical, anonymised marketplace data (2016–2018); not a statement about any company today.
2. Orders still in progress at extraction time are excluded from delivery metrics, and the edge months are incomplete.
3. Timestamps have no timezone; durations assume one consistent clock.
4. 188 eligible orders (of 189 in total) have an out-of-sequence carrier timestamp; they remain in the metrics because their purchase, delivery and estimate timestamps are valid.
5. Seller/category rates depend on the attribution rules stated above.
6. No statistical significance testing was done; rankings of small groups should be read as leads.
