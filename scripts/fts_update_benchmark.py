#!/usr/bin/env python3
import argparse
import csv
import select
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DUCKDB = ROOT / "build/release/duckdb"
DEFAULT_DB = ROOT / "duckdb-eval.duckdb"
DEFAULT_FTS_EXTENSION = ROOT / "build/release/extension/fts/fts.duckdb_extension"
DEFAULT_DATA = (
    Path.home()
    / "git/duckdb-monday/search-engine/search-evaluation/evaluation-data/data-sets/master/items.json"
)
DEFAULT_RESULTS = ROOT / "benchmark-results/fts_update_results.csv"
DEFAULT_TENANTS = ["account_28132365", "account_31245546", "combined"]
DEFAULT_BATCH_SIZES = [1, 10, 100, 500]
DATASET_KIND_EVAL_JSON = "eval-json"
DATASET_KIND_DATAGEN_JSONL = "datagen-jsonl"
DEFAULT_OPERATIONS = ["insert", "delete", "mixed"]
ALL_OPERATIONS = DEFAULT_OPERATIONS + ["mixed_no_txn", "mixed_delete_insert"]
DEFAULT_APPROACHES = ["default_rebuild", "incremental"]


def sql_string(value: object) -> str:
    if value is None:
        return "NULL"
    return "'" + str(value).replace("'", "''") + "'"


def csv_first_row(lines: list[str]) -> list[str]:
    rows = list(csv.reader(lines))
    if not rows:
        return []
    return rows[0]


