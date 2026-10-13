"""Data-quality checks.

Every check is a SQL query that returns ONE number: how many rows violate the rule.
0 means the check passes.

Severity decides what a violation means:

* CRITICAL - the data cannot be trusted or the model would be wrong. The run FAILS and
  the whole load transaction is rolled back (previous data stays in place).
* WARN     - a real-world data issue that is recorded and reported but does not stop the
  run (e.g. orders with impossible timestamps are flagged and kept, never deleted).

Checks run at two stages: ``staging`` (right after loading, before transformation) and
``analytics`` (after the model is built).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import TABLE_SPECS

log = logging.getLogger(__name__)

CRITICAL = "CRITICAL"
WARN = "WARN"
KNOWN_STATUSES = (
    "created", "approved", "invoiced", "processing",
    "shipped", "delivered", "unavailable", "canceled",
)


@dataclass(frozen=True)
class Check:
    name: str
    stage: str  # 'staging' | 'analytics'
    severity: str  # CRITICAL | WARN
    description: str
    sql: str  # returns a single integer: number of violations


@dataclass
class CheckResult:
    name: str
    stage: str
    severity: str
    description: str
    violations: int

    @property
    def status(self) -> str:
        if self.violations == 0:
            return "PASS"
        return "FAIL" if self.severity == CRITICAL else "WARN"


# --------------------------------------------------------------------------- staging
def _staging_checks() -> list:
    checks = []

    # Generated from the table specs: primary keys unique, required columns not null.
    for spec in TABLE_SPECS.values():
        t = f"staging.{spec.name}"
        pk = ", ".join(spec.primary_key)
        checks.append(Check(
            f"{spec.name}_pk_unique", "staging", CRITICAL,
            f"{t}: ({pk}) is unique (no duplicate IDs at the table grain)",
            f"SELECT COUNT(*) FROM (SELECT 1 FROM {t} GROUP BY {pk} HAVING COUNT(*) > 1) d",
        ))
        for col in spec.not_null_columns:
            checks.append(Check(
                f"{spec.name}_{col}_not_null", "staging", CRITICAL,
                f"{t}.{col} is never NULL",
                f"SELECT COUNT(*) FROM {t} WHERE {col} IS NULL",
            ))

    # Relationships: every child key must exist in its parent table.
    fks = [
        ("orders", "customer_id", "customers", "customer_id"),
        ("order_items", "order_id", "orders", "order_id"),
        ("order_items", "product_id", "products", "product_id"),
        ("order_items", "seller_id", "sellers", "seller_id"),
    ]
    for child, ccol, parent, pcol in fks:
        checks.append(Check(
            f"fk_{child}_{ccol}_in_{parent}", "staging", CRITICAL,
            f"every staging.{child}.{ccol} exists in staging.{parent}.{pcol} (no orphans)",
            f"SELECT COUNT(*) FROM staging.{child} c "
            f"WHERE c.{ccol} IS NOT NULL AND NOT EXISTS "
            f"(SELECT 1 FROM staging.{parent} p WHERE p.{pcol} = c.{ccol})",
        ))

    checks += [
        Check("order_items_price_positive", "staging", CRITICAL,
              "staging.order_items.price > 0",
              "SELECT COUNT(*) FROM staging.order_items WHERE price <= 0"),
        Check("order_items_freight_non_negative", "staging", CRITICAL,
              "staging.order_items.freight_value >= 0",
              "SELECT COUNT(*) FROM staging.order_items WHERE freight_value < 0"),
        Check("orders_status_known", "staging", WARN,
              "order_status is one of the 8 documented statuses",
              "SELECT COUNT(*) FROM staging.orders WHERE order_status NOT IN ("
              + ", ".join(f"'{s}'" for s in KNOWN_STATUSES) + ")"),
        Check("products_missing_category", "staging", WARN,
              "products with no category name (mapped to 'unknown')",
              "SELECT COUNT(*) FROM staging.products WHERE product_category_name IS NULL"),
        Check("categories_missing_translation", "staging", WARN,
              "distinct product categories without an English translation "
              "(Portuguese name is used instead)",
              "SELECT COUNT(DISTINCT p.product_category_name) FROM staging.products p "
              "WHERE p.product_category_name IS NOT NULL AND NOT EXISTS "
              "(SELECT 1 FROM staging.product_category_translation t "
              " WHERE t.product_category_name = p.product_category_name)"),
        Check("zip_prefix_format", "staging", WARN,
              "customer/seller zip prefixes are exactly 5 digits",
              "SELECT (SELECT COUNT(*) FROM staging.customers "
              "        WHERE customer_zip_code_prefix !~ '^[0-9]{5}$') "
              "     + (SELECT COUNT(*) FROM staging.sellers "
              "        WHERE seller_zip_code_prefix !~ '^[0-9]{5}$')"),
    ]
    return checks


# --------------------------------------------------------------------------- analytics
def _analytics_checks() -> list:
    c = []
    # Row counts reconcile between staging and the model: nothing lost, nothing duplicated.
    for stg, tgt in [
        ("orders", "fact_orders"),
        ("order_items", "fact_order_items"),
        ("customers", "dim_customers"),
        ("sellers", "dim_sellers"),
        ("products", "dim_products"),
    ]:
        c.append(Check(
            f"rowcount_staging_{stg}_equals_analytics_{tgt}", "analytics", CRITICAL,
            f"row count of analytics.{tgt} equals staging.{stg}",
            f"SELECT ABS((SELECT COUNT(*) FROM staging.{stg}) "
            f"- (SELECT COUNT(*) FROM analytics.{tgt}))",
        ))

    c += [
        Check("order_totals_reconcile_to_items", "analytics", CRITICAL,
              "sum of fact_orders.items_value/item_count equals the item rows "
              "(items neither lost nor double counted)",
              "SELECT (ABS((SELECT COALESCE(SUM(items_value), 0) FROM analytics.fact_orders) "
              "          - (SELECT COALESCE(SUM(price), 0) FROM staging.order_items)) > 0.001)::int "
              "     + ((SELECT COALESCE(SUM(item_count), 0) FROM analytics.fact_orders) "
              "          <> (SELECT COUNT(*) FROM staging.order_items))::int"),
        Check("late_orders_not_exceed_eligible", "analytics", CRITICAL,
              "number of late orders never exceeds number of eligible delivered orders",
              "SELECT (COUNT(*) FILTER (WHERE is_late) > "
              "        COUNT(*) FILTER (WHERE is_delivery_eligible))::int "
              "FROM analytics.fact_orders"),
        Check("late_flag_only_on_eligible_orders", "analytics", CRITICAL,
              "is_late / is_late_calendar_day / delivery metrics are NULL for "
              "non-eligible orders and set for eligible ones",
              "SELECT COUNT(*) FROM analytics.fact_orders WHERE "
              "(is_delivery_eligible AND (is_late IS NULL OR is_late_calendar_day IS NULL "
              "                           OR delivery_days IS NULL OR days_late IS NULL)) "
              "OR (NOT is_delivery_eligible AND (is_late IS NOT NULL "
              "                           OR is_late_calendar_day IS NOT NULL "
              "                           OR delivery_days IS NOT NULL OR days_late IS NOT NULL))"),
        Check("eligible_orders_have_required_timestamps", "analytics", CRITICAL,
              "eligible orders have status 'delivered' and non-null purchase, delivery "
              "and estimated timestamps",
              "SELECT COUNT(*) FROM analytics.fact_orders WHERE is_delivery_eligible AND "
              "(order_status <> 'delivered' OR purchased_at IS NULL "
              " OR delivered_at IS NULL OR estimated_delivery_at IS NULL)"),
        Check("eligible_orders_delivery_days_non_negative", "analytics", CRITICAL,
              "no eligible order has a negative purchase-to-delivery duration",
              "SELECT COUNT(*) FROM analytics.fact_orders "
              "WHERE is_delivery_eligible AND delivery_days < 0"),
        Check("calendar_day_late_subset_of_timestamp_late", "analytics", CRITICAL,
              "an order late by calendar day is always also late by full timestamp",
              "SELECT COUNT(*) FROM analytics.fact_orders "
              "WHERE is_late_calendar_day AND NOT is_late"),
        Check("seller_view_covers_all_item_orders", "analytics", CRITICAL,
              "v_order_sellers has the same set of orders as the item table "
              "(its order-level outcomes are not lost in the view)",
              "SELECT ABS((SELECT COUNT(DISTINCT order_id) FROM analytics.v_order_sellers) "
              "         - (SELECT COUNT(DISTINCT order_id) FROM analytics.fact_order_items))"),
    ]

    # Impossible / inconsistent data: counted and flagged, never deleted.
    warn_flags = [
        ("dq_approved_before_purchase", "orders approved before they were purchased"),
        ("dq_carrier_before_purchase", "orders handed to the carrier before purchase"),
        ("dq_delivered_before_carrier", "orders delivered to the customer before the carrier pick-up"),
        ("dq_delivered_before_purchase", "orders delivered before purchase"),
        ("dq_estimated_before_purchase", "orders whose estimated delivery precedes purchase"),
        ("dq_delivered_status_missing_date", "status 'delivered' but no delivery date"),
        ("dq_delivery_date_on_non_delivered", "delivery date present but status is not 'delivered'"),
        ("dq_carrier_before_approval", "carrier pick-up before payment approval (suspicious, possible)"),
        ("dq_no_items", "orders with no item rows"),
    ]
    for col, desc in warn_flags:
        c.append(Check(
            f"flag_{col[3:]}", "analytics", WARN, f"{desc} (flagged, kept in fact_orders)",
            f"SELECT COUNT(*) FROM analytics.fact_orders WHERE {col}",
        ))
    return c


ALL_CHECKS = _staging_checks() + _analytics_checks()


def checks_for(stage: str) -> list:
    return [c for c in ALL_CHECKS if c.stage == stage]


def run_checks(cur, stage: str) -> list:
    """Run every check of one stage on an open cursor and return the results."""
    results = []
    for chk in checks_for(stage):
        cur.execute(chk.sql)
        violations = int(cur.fetchone()[0])
        res = CheckResult(chk.name, chk.stage, chk.severity, chk.description, violations)
        results.append(res)
        level = {"PASS": logging.DEBUG, "WARN": logging.WARNING, "FAIL": logging.ERROR}[res.status]
        log.log(level, "[%s] %-6s %-60s violations=%s", stage, res.status, res.name, violations)
    return results


def critical_failures(results: list) -> list:
    return [r for r in results if r.status == "FAIL"]
