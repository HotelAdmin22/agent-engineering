"""The only module that may open a database or execute SQL.

The model proposes SQL. This decides. Those are different jobs, and keeping
them in different modules is what makes the boundary real rather than a rule
written in a prompt -- the agent is never handed a connection object, so no
wording and no model decision can reach the warehouse another way.

The pipeline, in order:

    parse -> authorize -> lint -> rewrite -> cost -> execute -> evidence

Each stage can refuse, and a refusal is a returned Decision rather than a
raised exception, because the refusal text is the most useful thing the model
receives. "unknown table: fct_order" is what lets it fix the query next turn.

Two structural guarantees, both enforced rather than requested:

  1. The connection cannot write and cannot read other files. How that is done
     differs per engine and lives in backends.py.
  2. Every executed statement writes an immutable evidence record before its
     rows are returned to anyone.

One gateway covers DuckDB and SQLite. The policy above is dialect-parameterised
sqlglot, so only the connection, the timeout mechanism, the schema query and
the interrupt exception differ -- which is all backends.py holds. Before this
there were two of everything: this and evals/sandbox.py, each with its own
integrity tests.
"""

from dataclasses import dataclass, field
from pathlib import Path

import sqlglot
from sqlglot import expressions as exp

from ..evidence.store import EvidenceRecord, EvidenceStore

from . import backends

MAX_ROWS = 1000
MAX_JOINS = 6

# MAX_ROWS bounds what comes back; it does nothing about how long the engine
# spends computing it. A cartesian join on a 134k-row table ran for 26 minutes
# before this existed. The mechanism is per-engine; see backends.py.
STATEMENT_TIMEOUT = 30.0


@dataclass
class Decision:
    """What the gateway decided, and why."""

    allowed: bool
    reason: str = ""
    stage: str = ""
    sql: str = ""            # the rewritten SQL that would actually run
    tables: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.allowed


def _refuse(stage: str, reason: str) -> Decision:
    return Decision(allowed=False, stage=stage, reason=reason)


