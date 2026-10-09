"""Gateway rules. Every rule here exists because something had to be refused.

The milestone's "done when" line names three: a DROP, a cross join with no
predicate, and an unknown table. The rest were added alongside them because a
rule that is only tested for its headline case tends to have a neighbour that
slips through.
"""

import sqlite3
from pathlib import Path

import duckdb
import pytest

from agent.evidence import EvidenceStore
from agent.gateway import Gateway

WAREHOUSE = "data/warehouse.duckdb"


@pytest.fixture
def gw(tmp_path):
    return Gateway(WAREHOUSE, store=EvidenceStore(tmp_path / "evidence.jsonl"))


# --- the three the milestone names ---------------------------------------

def test_drop_is_refused(gw):
    decision = gw.authorize("DROP TABLE fct_orders")
    assert not decision.allowed
    assert decision.stage == "authorize"


def test_cross_join_without_predicate_is_refused(gw):
    decision = gw.authorize("SELECT * FROM fct_orders, dim_customer")
    assert not decision.allowed
    assert decision.stage == "lint"
    assert "cross join" in decision.reason.lower()


def test_unknown_table_is_refused(gw):
    decision = gw.authorize("SELECT * FROM fct_order")  # singular: a typo
    assert not decision.allowed
    assert decision.stage == "authorize"
    assert "unknown table" in decision.reason.lower()
    # The refusal has to be useful enough to repair from.
    assert "fct_orders" in decision.reason


# --- the neighbours ------------------------------------------------------

@pytest.mark.parametrize("sql", [
    "INSERT INTO fct_orders VALUES (1)",
    "UPDATE fct_orders SET amount = 0",
    "DELETE FROM fct_orders",
    "CREATE TABLE x (a INT)",
    "ALTER TABLE fct_orders ADD COLUMN x INT",
    "ATTACH 'other.duckdb'",
])
def test_non_select_statements_are_refused(gw, sql):
    assert not gw.authorize(sql).allowed


def test_multiple_statements_are_refused(gw):
    decision = gw.authorize("SELECT 1; SELECT 2")
    assert not decision.allowed
    assert decision.stage == "parse"


def test_explicit_cross_join_without_where_is_refused(gw):
    decision = gw.authorize("SELECT * FROM fct_orders CROSS JOIN dim_product")
    assert not decision.allowed
    assert decision.stage == "lint"


def test_comma_join_with_where_is_allowed(gw):
    """An implicit join is still a join. Refusing it would be wrong."""
    decision = gw.authorize(
        "SELECT o.order_id FROM fct_orders o, dim_product p "
        "WHERE o.product_id = p.product_id"
    )
    assert decision.allowed


def test_cte_name_is_not_an_unknown_table(gw):
    decision = gw.authorize(
        "WITH recent AS (SELECT * FROM fct_orders) SELECT COUNT(*) FROM recent"
    )
    assert decision.allowed, decision.reason


def test_too_many_joins_is_refused(gw):
    joins = " ".join(
        f"JOIN dim_product p{i} ON p{i}.product_id = o.product_id" for i in range(8)
    )
    decision = gw.authorize(f"SELECT 1 FROM fct_orders o {joins}")
    assert not decision.allowed
    assert decision.stage == "cost"


# --- rewriting -----------------------------------------------------------

def test_limit_is_injected(gw):
    decision = gw.authorize("SELECT * FROM fct_orders")
    assert decision.allowed
    assert "LIMIT" in decision.sql.upper()


def test_oversized_limit_is_clamped(gw):
    decision = gw.authorize("SELECT * FROM fct_orders LIMIT 999999")
    assert decision.allowed
    assert "999999" not in decision.sql


def test_small_limit_is_left_alone(gw):
    decision = gw.authorize("SELECT * FROM fct_orders LIMIT 5")
    assert decision.allowed
    assert "5" in decision.sql


# --- the structural guarantee -------------------------------------------

def test_connection_is_read_only_whatever_sql_arrives(gw):
    """The validator above is a convenience. THIS is the line that holds.

    If every check in authorize() were deleted, the connection itself would
    still refuse to write.
    """
    con = gw._conn()
    try:
        for sql in ("DELETE FROM fct_orders",
                    "UPDATE fct_orders SET amount = 0",
                    "DROP TABLE dim_product",
                    "CREATE TABLE evil (a INT)"):
            with pytest.raises(duckdb.Error):
                con.execute(sql)
    finally:
        pass


# --- execution and evidence ---------------------------------------------

def test_successful_query_writes_evidence(gw):
    decision, record = gw.run_sql(
        "SELECT COUNT(*) AS n FROM fct_orders WHERE status = 'completed'"
    )
    assert decision.allowed
    assert record is not None
    assert record.id == "e1"
    assert record.row_count == 1
    assert record.sql_hash
    # a 1x1 result is addressable as a scalar, for report placeholders
    assert record.fields["n"] > 0