class DuckDBProcess:
    def __init__(
        self,
        duckdb: Path,
        database: Path,
        extension: Path,
        storage_version: str,
        statement_timeout: int,
    ):
        self.proc = subprocess.Popen(
            [
                str(duckdb),
                "-storage-version",
                storage_version,
                "-unsigned",
                "-batch",
                "-csv",
                "-noheader",
                str(database),
            ],
            cwd=str(ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self.extension = extension
        self._counter = 0
        self.statement_timeout = statement_timeout
        self.run(f"LOAD {sql_string(extension)};")

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                self.run("CHECKPOINT;")
            except Exception as checkpoint_error:
                print(f"warning: checkpoint failed during shutdown: {checkpoint_error}", file=sys.stderr)
        if self.proc.stdin:
            try:
                self.proc.stdin.close()
            except BrokenPipeError:
                pass
        if self.proc.poll() is None:
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()

    def run(self, sql: str, *, timed: bool = False) -> tuple[float | None, list[str]]:
        if self.proc.poll() is not None:
            raise RuntimeError(f"DuckDB process exited with code {self.proc.returncode}")
        if not self.proc.stdin or not self.proc.stdout:
            raise RuntimeError("DuckDB process was not opened with pipes")

        self._counter += 1
        sentinel = f"__FTS_BENCH_DONE_{self._counter}_{uuid.uuid4().hex}__"
        script = sql.rstrip()
        if script and not script.endswith(";"):
            script += ";"

        start = time.perf_counter() if timed else None
        self.proc.stdin.write(script + "\n")
        self.proc.stdin.write(f"SELECT {sql_string(sentinel)};\n")
        self.proc.stdin.flush()

        lines: list[str] = []
        while True:
            ready, _, _ = select.select([self.proc.stdout], [], [], self.statement_timeout)
            if not ready:
                raise TimeoutError(f"Timed out waiting for DuckDB after SQL:\n{sql}")
            line = self.proc.stdout.readline()
            if line == "":
                raise RuntimeError(
                    f"DuckDB exited while running SQL:\n{sql}\nOutput so far:\n{''.join(lines)}"
                )
            stripped = line.strip()
            cleaned = stripped.strip('"')
            if cleaned == sentinel or sentinel in cleaned:
                before_sentinel = line.split(sentinel, 1)[0]
                if before_sentinel.strip():
                    lines.append(before_sentinel)
                elapsed = time.perf_counter() - start if timed else None
                errors = [x for x in lines if " Error:" in x or x.startswith("Error:")]
                if errors:
                    raise RuntimeError("DuckDB reported an error:\n" + "".join(lines))
                return elapsed, lines
            lines.append(line)


@dataclass
class ResultRow:
    run_id: str
    dataset: str
    tenant: str
    approach: str
    operation: str
    base_rows: int
    batch_size: int | None
    run_index: int
    seconds: float
    rows_changed: int
    rows_per_sec: float
    ms_per_row: float
    db_file_size_bytes: int
    duckdb_version: str

    def as_list(self) -> list[object]:
        return [
            self.run_id,
            self.dataset,
            self.tenant,
            self.approach,
            self.operation,
            self.base_rows,
            self.batch_size,
            self.run_index,
            self.seconds,
            self.rows_changed,
            self.rows_per_sec,
            self.ms_per_row,
            self.db_file_size_bytes,
            self.duckdb_version,
        ]


RESULT_COLUMNS = [
    "run_id",
    "dataset",
    "tenant",
    "approach",
    "operation",
    "base_rows",
    "batch_size",
    "run_index",
    "seconds",
    "rows_changed",
    "rows_per_sec",
    "ms_per_row",
    "db_file_size_bytes",
    "duckdb_version",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark incremental FTS trigger updates against default FTS rebuilds."
    )
    parser.add_argument("--duckdb", type=Path, default=DEFAULT_DUCKDB)
    parser.add_argument("--database", type=Path, default=DEFAULT_DB)
    parser.add_argument("--fts-extension", type=Path, default=DEFAULT_FTS_EXTENSION)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument(
        "--dataset-kind",
        choices=[DATASET_KIND_EVAL_JSON, DATASET_KIND_DATAGEN_JSONL],
        default=DATASET_KIND_EVAL_JSON,
        help="Input data shape. eval-json is the checked-in search-evaluation JSON array; datagen-jsonl is data-generator items.jsonl.",
    )
    parser.add_argument("--results-csv", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument(
        "--storage-version",
        default="v2.0.0",
        help="DuckDB storage version for newly created benchmark databases.",
    )
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument(
        "--statement-timeout",
        type=int,
        default=60,
        help="Seconds to wait for a single DuckDB statement group before failing.",
    )
    parser.add_argument(
        "--batch-sizes",
        default=",".join(str(x) for x in DEFAULT_BATCH_SIZES),
        help="Comma-separated batch sizes.",
    )
    parser.add_argument(
        "--operations",
        default=",".join(DEFAULT_OPERATIONS),
        help=(
            "Comma-separated operations. Choices: "
            + ", ".join(ALL_OPERATIONS)
            + ". mixed is insert+delete in one transaction."
        ),
    )
    parser.add_argument(
        "--approaches",
        default=",".join(DEFAULT_APPROACHES),
        help="Comma-separated approaches. Choices: default_rebuild, incremental.",
    )
    parser.add_argument(
        "--skip-build-index-measurements",
        action="store_true",
        help="Skip timed initial index-build measurements while still building indexes for update scenarios.",
    )
    parser.add_argument(
        "--tenants",
        default=",".join(DEFAULT_TENANTS),
        help="Comma-separated tenant labels. Use account_<id>, or combined.",
    )
    parser.add_argument(
        "--recreate-db",
        action="store_true",
        help="Delete the benchmark database before running.",
    )
    parser.add_argument(
        "--clear-results",
        action="store_true",
        help="Clear bench_fts.results before appending this run.",
    )
    parser.add_argument(
        "--verify-index-counts",
        action="store_true",
        help="Also count FTS internal docs tables after each scenario. Useful for diagnostics, but can hang with current FTS builds.",
    )
    parser.add_argument(
        "--equality-checks",
        action="store_true",
        help="Run non-timed default-vs-incremental index equality checks after staging.",
    )
    parser.add_argument(
        "--skip-benchmarks",
        action="store_true",
        help="Prepare staging and optional equality checks without running timed benchmark scenarios.",
    )
    return parser.parse_args()


def get_duckdb_version(duckdb: Path) -> str:
    completed = subprocess.run(
        [str(duckdb), "-version"],
        cwd=str(ROOT),
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return completed.stdout.strip()


def validate_args(args: argparse.Namespace) -> tuple[list[int], list[str], list[str], list[str]]:
    for path in [args.duckdb, args.fts_extension, args.data]:
        if not path.exists():
            raise FileNotFoundError(path)
    if args.runs < 1:
        raise ValueError("--runs must be >= 1")
    batch_sizes = [int(x) for x in args.batch_sizes.split(",") if x.strip()]
    if not batch_sizes or any(x <= 0 for x in batch_sizes):
        raise ValueError("--batch-sizes must contain positive integers")
    operations = [x.strip() for x in args.operations.split(",") if x.strip()]
    invalid_operations = sorted(set(operations) - set(ALL_OPERATIONS))
    if invalid_operations:
        raise ValueError(f"Unknown operations: {', '.join(invalid_operations)}")
    approaches = [x.strip() for x in args.approaches.split(",") if x.strip()]
    invalid_approaches = sorted(set(approaches) - set(DEFAULT_APPROACHES))
    if invalid_approaches:
        raise ValueError(f"Unknown approaches: {', '.join(invalid_approaches)}")
    tenants = [x.strip() for x in args.tenants.split(",") if x.strip()]
    if args.dataset_kind == DATASET_KIND_EVAL_JSON:
        valid_tenants = set(DEFAULT_TENANTS)
        invalid = sorted(set(tenants) - valid_tenants)
        if invalid:
            raise ValueError(f"Unknown tenant labels for eval-json: {', '.join(invalid)}")
    else:
        invalid = [tenant for tenant in tenants if tenant != "combined" and not tenant.startswith("account_")]
        if invalid:
            raise ValueError(f"datagen-jsonl tenant labels must be 'combined' or 'account_<id>': {', '.join(invalid)}")
    return batch_sizes, tenants, operations, approaches


def prepare_staging(
    con: DuckDBProcess,
    data_path: Path,
    batch_sizes: list[int],
    dataset_kind: str,
) -> None:
    print("Preparing staging tables...", flush=True)
    size_values = ", ".join(f"({x})" for x in batch_sizes)
    if dataset_kind == DATASET_KIND_EVAL_JSON:
        read_raw_items_sql = f"""
        CREATE OR REPLACE TABLE bench_fts.raw_items AS
        SELECT *
        FROM read_json_auto({sql_string(data_path)});
        """
        create_base_docs_sql = """
        CREATE OR REPLACE TABLE bench_fts.base_docs AS
        SELECT
            id::VARCHAR AS id,
            account_id::BIGINT AS account_id,
            board_id::BIGINT AS board_id,
            NULLIF(
                regexp_replace(
                    concat_ws(
                        ' ',
                        name::VARCHAR,
                        description::VARCHAR,
                        board_name::VARCHAR,
                        group_name::VARCHAR,
                        array_to_string(column_values_index, ' ')
                    ),
                    '\\s+',
                    ' ',
                    'g'
                ),
                ''
            ) AS body
        FROM bench_fts.raw_items
        WHERE id IS NOT NULL;
        """
        tenant_docs_filter = "WHERE account_id IN (28132365, 31245546)"
    elif dataset_kind == DATASET_KIND_DATAGEN_JSONL:
        read_raw_items_sql = f"""
        CREATE OR REPLACE TABLE bench_fts.raw_items AS
        SELECT *
        FROM read_json_auto({sql_string(data_path)}, format='newline_delimited');
        """
        create_base_docs_sql = """
        CREATE OR REPLACE TABLE bench_fts.base_docs AS
        SELECT
            item_id::VARCHAR AS id,
            account_id::BIGINT AS account_id,
            board_id::BIGINT AS board_id,
            NULLIF(
                regexp_replace(
                    concat_ws(
                        ' ',
                        name::VARCHAR,
                        array_to_string(text_columns, ' '),
                        array_to_string(long_text_columns, ' ')
                    ),
                    '\\s+',
                    ' ',
                    'g'
                ),
                ''
            ) AS body
        FROM bench_fts.raw_items
        WHERE item_id IS NOT NULL;
        """
        tenant_docs_filter = ""
    else:
        raise ValueError(dataset_kind)

    con.run(
        f"""
        CREATE SCHEMA IF NOT EXISTS bench_fts;

        {read_raw_items_sql}

        {create_base_docs_sql}

        CREATE OR REPLACE TABLE bench_fts.tenant_docs AS
        SELECT 'account_' || account_id::VARCHAR AS tenant_label, *
        FROM bench_fts.base_docs
        {tenant_docs_filter}
        UNION ALL
        SELECT 'combined' AS tenant_label, *
        FROM bench_fts.base_docs;

        CREATE OR REPLACE TABLE bench_fts.insert_docs AS
        WITH sizes(batch_size) AS (VALUES {size_values}),
        numbered AS (
            SELECT
                tenant_label,
                id,
                account_id,
                board_id,
                body,
                row_number() OVER (PARTITION BY tenant_label ORDER BY id) AS rn
            FROM bench_fts.tenant_docs
        )
        SELECT
            tenant_label,
            batch_size,
            CAST(floor(((rn - 1)::DOUBLE) / batch_size) + 1 AS BIGINT) AS batch_id,
            tenant_label || '_ins_' || batch_size::VARCHAR || '_' || rn::VARCHAR || '_' || id AS id,
            account_id,
            board_id,
            body || ' benchmark inserted document ' || rn::VARCHAR AS body
        FROM numbered
        CROSS JOIN sizes;

        CREATE OR REPLACE TABLE bench_fts.delete_docs AS
        WITH sizes(batch_size) AS (VALUES {size_values}),
        numbered AS (
            SELECT
                tenant_label,
                id,
                row_number() OVER (PARTITION BY tenant_label ORDER BY id) AS rn
            FROM bench_fts.tenant_docs
        )
        SELECT
            tenant_label,
            batch_size,
            CAST(floor(((rn - 1)::DOUBLE) / batch_size) + 1 AS BIGINT) AS batch_id,
            id
        FROM numbered
        CROSS JOIN sizes;

        CREATE TABLE IF NOT EXISTS bench_fts.results (
            run_id VARCHAR,
            dataset VARCHAR,
            tenant VARCHAR,
            approach VARCHAR,
            operation VARCHAR,
            base_rows BIGINT,
            batch_size BIGINT,
            run_index BIGINT,
            seconds DOUBLE,
            rows_changed BIGINT,
            rows_per_sec DOUBLE,
            ms_per_row DOUBLE,
            db_file_size_bytes BIGINT,
            duckdb_version VARCHAR
        );
        """
    )


def clear_results(con: DuckDBProcess) -> None:
    con.run("DELETE FROM bench_fts.results;")


def scalar_int(con: DuckDBProcess, sql: str) -> int:
    _, lines = con.run(sql)
    row = csv_first_row(lines)
    if not row:
        raise RuntimeError(f"Expected scalar result from: {sql}")
    return int(row[0])


def reset_bench_docs(con: DuckDBProcess, tenant: str, table_name: str) -> None:
    con.run(
        f"""
        DROP TABLE IF EXISTS {table_name} CASCADE;
        DROP SCHEMA IF EXISTS fts_main_{table_name} CASCADE;

        CREATE TABLE {table_name} (
            id VARCHAR NOT NULL,
            account_id BIGINT,
            board_id BIGINT,
            body VARCHAR
        );

        INSERT INTO {table_name}
        SELECT id, account_id, board_id, body
        FROM bench_fts.tenant_docs
        WHERE tenant_label = {sql_string(tenant)};
        """
    )


def create_index_sql(table_name: str, approach: str, overwrite: bool = False) -> str:
    args = [
        sql_string(table_name),
        "'id'",
        "'body'",
        "stemmer='porter'",
        "stopwords='none'",
    ]
    if overwrite:
        args.append("overwrite=true")
    if approach == "incremental":
        args.append("incremental=true")
    return f"PRAGMA create_fts_index({', '.join(args)});"


def setup_indexed_table(
    con: DuckDBProcess,
    tenant: str,
    approach: str,
    table_name: str,
    base_rows: int,
    verify_index_counts: bool,
) -> None:
    reset_bench_docs(con, tenant, table_name)
    con.run(create_index_sql(table_name, approach))
    if verify_index_counts:
        verify_counts(
            con,
            table_name=table_name,
            expected_docs=base_rows,
            verify_index_counts=verify_index_counts,
        )


def cleanup_bench_table(con: DuckDBProcess, table_name: str) -> None:
    con.run(
        f"""
        DROP TABLE IF EXISTS {table_name} CASCADE;
        DROP SCHEMA IF EXISTS fts_main_{table_name} CASCADE;
        """
    )


def verify_counts(
    con: DuckDBProcess, table_name: str, expected_docs: int, verify_index_counts: bool
) -> None:
    if verify_index_counts:
        query = f"""
            SELECT
                (SELECT count(*) FROM {table_name}) AS source_count,
                (SELECT count(*) FROM fts_main_{table_name}.docs) AS index_count;
            """
    else:
        query = f"SELECT count(*) AS source_count, count(*) AS index_count FROM {table_name};"
    _, lines = con.run(query)
    row = csv_first_row(lines)
    if len(row) != 2:
        raise RuntimeError("Could not read verification counts")
    source_count, index_count = int(row[0]), int(row[1])
    if source_count != expected_docs or index_count != expected_docs:
        raise RuntimeError(
            f"Verification failed: source={source_count}, index={index_count}, expected={expected_docs}"
        )


def count_mutation_rows(con: DuckDBProcess, table: str, tenant: str, batch_size: int) -> int:
    return scalar_int(
        con,
        f"""
        SELECT count(*)
        FROM bench_fts.{table}
        WHERE tenant_label = {sql_string(tenant)}
          AND batch_size = {batch_size}
          AND batch_id = 1;
        """,
    )


def collect_mutation_counts(
    base_counts: dict[str, int], tenants: list[str], batch_sizes: list[int]
) -> dict[tuple[str, str, int], int]:
    counts: dict[tuple[str, str, int], int] = {}
    for tenant in tenants:
        for batch_size in batch_sizes:
            rows_in_first_batch = min(batch_size, base_counts[tenant])
            counts[(tenant, "insert", batch_size)] = rows_in_first_batch
            counts[(tenant, "delete", batch_size)] = rows_in_first_batch
    return counts


def collect_base_counts(con: DuckDBProcess, tenants: list[str]) -> dict[str, int]:
    tenant_list = ", ".join(sql_string(tenant) for tenant in tenants)
    _, lines = con.run(
        f"""
        SELECT tenant_label, count(*) AS row_count
        FROM bench_fts.tenant_docs
        WHERE tenant_label IN ({tenant_list})
        GROUP BY tenant_label;
        """
    )
    counts = {row[0]: int(row[1]) for row in csv.reader(lines)}
    missing = sorted(set(tenants) - set(counts))
    if missing:
        raise RuntimeError(f"No base rows found for tenants: {', '.join(missing)}")
    return counts


def timed_sql_for(operation: str, approach: str, tenant: str, batch_size: int, table_name: str) -> str:
    tenant_filter = sql_string(tenant)
    insert_sql = f"""
        INSERT INTO {table_name}
        SELECT id, account_id, board_id, body
        FROM bench_fts.insert_docs
        WHERE tenant_label = {tenant_filter}
          AND batch_size = {batch_size}
          AND batch_id = 1
    """
    delete_sql = f"""
        DELETE FROM {table_name}
        WHERE id IN (
            SELECT id
            FROM bench_fts.delete_docs
            WHERE tenant_label = {tenant_filter}
              AND batch_size = {batch_size}
              AND batch_id = 1
        )
    """
    if operation == "insert":
        sql = insert_sql + ";"
    elif operation == "delete":
        sql = delete_sql + ";"
    elif operation == "mixed":
        sql = f"BEGIN TRANSACTION; {insert_sql}; {delete_sql}; COMMIT;"
    elif operation == "mixed_no_txn":
        sql = insert_sql + ";\n" + delete_sql + ";"
    elif operation == "mixed_delete_insert":
        sql = f"BEGIN TRANSACTION; {delete_sql}; {insert_sql}; COMMIT;"
    else:
        raise ValueError(operation)

    if approach == "default_rebuild":
        sql += "\n" + create_index_sql(table_name, "default_rebuild", overwrite=True)
    return sql


def apply_mutation_sql(operation: str, tenant: str, batch_size: int, table_name: str) -> str:
    tenant_filter = sql_string(tenant)
    insert_sql = f"""
        INSERT INTO {table_name}
        SELECT id, account_id, board_id, body
        FROM bench_fts.insert_docs
        WHERE tenant_label = {tenant_filter}
          AND batch_size = {batch_size}
          AND batch_id = 1
    """
    delete_sql = f"""
        DELETE FROM {table_name}
        WHERE id IN (
            SELECT id
            FROM bench_fts.delete_docs
            WHERE tenant_label = {tenant_filter}
              AND batch_size = {batch_size}
              AND batch_id = 1
        )
    """
    if operation == "insert":
        return insert_sql + ";"
    if operation == "delete":
        return delete_sql + ";"
    if operation == "mixed":
        return f"BEGIN TRANSACTION; {insert_sql}; {delete_sql}; COMMIT;"
    if operation == "mixed_no_txn":
        return insert_sql + ";\n" + delete_sql + ";"
    if operation == "mixed_delete_insert":
        return f"BEGIN TRANSACTION; {delete_sql}; {insert_sql}; COMMIT;"
    raise ValueError(operation)


def equality_diff_sql(default_table: str, incremental_table: str) -> str:
    default_schema = f"fts_main_{default_table}"
    incremental_schema = f"fts_main_{incremental_table}"
    return f"""
        WITH
        default_source AS (
            SELECT id, account_id, board_id, body FROM {default_table}
        ),
        incremental_source AS (
            SELECT id, account_id, board_id, body FROM {incremental_table}
        ),
        default_docs AS (
            SELECT name, len FROM {default_schema}.docs
        ),
        incremental_docs AS (
            SELECT name, len FROM {incremental_schema}.docs
        ),
        default_terms AS (
            SELECT docs.name, fields.field, dict.term, count(*) AS tf
            FROM {default_schema}.terms AS terms
            JOIN {default_schema}.docs AS docs ON docs.docid = terms.docid
            JOIN {default_schema}.fields AS fields ON fields.fieldid = terms.fieldid
            JOIN {default_schema}.dict AS dict ON dict.termid = terms.termid
            GROUP BY docs.name, fields.field, dict.term
        ),
        incremental_terms AS (
            SELECT docs.name, fields.field, dict.term, count(*) AS tf
            FROM {incremental_schema}.terms AS terms
            JOIN {incremental_schema}.docs AS docs ON docs.docid = terms.docid
            JOIN {incremental_schema}.fields AS fields ON fields.fieldid = terms.fieldid
            JOIN {incremental_schema}.dict AS dict ON dict.termid = terms.termid
            GROUP BY docs.name, fields.field, dict.term
        ),
        default_dict AS (
            SELECT term, df FROM {default_schema}.dict
        ),
        incremental_dict AS (
            SELECT term, df FROM {incremental_schema}.dict
        ),
        default_stats AS (
            SELECT num_docs, avgdl FROM {default_schema}.stats
        ),
        incremental_stats AS (
            SELECT num_docs, avgdl FROM {incremental_schema}.stats
        ),
        diffs AS (
            SELECT 'source default-incremental' AS check_name, count(*) AS diff_count
            FROM (SELECT * FROM default_source EXCEPT SELECT * FROM incremental_source)
            UNION ALL
            SELECT 'source incremental-default', count(*)
            FROM (SELECT * FROM incremental_source EXCEPT SELECT * FROM default_source)
            UNION ALL
            SELECT 'docs default-incremental', count(*)
            FROM (SELECT * FROM default_docs EXCEPT SELECT * FROM incremental_docs)
            UNION ALL
            SELECT 'docs incremental-default', count(*)
            FROM (SELECT * FROM incremental_docs EXCEPT SELECT * FROM default_docs)
            UNION ALL
            SELECT 'terms default-incremental', count(*)
            FROM (SELECT * FROM default_terms EXCEPT SELECT * FROM incremental_terms)
            UNION ALL
            SELECT 'terms incremental-default', count(*)
            FROM (SELECT * FROM incremental_terms EXCEPT SELECT * FROM default_terms)
            UNION ALL
            SELECT 'dict default-incremental', count(*)
            FROM (SELECT * FROM default_dict EXCEPT SELECT * FROM incremental_dict)
            UNION ALL
            SELECT 'dict incremental-default', count(*)
            FROM (SELECT * FROM incremental_dict EXCEPT SELECT * FROM default_dict)
            UNION ALL
            SELECT 'stats default-incremental', count(*)
            FROM (SELECT * FROM default_stats EXCEPT SELECT * FROM incremental_stats)
            UNION ALL
            SELECT 'stats incremental-default', count(*)
            FROM (SELECT * FROM incremental_stats EXCEPT SELECT * FROM default_stats)
        )
        SELECT check_name, diff_count
        FROM diffs
        WHERE diff_count <> 0
        ORDER BY check_name;
        """


def run_equality_checks(
    *,
    duckdb: Path,
    database: Path,
    fts_extension: Path,
    storage_version: str,
    statement_timeout: int,
    tenants: list[str],
    batch_sizes: list[int],
) -> None:
    print("Running default-vs-incremental equality checks...", flush=True)
    check_counter = 0
    failures: list[str] = []
    for tenant in tenants:
        for operation in ["insert", "delete", "mixed"]:
            for batch_size in batch_sizes:
                check_counter += 1
                default_table = f"eq_default_{check_counter}"
                incremental_table = f"eq_incremental_{check_counter}"
                print(f"  {tenant} {operation} batch={batch_size}", flush=True)
                con = DuckDBProcess(duckdb, database, fts_extension, storage_version, statement_timeout)
                try:
                    reset_bench_docs(con, tenant, default_table)
                    reset_bench_docs(con, tenant, incremental_table)
                    con.run(create_index_sql(default_table, "default_rebuild"))
                    con.run(create_index_sql(incremental_table, "incremental"))
                    con.run(apply_mutation_sql(operation, tenant, batch_size, default_table))
                    con.run(apply_mutation_sql(operation, tenant, batch_size, incremental_table))
                    con.run(create_index_sql(default_table, "default_rebuild", overwrite=True))
                    _, lines = con.run(equality_diff_sql(default_table, incremental_table))
                    if lines:
                        failures.append(
                            f"{tenant} {operation} batch={batch_size}:\n{''.join(lines)}"
                        )
                finally:
                    for table_name in [default_table, incremental_table]:
                        try:
                            cleanup_bench_table(con, table_name)
                        except Exception as cleanup_error:
                            print(f"warning: cleanup failed for {table_name}: {cleanup_error}", file=sys.stderr)
                    con.close()
    if failures:
        raise RuntimeError("Index equality checks failed:\n" + "\n".join(failures))
    print("Equality checks passed.", flush=True)


def insert_result(con: DuckDBProcess, row: ResultRow) -> None:
    values = []
    for value in row.as_list():
        if isinstance(value, str):
            values.append(sql_string(value))
        elif value is None:
            values.append("NULL")
        else:
            values.append(str(value))
    con.run(f"INSERT INTO bench_fts.results VALUES ({', '.join(values)});")


def append_csv(path: Path, rows: list[ResultRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists()
    with path.open("a", newline="") as handle:
        writer = csv.writer(handle)
        if write_header:
            writer.writerow(RESULT_COLUMNS)
        for row in rows:
            writer.writerow(row.as_list())


def record_result(
    *,
    con: DuckDBProcess,
    rows: list[ResultRow],
    run_id: str,
    dataset: str,
    tenant: str,
    approach: str,
    operation: str,
    base_rows: int,
    batch_size: int | None,
    run_index: int,
    seconds: float,
    rows_changed: int,
    database: Path,
    duckdb_version: str,
) -> None:
    rows_per_sec = rows_changed / seconds if seconds > 0 else 0.0
    ms_per_row = (seconds * 1000.0 / rows_changed) if rows_changed else 0.0
    result = ResultRow(
        run_id=run_id,
        dataset=dataset,
        tenant=tenant,
        approach=approach,
        operation=operation,
        base_rows=base_rows,
        batch_size=batch_size,
        run_index=run_index,
        seconds=seconds,
        rows_changed=rows_changed,
        rows_per_sec=rows_per_sec,
        ms_per_row=ms_per_row,
        db_file_size_bytes=database.stat().st_size if database.exists() else 0,
        duckdb_version=duckdb_version,
    )
    rows.append(result)
    insert_result(con, result)
    print(
        f"{operation:12} {approach:15} {tenant:16} "
        f"batch={str(batch_size):>4} run={run_index:<2} "
        f"{seconds:.6f}s {rows_per_sec:.1f} rows/s"
    )


def run_benchmarks(
    *,
    duckdb: Path,
    database: Path,
    fts_extension: Path,
    storage_version: str,
    statement_timeout: int,
    tenants: list[str],
    batch_sizes: list[int],
    runs: int,
    operations: list[str],
    approaches: list[str],
    skip_build_index_measurements: bool,
    dataset: str,
    duckdb_version: str,
    run_id: str,
    base_counts: dict[str, int],
    mutation_counts: dict[tuple[str, str, int], int],
    verify_index_counts: bool,
) -> list[ResultRow]:
    rows: list[ResultRow] = []
    table_counter = 0

    def next_table_name() -> str:
        nonlocal table_counter
        table_counter += 1
        return f"bench_docs_{table_counter}"

    def open_connection() -> DuckDBProcess:
        return DuckDBProcess(duckdb, database, fts_extension, storage_version, statement_timeout)

    for tenant in tenants:
        print(f"Running tenant {tenant}...", flush=True)
        if not skip_build_index_measurements:
            for approach in approaches:
                for run_index in range(1, runs + 1):
                    print(f"  build_index {approach} run={run_index}", flush=True)
                    table_name = next_table_name()
                    con = open_connection()
                    try:
                        base_rows = base_counts[tenant]
                        reset_bench_docs(con, tenant, table_name)
                        elapsed, _ = con.run(create_index_sql(table_name, approach), timed=True)
                        assert elapsed is not None
                        if verify_index_counts:
                            verify_counts(
                                con,
                                table_name=table_name,
                                expected_docs=base_rows,
                                verify_index_counts=verify_index_counts,
                            )
                        record_result(
                            con=con,
                            rows=rows,
                            run_id=run_id,
                            dataset=dataset,
                            tenant=tenant,
                            approach=approach,
                            operation="build_index",
                            base_rows=base_rows,
                            batch_size=None,
                            run_index=run_index,
                            seconds=elapsed,
                            rows_changed=base_rows,
                            database=database,
                            duckdb_version=duckdb_version,
                        )
                    finally:
                        try:
                            cleanup_bench_table(con, table_name)
                        except Exception as cleanup_error:
                            print(f"warning: cleanup failed for {table_name}: {cleanup_error}", file=sys.stderr)
                        con.close()

        for operation in operations:
            for batch_size in batch_sizes:
                insert_count = mutation_counts[(tenant, "insert", batch_size)]
                delete_count = mutation_counts[(tenant, "delete", batch_size)]
                if operation == "insert":
                    rows_changed = insert_count
                elif operation == "delete":
                    rows_changed = delete_count
                elif operation in {"mixed", "mixed_no_txn", "mixed_delete_insert"}:
                    rows_changed = insert_count + delete_count
                else:
                    raise ValueError(operation)

                for approach in approaches:
                    for run_index in range(1, runs + 1):
                        print(
                            f"  {operation} {approach} batch={batch_size} run={run_index}",
                            flush=True,
                        )
                        table_name = next_table_name()
                        con = open_connection()
                        try:
                            base_rows = base_counts[tenant]
                            setup_indexed_table(
                                con,
                                tenant,
                                "incremental" if approach == "incremental" else "default_rebuild",
                                table_name,
                                base_rows,
                                verify_index_counts,
                            )
                            elapsed, _ = con.run(
                                timed_sql_for(operation, approach, tenant, batch_size, table_name),
                                timed=True,
                            )
                            assert elapsed is not None
                            expected_docs = base_rows
                            if operation == "insert":
                                expected_docs += insert_count
                            elif operation == "delete":
                                expected_docs -= delete_count
                            if verify_index_counts:
                                verify_counts(
                                    con,
                                    table_name=table_name,
                                    expected_docs=expected_docs,
                                    verify_index_counts=verify_index_counts,
                                )
                            record_result(
                                con=con,
                                rows=rows,
                                run_id=run_id,
                                dataset=dataset,
                                tenant=tenant,
                                approach=approach,
                                operation=operation,
                                base_rows=base_rows,
                                batch_size=batch_size,
                                run_index=run_index,
                                seconds=elapsed,
                                rows_changed=rows_changed,
                                database=database,
                                duckdb_version=duckdb_version,
                            )
                        finally:
                            try:
                                cleanup_bench_table(con, table_name)
                            except Exception as cleanup_error:
                                print(f"warning: cleanup failed for {table_name}: {cleanup_error}", file=sys.stderr)
                            con.close()
    return rows


def print_summary(rows: list[ResultRow]) -> None:
    print("\nMedian timings by scenario:")
    grouped: dict[tuple[str, str, str, int | None], list[ResultRow]] = {}
    for row in rows:
        key = (row.tenant, row.operation, row.approach, row.batch_size)
        grouped.setdefault(key, []).append(row)

    for key in sorted(grouped):
        values = sorted(row.seconds for row in grouped[key])
        median = values[len(values) // 2]
        tenant, operation, approach, batch_size = key
        print(
            f"{tenant:16} {operation:12} {approach:15} "
            f"batch={str(batch_size):>4} median={median:.6f}s"
        )


def main() -> int:
    args = parse_args()
    batch_sizes, tenants, operations, approaches = validate_args(args)
    if args.recreate_db:
        for path in [args.database, Path(str(args.database) + ".wal")]:
            if path.exists():
                path.unlink()

    run_id = time.strftime("%Y%m%d-%H%M%S")
    duckdb_version = get_duckdb_version(args.duckdb)
    con = DuckDBProcess(
        args.duckdb,
        args.database,
        args.fts_extension,
        args.storage_version,
        args.statement_timeout,
    )
    try:
        prepare_staging(con, args.data, batch_sizes, args.dataset_kind)
        if args.clear_results:
            clear_results(con)
            if args.results_csv.exists():
                args.results_csv.unlink()
        base_counts = collect_base_counts(con, tenants)
        mutation_counts = collect_mutation_counts(base_counts, tenants, batch_sizes)
    finally:
        con.close()

    if args.equality_checks:
        run_equality_checks(
            duckdb=args.duckdb,
            database=args.database,
            fts_extension=args.fts_extension,
            storage_version=args.storage_version,
            statement_timeout=args.statement_timeout,
            tenants=tenants,
            batch_sizes=batch_sizes,
        )

    if args.skip_benchmarks:
        return 0

    rows = run_benchmarks(
        duckdb=args.duckdb,
        database=args.database,
        fts_extension=args.fts_extension,
        storage_version=args.storage_version,
        statement_timeout=args.statement_timeout,
        tenants=tenants,
        batch_sizes=batch_sizes,
        runs=args.runs,
        operations=operations,
        approaches=approaches,
        skip_build_index_measurements=args.skip_build_index_measurements,
        dataset=args.data.name,
        duckdb_version=duckdb_version,
        run_id=run_id,
        base_counts=base_counts,
        mutation_counts=mutation_counts,
        verify_index_counts=args.verify_index_counts,
    )
    try:
        append_csv(args.results_csv, rows)
        print_summary(rows)
        print(f"\nResults appended to {args.results_csv}")
        print("DuckDB results table: bench_fts.results")
        return 0
    except Exception:
        if rows:
            append_csv(args.results_csv, rows)
        raise


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