class Gateway:
    """Read-only SQL execution against one DuckDB file."""

    def __init__(
        self,
        db_path: Path | str,
        store: EvidenceStore | None = None,
        allowlist: set[str] | None = None,
        max_rows: int = MAX_ROWS,
        max_joins: int = MAX_JOINS,
        timeout: float = STATEMENT_TIMEOUT,
        backend: str | None = None,
    ):
        self.db_path = Path(db_path)
        if not self.db_path.exists():
            raise FileNotFoundError(f"warehouse not found: {self.db_path}")
        # `store or EvidenceStore()` would be wrong: EvidenceStore defines
        # __len__, so an empty one is falsy and the caller's store would be
        # silently replaced -- evidence would land somewhere nobody reads.
        self.store = store if store is not None else EvidenceStore()
        self.max_rows = max_rows
        self.max_joins = max_joins
        self.timeout = timeout
        # Inferred from the file extension unless named: .duckdb or .sqlite.
        self.backend = backends.for_path(self.db_path, backend)
        self._con = None
        self.allowlist = allowlist if allowlist is not None else self._discover_tables()

    # --- connection ------------------------------------------------------

    def _conn(self):
        """One connection for the gateway's lifetime, opened on first use.

        It used to be one per query, which meant one watchdog thread per query
        on a handle that was about to close. A timer firing a moment late
        called interrupt() on a dead connection and crashed the interpreter.
        A connection that outlives every timer removes that race entirely, and
        reconnecting per query was never worth anything.
        """
        if self._con is None:
            self._con = self.backend.connect(self.db_path)
        return self._con

    def close(self) -> None:
        if self._con is not None:
            self.backend.close(self._con)
            self._con = None

    def _discover_tables(self) -> set[str]:
        return self.backend.table_names(self._conn())

    def describe_schema(self) -> list[dict]:
        """Tables, columns and row counts.

        Metadata lives here rather than in catalog/ for the same reason queries
        do: this is the only module holding a connection. The catalog consumes
        what this returns and never opens the file itself.
        """
        return self.backend.describe(self._conn())

    # --- stages ----------------------------------------------------------

    def authorize(self, sql: str) -> Decision:
        """Everything except execution. Pure, so it is cheap to test."""
        # parse
        try:
            statements = [s for s in sqlglot.parse(sql, dialect=self.backend.dialect) if s]
        except sqlglot.ParseError as exc:
            return _refuse("parse", f"could not parse SQL: {exc}")
        if not statements:
            return _refuse("parse", "empty statement")
        if len(statements) > 1:
            return _refuse("parse", "one statement at a time")

        tree = statements[0]

        # authorize: reads only
        if not isinstance(tree, (exp.Select, exp.Union, exp.Subquery)):
            return _refuse(
                "authorize",
                f"only SELECT is allowed, got {type(tree).__name__.upper()}",
            )
        for node_type in (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create,
                          exp.Alter, exp.TruncateTable, exp.Command):
            if tree.find(node_type):
                return _refuse(
                    "authorize",
                    f"{node_type.__name__.upper()} is not allowed in a read-only gateway",
                )

        # authorize: known tables only
        cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
        tables = []
        for table in tree.find_all(exp.Table):
            name = table.name.lower()
            if name and name not in cte_names:
                tables.append(name)
        unknown = sorted({t for t in tables if t not in self.allowlist})
        if unknown:
            return _refuse(
                "authorize",
                f"unknown table(s): {', '.join(unknown)}. "
                f"Available: {', '.join(sorted(self.allowlist))}",
            )

        # lint: a join with no ON/USING and no WHERE is a cartesian product
        for select in tree.find_all(exp.Select):
            joins = select.args.get("joins") or []
            unqualified = [
                j for j in joins
                if not j.args.get("on") and not j.args.get("using")
            ]
            if unqualified and not select.args.get("where"):
                return _refuse(
                    "lint",
                    "cross join with no join predicate and no WHERE clause: "
                    "this multiplies every row. Add ON or USING.",
                )

        # cost: a cheap structural guard, not a real optimiser estimate
        n_joins = sum(
            len(s.args.get("joins") or []) for s in tree.find_all(exp.Select)
        )
        if n_joins > self.max_joins:
            return _refuse(
                "cost",
                f"{n_joins} joins exceeds the limit of {self.max_joins}",
            )

        # rewrite: bound the result. +1 row so truncation is detectable.
        bounded = self._apply_limit(tree)
        return Decision(
            allowed=True,
            stage="authorized",
            sql=bounded.sql(dialect=self.backend.dialect),
            tables=sorted(set(tables)),
        )

    def _apply_limit(self, tree: exp.Expression) -> exp.Expression:
        tree = tree.copy()
        probe = self.max_rows + 1
        existing = tree.args.get("limit")
        if existing is None:
            return tree.limit(probe)
        try:
            current = int(existing.expression.name)
        except (AttributeError, ValueError):
            return tree.limit(probe)
        return tree if current <= self.max_rows else tree.limit(probe)

    # --- execution -------------------------------------------------------

    def run_sql(self, sql: str, note: str = "") -> tuple[Decision, EvidenceRecord | None]:
        """Authorize, then execute. Returns (decision, evidence or None)."""
        decision = self.authorize(sql)
        if not decision.allowed:
            return decision, None

        con = self._conn()
        watchdog = self.backend.watch(con, self.timeout)
        try:
            cur = con.execute(decision.sql)
            columns = [d[0] for d in cur.description] if cur.description else []
            rows = [list(r) for r in cur.fetchall()]
        except Exception as exc:
            if self.backend.is_interrupt(exc):
                return _refuse(
                    "execute",
                    f"Query aborted after {self.timeout:.0f}s. It is too expensive"
                    " -- add a WHERE clause, a LIMIT, or aggregate instead of"
                    " scanning.",
                ), None
            if self.backend.is_engine_error(exc):
                # Execution errors are refusals too: the model needs the text
                # to repair the query, and nothing was recorded as evidence.
                return _refuse("execute", f"{type(exc).__name__}: {exc}"), None
            raise
        finally:
            if watchdog:
                watchdog.cancel()

        truncated = len(rows) > self.max_rows
        rows = rows[: self.max_rows]

        record = self.store.put_query(
            sql=decision.sql,
            columns=columns,
            rows=rows,
            truncated=truncated,
            note=note,
        )
        return decision, record