def test_refused_query_writes_no_evidence(gw):
    decision, record = gw.run_sql("DROP TABLE fct_orders")
    assert not decision.allowed
    assert record is None
    assert len(gw.store) == 0


def test_execution_error_is_returned_not_raised(gw):
    decision, record = gw.run_sql("SELECT no_such_column FROM fct_orders")
    assert not decision.allowed
    assert decision.stage == "execute"
    assert record is None


def test_evidence_ids_increment(gw):
    gw.run_sql("SELECT 1 AS a FROM fct_orders LIMIT 1")
    _, second = gw.run_sql("SELECT 2 AS b FROM fct_orders LIMIT 1")
    assert second.id == "e2"


def test_preview_never_returns_every_row(gw):
    _, record = gw.run_sql("SELECT order_id FROM fct_orders")
    preview = record.preview(limit=10)
    assert preview["rows_shown"] == 10
    assert preview["row_count"] > 10
    assert preview["truncated"] is True


def test_resolve_raises_on_a_missing_field(gw):
    _, record = gw.run_sql("SELECT COUNT(*) AS n FROM fct_orders")
    assert gw.store.resolve(record.id, "n") > 0
    with pytest.raises(KeyError):
        gw.store.resolve(record.id, "not_a_field")
    with pytest.raises(KeyError):
        gw.store.resolve("e999", "n")


# --- benchmark integrity -------------------------------------------------
#
# These exist because the gateway DID leak. read_only=True stops writes and
# says nothing about reads, and the table allowlist never fires because a table
# function is not a table. Seven of nine file-read attempts reached
# evals/gold/questions.jsonl and evals/seeded/planted.json before
# enable_external_access=false was added.

GOLD = "evals/gold/questions.jsonl"
PLANTED = "evals/seeded/planted.json"


@pytest.mark.parametrize("sql", [
    f"SELECT * FROM '{GOLD}'",
    f"SELECT * FROM read_json_auto('{GOLD}')",
    f"SELECT * FROM read_csv_auto('{GOLD}')",
    f"SELECT * FROM read_text('{GOLD}')",
    f"SELECT * FROM read_blob('{PLANTED}')",
    "SELECT * FROM glob('evals/gold/*')",
    f"SELECT (SELECT COUNT(*) FROM read_json_auto('{GOLD}')) AS n",
    f"WITH g AS (SELECT * FROM read_json_auto('{GOLD}')) SELECT COUNT(*) FROM g",
    "SELECT * FROM read_parquet('anything.parquet')",
])
def test_the_gold_set_is_unreachable(gw, sql):
    decision, record = gw.run_sql(sql)
    assert not decision.allowed, f"LEAK: {sql}"
    assert record is None


def test_external_access_cannot_be_switched_back_on(gw):
    """lock_configuration=true, so even a SET that got through would fail."""
    con = gw._conn()
    try:
        with pytest.raises(duckdb.Error):
            con.execute("SET enable_external_access=true")
    finally:
        pass


def test_lockdown_does_not_break_the_warehouse(gw):
    """The restriction has to be invisible to legitimate queries."""
    decision, record = gw.run_sql("SELECT COUNT(*) AS n FROM fct_orders")
    assert decision.allowed
    assert record.fields["n"] > 0


# --- statement timeouts --------------------------------------------------
#
# Added after a Spider sweep sat on one task for 26 minutes. MAX_ROWS bounds
# what comes BACK; it does nothing about how long the engine spends computing
# it. The agent had written a cartesian join on a 134k-row table.

def test_an_expensive_query_is_aborted(tmp_path):
    gw = Gateway(WAREHOUSE, store=EvidenceStore(tmp_path / "e.jsonl"), timeout=3)
    sql = (
        "WITH RECURSIVE t(i) AS "
        "(SELECT 1 UNION ALL SELECT i + 1 FROM t WHERE i < 2000000000) "
        "SELECT COUNT(*) AS n FROM t"
    )
    import time
    started = time.monotonic()
    decision, record = gw.run_sql(sql)
    elapsed = time.monotonic() - started

    assert not decision.allowed
    assert decision.stage == "execute"
    assert "too expensive" in decision.reason      # actionable for the model
    assert record is None                          # no evidence from a dead query
    assert elapsed < 15, f"watchdog did not fire ({elapsed:.1f}s)"


def test_the_timeout_does_not_slow_normal_queries(gw):
    decision, record = gw.run_sql("SELECT COUNT(*) AS n FROM fct_orders")
    assert decision.allowed
    assert record.fields["n"] == 590


