#!/usr/bin/env python3
import argparse
import csv
import re
import sys
import time
import json
from dataclasses import dataclass
from pathlib import Path

from fts_update_benchmark import (
    DATASET_KIND_DATAGEN_JSONL,
    DEFAULT_DB,
    DEFAULT_DUCKDB,
    DEFAULT_FTS_EXTENSION,
    DuckDBProcess,
    cleanup_bench_table,
    collect_base_counts,
    create_index_sql,
    prepare_staging,
    reset_bench_docs,
    sql_string,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = ROOT / "benchmark-results/datagen-500k/items.jsonl"
DEFAULT_OUTPUT_DIR = ROOT / "benchmark-results/trigger-profiles"


@dataclass
class ProfileResult:
    run_id: str
    tenant: str
    base_rows: int
    operation: str
    batch_size: int
    component: str
    seconds: float
    explain_total_seconds: float | None
    plan_file: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Profile trigger-equivalent FTS maintenance statements with EXPLAIN ANALYZE."
    )
    parser.add_argument("--duckdb", type=Path, default=DEFAULT_DUCKDB)
    parser.add_argument("--database", type=Path, default=DEFAULT_DB)
    parser.add_argument("--fts-extension", type=Path, default=DEFAULT_FTS_EXTENSION)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--dataset-kind", default=DATASET_KIND_DATAGEN_JSONL)
    parser.add_argument("--tenant", default="account_900001")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument(
        "--operation",
        choices=["insert", "delete", "mixed"],
        default="mixed",
        help="Which trigger path to profile. mixed runs insert components then delete components.",
    )
    parser.add_argument("--statement-timeout", type=int, default=900)
    parser.add_argument("--storage-version", default="v2.0.0")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--recreate-db",
        action="store_true",
        help="Delete the profiling database before running.",
    )
    parser.add_argument(
        "--skip-staging",
        action="store_true",
        help="Reuse existing bench_fts staging tables.",
    )
    parser.add_argument(
        "--no-explain",
        action="store_true",
        help="Run the component statements without DuckDB JSON profiling. Useful for checking raw statement timing.",
    )
    return parser.parse_args()


def fts_schema(table_name: str) -> str:
    return f"fts_main_{table_name}"


def explain_total_seconds(lines: list[str]) -> float | None:
    text = "".join(lines)
    match = re.search(r"Total Time:\s*([0-9.]+)s", text)
    return float(match.group(1)) if match else None


def write_plan(output_dir: Path, run_id: str, component: str, lines: list[str]) -> Path:
    plan_dir = output_dir / "plans" / run_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    path = plan_dir / f"{component}.txt"
    path.write_text("".join(lines), encoding="utf-8")
    return path


def profile_json_path(output_dir: Path, run_id: str, component: str) -> Path:
    profile_dir = output_dir / "profiles" / run_id
    profile_dir.mkdir(parents=True, exist_ok=True)
    return profile_dir / f"{component}.json"


