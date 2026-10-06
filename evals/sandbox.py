"""The agent's entire capability surface for a Spider task: one read-only query.

Benchmark integrity is a capability question, not a rule question. The Spider
repo ships gold SQL (gold/sql/), gold results (gold/exec_result/) and the eval
key -- all of which sit on this filesystem. Telling the agent not to look at
them would be a validator, and a validator is a guess about what the agent might
try.

Instead the agent gets ONE tool, bound to ONE SQLite file, opened read-only. It
has no file tool, no shell, no glob, no search, no network. There is no path
from `query_database(sql)` to the gold answers, so no prompt wording and no
model decision can reach them.

Two structural guarantees, both enforced here rather than requested:

  1. mode=ro    -- the connection cannot write, whatever SQL arrives
  2. one path   -- the connection is bound at construction to this task's
                   database, so the agent cannot address any other file
"""

import sqlite3
from pathlib import Path

# Enough rows to answer an aggregate; few enough that one careless SELECT *
# cannot fill the context window.
MAX_ROWS = 50

QUERY_DATABASE_TOOL = {
    "name": "query_database",
    "description": (
        "Run a read-only SQL query against the task's SQLite database and get "
        "the rows back. Use it to inspect the schema and to compute the answer. "
        "The schema is available via standard SQLite catalogue queries, e.g. "
        "SELECT name, sql FROM sqlite_master WHERE type='table'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "sql": {
                "type": "string",
                "description": "A single SQL statement in SQLite dialect.",
            }
        },
        "required": ["sql"],
        "additionalProperties": False,
    },
}


class Sandbox:
    """A read-only, single-database execution surface."""

    def __init__(self, db_path: Path, max_rows: int = MAX_ROWS):
        self.db_path = Path(db_path)
        self.max_rows = max_rows
        if not self.db_path.exists():
            raise FileNotFoundError(f"database not found: {self.db_path}")

    @property
    def tools(self) -> list[dict]:
        """The complete tool surface. One tool. Nothing else exists."""
        return [QUERY_DATABASE_TOOL]

    def _connect(self) -> sqlite3.Connection:
        # file:...?mode=ro is the first structural restriction. A write is
        # rejected by SQLite itself, not by inspecting the SQL for keywords.
        uri = f"file:{self.db_path.as_posix()}?mode=ro"
        con = sqlite3.connect(uri, uri=True)

        # mode=ro alone is NOT enough: it stops writes but still permits
        # ATTACH DATABASE, which reaches other files on disk -- including the
        # gold answers. Verified: ATTACH succeeded before this authorizer was
        # added. Deny it at the SQLite level so the connection is bound to
        # exactly one file.
        def authorizer(action, arg1, arg2, dbname, source):
            if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        con.set_authorizer(authorizer)
        return con

    def query(self, sql: str) -> str:
        """Execute SQL, return rows as text. Raises on failure."""
        con = self._connect()
        try:
            cur = con.execute(sql)
            columns = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchmany(self.max_rows)
            more = cur.fetchone() is not None
        finally:
            con.close()

        if not columns:
            return "(statement returned no result set)"

        lines = [" | ".join(columns)]
        lines += [
            " | ".join("NULL" if v is None else str(v) for v in row) for row in rows
        ]
        if not rows:
            lines.append("(0 rows)")
        if more:
            # Announce truncation. Silent truncation is worse than an error --
            # the model answers confidently from partial data.
            lines.append(f"... truncated at {self.max_rows} rows; aggregate or LIMIT.")
        return "\n".join(lines)

    def execute(self, name: str, tool_input: dict) -> tuple[str, bool]:
        """Dispatch for the loop. Errors are RETURNED, never raised.

        The error text is the most useful thing the model receives: SQLite's
        "no such column: foo" is what lets it repair the query next turn.
        Recovery is where the accuracy lives.
        """
        if name != "query_database":
            # Unreachable unless the tool list and this dispatch disagree.
            return f"No such tool: {name}", True
        try:
            return self.query(tool_input["sql"]), False
        except Exception as exc:
            return f"{type(exc).__name__}: {exc}", True
