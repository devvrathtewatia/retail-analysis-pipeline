"""Pipeline entry point.   Run with:   python -m src.run_pipeline

Stages
  1. extract   - read + validate every CSV (no database access yet)
  2. transform - parse dates/numbers, standardise nulls (still no database changes)
  3. load      - ONE transaction: TRUNCATE + COPY staging, check staging, build analytics
                 tables from SQL, check analytics. COMMIT only if no CRITICAL check fails.
  4. audit     - every run (success or failure) is recorded in audit.pipeline_runs, and
                 every check result in audit.dq_results, over a separate connection.

Exit code: 0 on success, 1 on any failure. A failed run changes nothing in staging or
analytics (the transaction is rolled back) and is recorded with status FAILED.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import psycopg2
from psycopg2.extras import Json

from . import config, extract, load, quality, transform

log = logging.getLogger("pipeline")

RUN_STATUS_RUNNING = "RUNNING"


class PipelineError(RuntimeError):
    """A pipeline failure tagged with the stage in which it happened."""

    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage


def setup_logging(verbose: bool = False, log_file: Path | None = None) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)


# --------------------------------------------------------------------------- stages
def extract_and_transform(data_dir: Path, max_bad_pct: float) -> tuple[dict, dict]:
    """Stages 1 + 2. Returns ({table: DataFrame}, {table: TableReport})."""
    frames, reports = {}, {}
    for name, spec in config.TABLE_SPECS.items():
        try:
            raw, source_rows = extract.read_table(data_dir, spec)
        except extract.ExtractError as exc:
            raise PipelineError("extract", str(exc)) from exc
        try:
            frames[name], reports[name] = transform.clean_table(raw, spec, source_rows, max_bad_pct)
        except transform.TransformError as exc:
            raise PipelineError("transform", str(exc)) from exc
    return frames, reports


def load_and_model(
    db_params: dict, frames: dict, reports: dict, sql_dir: Path,
    rebuild_schema: bool, results: list, details: dict,
) -> None:
    """Stage 3: the single load transaction. ``results`` is appended to in place so the
    caller still has the check results if this raises."""
    conn = load.connect(db_params)  # autocommit off: everything below is one transaction
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_xact_lock(%s)", (config.ADVISORY_LOCK_KEY,))
            if not cur.fetchone()[0]:
                raise PipelineError("load", "Another pipeline run is already in progress")

            if rebuild_schema:
                log.warning("--rebuild-schema: dropping schemas staging and analytics")
                cur.execute("DROP SCHEMA IF EXISTS analytics CASCADE; "
                            "DROP SCHEMA IF EXISTS staging CASCADE;")
            load.run_sql_file(cur, sql_dir / "01_create_staging.sql")

            specs = list(config.TABLE_SPECS.values())
            load.truncate_staging(cur, specs)
            for spec in specs:
                loaded = load.copy_dataframe(cur, spec, frames[spec.name])
                source_rows = reports[spec.name].source_rows
                results.append(quality.CheckResult(
                    f"rowcount_source_equals_staging_{spec.name}", "staging", quality.CRITICAL,
                    f"{spec.filename}: CSV record count equals rows loaded into staging.{spec.name}",
                    abs(source_rows - loaded),
                ))

            results.extend(quality.run_checks(cur, "staging"))
            failed = quality.critical_failures(results)
            if failed:
                raise PipelineError("quality", _format_failures(failed, "staging"))

            load.run_sql_file(cur, sql_dir / "02_create_analytics.sql")
            load.run_sql_file(cur, sql_dir / "03_transform.sql")

            analytics_results = quality.run_checks(cur, "analytics")
            results.extend(analytics_results)
            failed = quality.critical_failures(results)
            if failed:
                raise PipelineError("quality", _format_failures(failed, "analytics"))

            details["analytics_rows"] = _row_counts(cur)
            details["headline"] = _headline(cur)
        conn.commit()
    except PipelineError:
        conn.rollback()
        raise
    except psycopg2.Error as exc:
        conn.rollback()
        raise PipelineError("database", f"{type(exc).__name__}: {str(exc).strip()}") from exc
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _format_failures(failed: list, stage: str) -> str:
    items = "; ".join(f"{r.name} ({r.violations} violation(s))" for r in failed)
    return f"Critical data-quality check(s) failed at {stage} stage - load rolled back: {items}"


def _row_counts(cur) -> dict:
    out = {}
    for t in ("dim_customers", "dim_sellers", "dim_products", "fact_orders", "fact_order_items"):
        cur.execute(f"SELECT COUNT(*) FROM analytics.{t}")
        out[t] = int(cur.fetchone()[0])
    return out


def _headline(cur) -> dict:
    cur.execute(
        "SELECT COUNT(*) FILTER (WHERE is_delivery_eligible), "
        "       COUNT(*) FILTER (WHERE is_late), "
        "       COUNT(*) FILTER (WHERE is_canceled), COUNT(*) FROM analytics.fact_orders"
    )
    eligible, late, canceled, total = (int(x) for x in cur.fetchone())
    return {"orders": total, "eligible_orders": eligible, "late_orders": late,
            "canceled_orders": canceled}


# --------------------------------------------------------------------------- audit
def _audit_start(audit_conn, sql_dir: Path) -> int:
    with audit_conn.cursor() as cur:
        cur.execute((sql_dir / "00_create_audit.sql").read_text(encoding="utf-8"))
        cur.execute("INSERT INTO audit.pipeline_runs (status) VALUES (%s) RETURNING run_id",
                    (RUN_STATUS_RUNNING,))
        return int(cur.fetchone()[0])


def _audit_finish(audit_conn, run_id, status, stage, error, details, results) -> None:
    with audit_conn.cursor() as cur:
        cur.execute(
            "UPDATE audit.pipeline_runs SET finished_at = now(), status = %s, failed_stage = %s, "
            "error_message = %s, details = %s WHERE run_id = %s",
            (status, stage, error, Json(details), run_id),
        )
        for r in results:
            cur.execute(
                "INSERT INTO audit.dq_results (run_id, check_name, stage, severity, status, "
                "violations, description) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (run_id, r.name, r.stage, r.severity, r.status, r.violations, r.description),
            )


# --------------------------------------------------------------------------- orchestration
def run(
    data_dir: Path,
    db_params: dict,
    sql_dir: Path = config.SQL_DIR,
    rebuild_schema: bool = False,
    max_bad_pct: float | None = None,
) -> int:
    """Run the whole pipeline. Returns the process exit code (0 = success, 1 = failure)."""
    t0 = time.monotonic()
    data_dir, sql_dir = Path(data_dir), Path(sql_dir)
    if max_bad_pct is None:
        max_bad_pct = config.get_max_bad_value_pct()

    try:
        audit_conn = load.connect(db_params, autocommit=True)
    except psycopg2.Error as exc:
        log.error("PIPELINE FAILED [database]: cannot connect to PostgreSQL: %s", str(exc).strip())
        return 1

    run_id, results, details = None, [], {}
    status, failed_stage, error = "SUCCESS", None, None
    try:
        run_id = _audit_start(audit_conn, sql_dir)
        log.info("=== Pipeline run %s started (data dir: %s) ===", run_id, data_dir)

        frames, reports = extract_and_transform(data_dir, max_bad_pct)
        details["tables"] = {n: r.to_dict() for n, r in reports.items()}

        load_and_model(db_params, frames, reports, sql_dir, rebuild_schema, results, details)

        warn = [r for r in results if r.status == "WARN"]
        log.info("Data-quality: %d checks, %d passed, %d warnings, 0 critical failures",
                 len(results), sum(r.status == "PASS" for r in results), len(warn))
        h = details.get("headline", {})
        log.info("Orders: %s total, %s delivery-eligible, %s late, %s canceled",
                 h.get("orders"), h.get("eligible_orders"), h.get("late_orders"),
                 h.get("canceled_orders"))
        try:  # refresh planner statistics; harmless if it fails
            with audit_conn.cursor() as cur:
                cur.execute("ANALYZE analytics.fact_orders; ANALYZE analytics.fact_order_items;")
        except psycopg2.Error:
            log.debug("ANALYZE skipped", exc_info=True)
    except PipelineError as exc:
        status, failed_stage, error = "FAILED", exc.stage, str(exc)
    except Exception as exc:  # noqa: BLE001 - last line of defence, must record the failure
        status, failed_stage, error = "FAILED", "unexpected", f"{type(exc).__name__}: {exc}"
        log.exception("Unexpected error")
    finally:
        details["duration_seconds"] = round(time.monotonic() - t0, 2)
        details["dq_summary"] = {
            "checks": len(results),
            "pass": sum(r.status == "PASS" for r in results),
            "warn": sum(r.status == "WARN" for r in results),
            "fail": sum(r.status == "FAIL" for r in results),
        }
        if run_id is not None:
            try:
                _audit_finish(audit_conn, run_id, status, failed_stage, error, details, results)
            except psycopg2.Error:
                log.exception("Could not write the audit record for run %s", run_id)
        audit_conn.close()

    if status == "FAILED":
        log.error("PIPELINE FAILED [%s] (run %s): %s", failed_stage, run_id, error)
        return 1
    log.info("=== PIPELINE SUCCEEDED (run %s, %.1fs) ===", run_id, details["duration_seconds"])
    return 0


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="Retail Order & Delivery Analytics Pipeline")
    parser.add_argument("--data-dir", type=Path, default=None,
                        help="folder with the raw CSV files (default: RAW_DATA_DIR or data/raw)")
    parser.add_argument("--dry-run", action="store_true",
                        help="extract + validate + clean only; do not touch the database")
    parser.add_argument("--rebuild-schema", action="store_true",
                        help="DROP and recreate the staging and analytics schemas first "
                             "(needed only after changing the table definitions)")
    parser.add_argument("--max-bad-pct", type=float, default=None,
                        help="fail if more than this %% of one column's values are unparseable "
                             "(default: MAX_BAD_VALUE_PCT or 1.0)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    setup_logging(args.verbose, config.LOG_DIR / "pipeline.log")
    data_dir = args.data_dir or config.get_raw_data_dir()

    if args.dry_run:
        try:
            _, reports = extract_and_transform(
                data_dir, args.max_bad_pct if args.max_bad_pct is not None
                else config.get_max_bad_value_pct())
        except PipelineError as exc:
            log.error("DRY RUN FAILED [%s]: %s", exc.stage, exc)
            return 1
        for name, rep in reports.items():
            log.info("dry-run OK  %-30s %9s rows", name, f"{rep.rows_out:,}")
        return 0

    try:
        db_params = config.get_db_params()
    except config.ConfigError as exc:
        log.error("PIPELINE FAILED [config]: %s", exc)
        return 1
    return run(data_dir, db_params, rebuild_schema=args.rebuild_schema, max_bad_pct=args.max_bad_pct)


if __name__ == "__main__":
    sys.exit(main())