def profile_latency_seconds(path: Path) -> float | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    for key in ["latency", "execution_time", "timing", "time"]:
        value = data.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def append_results(output_dir: Path, rows: list[ProfileResult]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "fts_trigger_profile_results.csv"
    write_header = not path.exists()
    with path.open("a", newline="") as handle:
        writer = csv.writer(handle)
        if write_header:
            writer.writerow(
                [
                    "run_id",
                    "tenant",
                    "base_rows",
                    "operation",
                    "batch_size",
                    "component",
                    "seconds",
                    "explain_total_seconds",
                    "plan_file",
                ]
            )
        for row in rows:
            writer.writerow(
                [
                    row.run_id,
                    row.tenant,
                    row.base_rows,
                    row.operation,
                    row.batch_size,
                    row.component,
                    row.seconds,
                    "" if row.explain_total_seconds is None else row.explain_total_seconds,
                    row.plan_file,
                ]
            )


def prepare_profile_table(con: DuckDBProcess, tenant: str, table_name: str) -> int:
    base_counts = collect_base_counts(con, [tenant])
    reset_bench_docs(con, tenant, table_name)
    con.run(create_index_sql(table_name, "incremental"))
    return base_counts[tenant]


def prepare_delta_tables(con: DuckDBProcess, tenant: str, batch_size: int) -> None:
    con.run(
        f"""
        CREATE OR REPLACE TEMP TABLE fts_new_rows AS
        SELECT id, account_id, board_id, body
        FROM bench_fts.insert_docs
        WHERE tenant_label = {sql_string(tenant)}
          AND batch_size = {batch_size}
          AND batch_id = 1;

        CREATE OR REPLACE TEMP TABLE fts_old_rows AS
        SELECT id
        FROM bench_fts.delete_docs
        WHERE tenant_label = {sql_string(tenant)}
          AND batch_size = {batch_size}
          AND batch_id = 1;
        """
    )


def insert_components(schema: str) -> list[tuple[str, str]]:
    new_docs_for_docs = f"""
        fts_new_docs AS (
            SELECT COALESCE((SELECT max(docid) + 1 FROM {schema}.docs), 0)
                       + row_number() OVER (ORDER BY fts_new_rows.id) - 1 AS docid,
                   fts_new_rows.id AS name,
                   fts_new_rows.body AS body
            FROM fts_new_rows
        )
    """
    new_docs_after_docs = f"""
        fts_new_docs AS (
            SELECT (SELECT max(docid) - (SELECT count(*) FROM fts_new_rows) + 1 FROM {schema}.docs)
                       + row_number() OVER (ORDER BY fts_new_rows.id) - 1 AS docid,
                   fts_new_rows.id AS name,
                   fts_new_rows.body AS body
            FROM fts_new_rows
        )
    """
    tokenized = f"""
        tokenized AS (
            SELECT unnest({schema}.tokenize(fts_ii.body)) AS w,
                   fts_ii.docid AS docid,
                   (SELECT fieldid FROM {schema}.fields WHERE field = 'body') AS fieldid
            FROM fts_new_docs AS fts_ii
        ),
        stemmed_stopped AS (
            SELECT stem(t.w, 'porter') AS term,
                   t.docid AS docid,
                   t.fieldid AS fieldid
            FROM tokenized AS t
            WHERE t.w NOT NULL
              AND len(t.w) > 0
              AND t.w NOT IN (SELECT sw FROM {schema}.stopwords)
        )
    """
    token_ctes = f"WITH {new_docs_after_docs}, {tokenized}"
    return [
        (
            "insert_00_docs",
            f"""
            INSERT INTO {schema}.docs (docid, name, len)
            WITH {new_docs_for_docs},
            {tokenized},
            lengths AS (
                SELECT docid, count(term) AS len
                FROM stemmed_stopped
                GROUP BY docid
            )
            SELECT nd.docid,
                   nd.name,
                   COALESCE(l.len, 0) AS len
            FROM fts_new_docs AS nd
            LEFT JOIN lengths AS l ON nd.docid = l.docid
            """,
        ),
        (
            "insert_10_dict_insert",
            f"""
            INSERT INTO {schema}.dict (termid, term, df)
            {token_ctes},
            new_terms AS (
                SELECT DISTINCT term
                FROM stemmed_stopped
                WHERE term NOT IN (SELECT term FROM {schema}.dict)
                ORDER BY term
            )
            SELECT (SELECT COALESCE(max(termid) + 1, 0) FROM {schema}.dict) + row_number() OVER () - 1 AS termid,
                   term,
                   0 AS df
            FROM new_terms
            """,
        ),
        (
            "insert_20_terms",
            f"""
            INSERT INTO {schema}.terms (docid, fieldid, termid)
            {token_ctes}
            SELECT ss.docid,
                   ss.fieldid,
                   d.termid
            FROM stemmed_stopped AS ss
            JOIN {schema}.dict AS d ON ss.term = d.term
            """,
        ),
        (
            "insert_30_dict_df",
            f"""
            UPDATE {schema}.dict AS d
            SET df = (
                SELECT count(DISTINCT docid)
                FROM {schema}.terms AS t
                WHERE d.termid = t.termid
            )
            """,
        ),
        (
            "insert_40_stats",
            f"""
            UPDATE {schema}.stats
            SET num_docs = (SELECT COUNT(docid) FROM {schema}.docs),
                avgdl = (SELECT SUM(len) / COUNT(len) FROM {schema}.docs)
            """,
        ),
    ]


def delete_components(schema: str) -> list[tuple[str, str]]:
    return [
        (
            "delete_00_terms",
            f"""
            DELETE FROM {schema}.terms
            WHERE docid IN (
                SELECT d.docid
                FROM {schema}.docs AS d
                JOIN fts_old_rows AS old_rows ON d.name = old_rows.id
            )
            """,
        ),
        (
            "delete_10_docs",
            f"""
            DELETE FROM {schema}.docs
            WHERE name IN (SELECT id FROM fts_old_rows)
            """,
        ),
        (
            "delete_20_dict_df",
            f"""
            UPDATE {schema}.dict AS d
            SET df = (
                SELECT count(DISTINCT docid)
                FROM {schema}.terms AS t
                WHERE d.termid = t.termid
            )
            """,
        ),
        (
            "delete_30_dict_prune",
            f"""
            DELETE FROM {schema}.dict
            WHERE df = 0
            """,
        ),
        (
            "delete_40_stats",
            f"""
            UPDATE {schema}.stats
            SET num_docs = (SELECT COUNT(docid) FROM {schema}.docs),
                avgdl = (SELECT SUM(len) / COUNT(len) FROM {schema}.docs)
            """,
        ),
    ]


def components_for(operation: str, schema: str) -> list[tuple[str, str]]:
    if operation == "insert":
        return insert_components(schema)
    if operation == "delete":
        return delete_components(schema)
    if operation == "mixed":
        return insert_components(schema) + delete_components(schema)
    raise ValueError(operation)


def profile_component(
    con: DuckDBProcess,
    sql: str,
    profile_path: Path | None,
) -> tuple[float, float | None, list[str]]:
    if profile_path is not None:
        if profile_path.exists():
            profile_path.unlink()
        con.run(
            f"""
            PRAGMA enable_profiling='json';
            PRAGMA profiling_output={sql_string(profile_path)};
            """
        )
    elapsed, lines = con.run(sql, timed=True)
    if profile_path is not None:
        con.run("PRAGMA disable_profiling;")
    assert elapsed is not None
    return (
        elapsed,
        profile_latency_seconds(profile_path) if profile_path is not None else None,
        lines,
    )


def main() -> int:
    args = parse_args()
    for path in [args.duckdb, args.fts_extension, args.data]:
        if not path.exists():
            raise FileNotFoundError(path)
    if args.batch_size <= 0:
        raise ValueError("--batch-size must be positive")

    if args.recreate_db:
        for path in [args.database, Path(str(args.database) + ".wal")]:
            if path.exists():
                path.unlink()

    run_id = time.strftime("%Y%m%d-%H%M%S")
    table_name = "profile_docs"
    schema = fts_schema(table_name)
    rows: list[ProfileResult] = []

    con = DuckDBProcess(
        args.duckdb,
        args.database,
        args.fts_extension,
        args.storage_version,
        args.statement_timeout,
    )
    try:
        if not args.skip_staging:
            prepare_staging(con, args.data, [args.batch_size], args.dataset_kind)
        cleanup_bench_table(con, table_name)
        base_rows = prepare_profile_table(con, args.tenant, table_name)
        prepare_delta_tables(con, args.tenant, args.batch_size)

        print(
            f"Profiling {args.operation} batch={args.batch_size} tenant={args.tenant} "
            f"base_rows={base_rows} run_id={run_id}",
            flush=True,
        )
        for component, sql in components_for(args.operation, schema):
            print(f"  {component}", flush=True)
            profile_path_value = None if args.no_explain else profile_json_path(args.output_dir, run_id, component)
            seconds, explain_seconds, lines = profile_component(
                con,
                sql,
                profile_path=profile_path_value,
            )
            plan_file = ""
            if profile_path_value is not None and profile_path_value.exists():
                plan_file = str(profile_path_value.relative_to(ROOT))
            elif lines:
                plan_file = str(
                    write_plan(args.output_dir, run_id, component, lines).relative_to(ROOT)
                )
            rows.append(
                ProfileResult(
                    run_id=run_id,
                    tenant=args.tenant,
                    base_rows=base_rows,
                    operation=args.operation,
                    batch_size=args.batch_size,
                    component=component,
                    seconds=seconds,
                    explain_total_seconds=explain_seconds,
                    plan_file=plan_file,
                )
            )
            explain_label = "" if explain_seconds is None else f" explain_total={explain_seconds:.6f}s"
            print(f"    wall={seconds:.6f}s{explain_label}", flush=True)
        append_results(args.output_dir, rows)
    finally:
        try:
            cleanup_bench_table(con, table_name)
        except Exception as cleanup_error:
            print(f"warning: cleanup failed for {table_name}: {cleanup_error}", file=sys.stderr)
        con.close()

    print(f"Results appended to {args.output_dir / 'fts_trigger_profile_results.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