def test_the_watchdog_does_not_leak_between_queries(gw):
    """A cancelled timer must not interrupt the NEXT query on a new connection."""
    gw.run_sql("SELECT 1 AS a FROM fct_orders LIMIT 1")
    for _ in range(3):
        decision, record = gw.run_sql("SELECT COUNT(*) AS n FROM fct_orders")
        assert decision.allowed, decision.reason


# --- the same gateway, the other engine ----------------------------------
#
# One gateway covers DuckDB and SQLite. These assert the policy and the
# integrity guarantees hold identically on the SQLite backend, because the
# alternative was a second gateway (evals/sandbox.py) with a second set of
# these tests that had to be kept in sync by hand.

SPIDER_DB = Path("evals/spider/databases/IPL.sqlite")
needs_spider = pytest.mark.skipif(
    not SPIDER_DB.exists(), reason="Spider databases not downloaded"
)


@pytest.fixture
def sqlite_gw(tmp_path):
    return Gateway(SPIDER_DB, store=EvidenceStore(tmp_path / "e.jsonl"), timeout=5)


@needs_spider
def test_backend_is_inferred_from_the_extension(sqlite_gw, gw):
    assert sqlite_gw.backend.name == "sqlite"
    assert gw.backend.name == "duckdb"


@needs_spider
def test_sqlite_schema_discovery_works(sqlite_gw):
    tables = sqlite_gw.describe_schema()
    assert tables
    assert all({"table", "columns", "row_count"} <= set(t) for t in tables)


@needs_spider
@pytest.mark.parametrize("sql,stage", [
    ("DROP TABLE Ball_by_Ball", "authorize"),
    ("ATTACH DATABASE 'x.sqlite' AS y", "authorize"),
    ("DELETE FROM Ball_by_Ball", "authorize"),
    ("SELECT * FROM no_such_table", "authorize"),
    ("SELECT * FROM Ball_by_Ball a, Ball_by_Ball b", "lint"),
])
def test_the_same_policy_applies_on_sqlite(sqlite_gw, sql, stage):
    decision = sqlite_gw.authorize(sql)
    assert not decision.allowed
    assert decision.stage == stage


@needs_spider
def test_sqlite_connection_cannot_write_or_reach_other_files(sqlite_gw):
    """The engine-level guarantees, with authorize() bypassed entirely."""
    con = sqlite_gw._conn()
    try:
        with pytest.raises(sqlite3.Error):
            con.execute("DELETE FROM Ball_by_Ball")
        with pytest.raises(sqlite3.Error):           # ATTACH reaches the gold answers
            con.execute("ATTACH DATABASE 'evals/spider/gold/x.jsonl' AS g")
        with pytest.raises(sqlite3.Error):           # no file-reading function exists
            con.execute("SELECT readfile('evals/gold/questions.jsonl')")
    finally:
        pass


@needs_spider
def test_sqlite_statement_timeout_fires(sqlite_gw):
    """A cartesian product that gets past lint must still be bounded."""
    import time
    sql = (
        "SELECT COUNT(*) FROM Ball_by_Ball a JOIN Ball_by_Ball b ON 1=1 "
        "JOIN Ball_by_Ball c ON 1=1 WHERE a.match_id > 0"
    )
    started = time.monotonic()
    decision, record = sqlite_gw.run_sql(sql)
    elapsed = time.monotonic() - started
    assert not decision.allowed
    assert record is None
    assert elapsed < 20, f"timeout did not fire ({elapsed:.1f}s)"


@needs_spider
def test_sqlite_normal_query_returns_evidence(sqlite_gw):
    decision, record = sqlite_gw.run_sql("SELECT COUNT(*) AS n FROM Ball_by_Ball")
    assert decision.allowed
    assert record.fields["n"] == 134703


def test_an_unknown_extension_is_refused_rather_than_guessed(tmp_path):
    odd = tmp_path / "mystery.bin"
    odd.write_bytes(b"")
    with pytest.raises(ValueError, match="cannot infer"):
        Gateway(odd)


# --- a dbt project's DuckDB file (Spider2-DBT) ---------------------------
#
# The DBT harness points the same gateway at the task's own .duckdb, which dbt
# builds into. Two things differ from the warehouse: models land in custom
# schemas (`+schema: staging` -> main_staging), and the agent's next step is a
# `dbt run` that needs the write lock -- so the gateway must not hold the file.

@pytest.fixture
def dbt_db(tmp_path):
    path = tmp_path / "project.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE raw_orders AS SELECT range AS id, range * 2 AS amt FROM range(50)")
    con.execute("CREATE SCHEMA main_staging")
    con.execute("CREATE TABLE main_staging.stg_orders AS SELECT * FROM raw_orders")
    con.close()
    return path


@pytest.fixture
def dbt_gw(dbt_db, tmp_path):
    gw = Gateway(dbt_db, store=EvidenceStore(tmp_path / "e.jsonl"), max_rows=20)
    yield gw
    gw.close()


