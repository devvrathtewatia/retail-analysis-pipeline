
import csv
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import psycopg2
from dotenv import load_dotenv


# Project paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SQL_FILE = PROJECT_ROOT / "sql" / "04_analysis_queries.sql"
OUTPUT_DIR = PROJECT_ROOT / "outputs" / "analysis"
# Load database credentials from the project's .env file
load_dotenv(PROJECT_ROOT / ".env")


def get_connection():
    """Connect to the project's PostgreSQL database."""

    database_url = os.getenv("DATABASE_URL")

    if database_url:
        return psycopg2.connect(database_url)

    # Support common PostgreSQL environment-variable names.
    host = os.getenv("DB_HOST") or os.getenv("PGHOST") or "localhost"
    port = os.getenv("DB_PORT") or os.getenv("PGPORT") or "5432"
    dbname = (
        os.getenv("DB_NAME")
        or os.getenv("DB_DATABASE")
        or os.getenv("PGDATABASE")
    )
    user = os.getenv("DB_USER") or os.getenv("PGUSER")
    password = os.getenv("DB_PASSWORD") or os.getenv("PGPASSWORD")

    missing = [
        name
        for name, value in {
            "database name": dbname,
            "database user": user,
            "database password": password,
        }.items()
        if not value
    ]

    if missing:
        raise RuntimeError(
            "Missing database settings in .env: " + ", ".join(missing)
        )

    return psycopg2.connect(
        host=host,
        port=port,
        dbname=dbname,
        user=user,
        password=password,
    )


def load_queries():
    """Read named SQL queries from the existing analysis SQL file."""

    if not SQL_FILE.exists():
        raise FileNotFoundError(f"SQL file not found: {SQL_FILE}")

    sql_text = SQL_FILE.read_text(encoding="utf-8")

    # Each query starts with a marker such as:
    # -- name: q01_overall_late_rate
    marker = re.compile(
        r"(?m)^--\s*name:\s*([A-Za-z0-9_]+)\s*$",
        re.IGNORECASE,
    )

    matches = list(marker.finditer(sql_text))

    if not matches:
        raise ValueError(
            "No '-- name: query_name' markers found in the SQL file."
        )

    queries = []

    for index, match in enumerate(matches):
        start = match.end()
        end = (
            matches[index + 1].start()
            if index + 1 < len(matches)
            else len(sql_text)
        )

        query_name = match.group(1)
        query_sql = sql_text[start:end].strip()

        if query_sql:
            queries.append((query_name, query_sql))

    return queries


def save_csv(query_name, columns, rows):
    """Save one query's results as a CSV file."""

    output_path = OUTPUT_DIR / f"{query_name}.csv"

    with output_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(columns)
        writer.writerows(rows)

    return output_path


def run_analysis():
    """Execute all named queries and save their results."""

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    queries = load_queries()

    print(f"SQL file: {SQL_FILE}")
    print(f"Queries found: {len(queries)}")
    print(f"Output directory: {OUTPUT_DIR}\n")

    summary = {
        "run_at": datetime.now().astimezone().isoformat(),
        "sql_file": str(SQL_FILE.relative_to(PROJECT_ROOT)),
        "query_count": len(queries),
        "queries": [],
    }

    connection = None

    try:
        connection = get_connection()

        with connection.cursor() as cursor:
            for number, (query_name, query_sql) in enumerate(
                queries, start=1
            ):
                print(f"[{number}/{len(queries)}] Running {query_name}...")

                try:
                    cursor.execute(query_sql)

                    if cursor.description is None:
                        raise RuntimeError(
                            "Query did not return a result set."
                        )

                    columns = [
                        description[0]
                        for description in cursor.description
                    ]
                    rows = cursor.fetchall()

                    output_path = save_csv(query_name, columns, rows)

                    summary["queries"].append(
                        {
                            "name": query_name,
                            "status": "success",
                            "row_count": len(rows),
                            "columns": columns,
                            "output_file": str(
                                output_path.relative_to(PROJECT_ROOT)
                            ),
                        }
                    )

                    print(
                        f"    OK | {len(rows)} rows | "
                        f"{output_path.relative_to(PROJECT_ROOT)}"
                    )

                except Exception:
                    connection.rollback()
                    print(f"\nFAILED: {query_name}", file=sys.stderr)
                    raise

                else:
                    connection.commit()

        summary["status"] = "success"

    except Exception:
        summary["status"] = "failed"
        raise

    finally:
        if connection is not None:
            connection.close()

        summary_path = OUTPUT_DIR / "run_summary.json"
        summary_path.write_text(
            json.dumps(summary, indent=2, default=str),
            encoding="utf-8",
        )

    print("\nAll analysis queries completed successfully.")
    print(f"Summary: {OUTPUT_DIR / 'run_summary.json'}")


if __name__ == "__main__":
    try:
        run_analysis()
    except Exception as error:
        print(f"\nAnalysis runner stopped: {error}", file=sys.stderr)
        sys.exit(1)