def test_tables_in_custom_schemas_are_queryable(dbt_gw):
    assert "stg_orders" in dbt_gw.allowlist
    decision, record = dbt_gw.run_sql("SELECT COUNT(*) AS n FROM main_staging.stg_orders")
    assert decision.allowed, decision.reason
    assert record.rows == [[50]]


# --- schema discovery ----------------------------------------------------
#
# These four exist because of a measured failure, not a hypothesis. In run
# luna-20261009-030506, 169 of 361 gateway rejections were the agent asking
# information_schema what the database contained, and a further 142 execution
# errors were it guessing a bare name for a table that lives in a dbt schema.
# The sandbox has no describe tool, so refusing introspection left it with no
# way at all to learn what `dbt run` had just built.

def test_information_schema_is_readable(dbt_gw):
    decision, record = dbt_gw.run_sql(
        "SELECT table_schema, table_name FROM information_schema.tables "
        "WHERE table_schema NOT IN ('information_schema', 'pg_catalog')"
    )
    assert decision.allowed, decision.reason
    found = {(r[0], r[1]) for r in record.rows}
    assert ("main_staging", "stg_orders") in found
    assert ("main", "raw_orders") in found


def test_information_schema_columns_is_readable(dbt_gw):
    decision, record = dbt_gw.run_sql(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'stg_orders'"
    )
    assert decision.allowed, decision.reason
    assert {r[0] for r in record.rows} == {"id", "amt"}


def test_refusal_names_the_qualified_reference(dbt_gw):
    """A bare name is not enough to write the query that follows."""
    decision = dbt_gw.authorize("SELECT * FROM stg_order")  # singular: a typo
    assert not decision.allowed
    # Not just "stg_orders" -- the schema the model has to type with it.
    assert "main_staging.stg_orders" in decision.reason
    # A table in `main` keeps its bare form, because that is what works there.
    assert "raw_orders" in decision.reason


def test_describe_covers_every_user_schema(dbt_gw):
    tables = {t["table"]: t for t in dbt_gw.describe_schema()}
    assert "raw_orders" in tables                 # main stays implicit
    assert "main_staging.stg_orders" in tables    # anything else is qualified
    assert tables["main_staging.stg_orders"]["row_count"] == 50


def test_introspection_does_not_open_a_second_file(dbt_gw, dbt_db):
    """Readable catalog, still one database."""
    other = dbt_db.parent / "secret.duckdb"
    con = duckdb.connect(str(other))
    con.execute("CREATE TABLE answers AS SELECT 1 AS gold")
    con.close()
    decision, record = dbt_gw.run_sql(
        "SELECT table_catalog, table_name FROM information_schema.tables"
    )
    assert decision.allowed, decision.reason
    assert "answers" not in {r[1] for r in record.rows}


@needs_spider
def test_sqlite_master_is_readable(sqlite_gw):
    decision, record = sqlite_gw.run_sql(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    )
    assert decision.allowed, decision.reason
    assert record.row_count > 0


@pytest.mark.parametrize("sql", [
    "DROP TABLE raw_orders",
    "INSERT INTO raw_orders VALUES (1, 2)",
    "ATTACH 'other.duckdb' AS o",
    "COPY raw_orders TO 'out.csv'",
    "PRAGMA database_list",
    "SET enable_external_access = true",
    "SELECT 1; SELECT 2",
])
def test_dbt_target_refuses_everything_but_one_select(dbt_gw, sql):
    decision, record = dbt_gw.run_sql(sql)
    assert not decision.allowed
    assert record is None


def test_dbt_target_cannot_read_files(dbt_gw, dbt_db):
    other = dbt_db.parent / "gold.csv"
    other.write_text("a\n1\n", encoding="utf-8")
    decision, record = dbt_gw.run_sql(f"SELECT * FROM read_csv('{other.as_posix()}')")
    assert not decision.allowed
    assert record is None


def test_dbt_target_lock_configuration_holds(dbt_gw):
    con = dbt_gw._conn()
    with pytest.raises(duckdb.Error):
        con.execute("SET enable_external_access = true")


def test_dbt_target_limit_is_injected(dbt_gw):
    decision, record = dbt_gw.run_sql("SELECT * FROM raw_orders")
    assert "LIMIT 21" in decision.sql
    assert record.row_count == 20 and record.truncated


def test_dbt_target_releases_the_file_on_close(dbt_db, tmp_path):
    gw = Gateway(dbt_db, store=EvidenceStore(tmp_path / "e2.jsonl"))
    gw.run_sql("SELECT 1 AS x FROM raw_orders LIMIT 1")
    gw.close()
    # dbt needs a read-write handle next; this fails if one is still open.
    con = duckdb.connect(str(dbt_db))
    con.execute("CREATE TABLE built AS SELECT 1 AS x")
    con.close()
