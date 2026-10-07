"""Engine-specific pieces of the gateway. The policy does not live here.

Parse, authorize, lint, cost and the LIMIT rewrite are all dialect-parameterised
sqlglot, so they work unchanged on either engine. What genuinely differs is
four things, and that is all a backend is:

  1. how to open a connection that CANNOT write and CANNOT read other files
  2. how to bound a statement in time
  3. how to read the schema
  4. which exception means "the engine interrupted this"

Keeping those four in one place is what lets one agent run against the DuckDB
warehouse and Spider's 30 SQLite databases without a second gateway, a second
sandbox, and a second set of integrity tests.

## The read-only guarantees, per engine

DuckDB: `read_only=True` stops writes. It does NOT stop reads of other files --
`read_json_auto('evals/gold/questions.jsonl')` worked, and the table allowlist
never fired because a table function is not a table. `enable_external_access=false`
closes that, and `lock_configuration=true` stops SET re-opening it.

SQLite: `mode=ro` stops writes. `ATTACH` is the equivalent hole -- it reaches
any file on disk, including the gold answers -- so it is denied by a connection
authorizer. SQLite has no table functions for reading files, so there is no
third vector.
"""

import sqlite3
import threading
from pathlib import Path
from typing import Any


class _Watchdog:
    """A timer that cannot interrupt a connection after its query is done.

    `Timer.cancel()` only helps if the timer has not already started running.
    With a connection created per query, a timer firing a moment after its
    connection closed called interrupt() on a dead handle -- which crashes the
    interpreter rather than raising. The flag closes that race, and the
    connection now outlives every timer anyway.
    """

    def __init__(self, con, timeout: float):
        self._done = False
        self._timer = threading.Timer(timeout, self._fire)
        self._timer.daemon = True
        self._con = con
        self._timer.start()

    def _fire(self) -> None:
        if self._done:
            return
        try:
            self._con.interrupt()
        except Exception:
            pass

    def cancel(self) -> None:
        self._done = True
        self._timer.cancel()


class DuckDBBackend:
    """DuckDB is imported lazily, inside the methods that need it.

    A Spider run is pure SQLite and has no business loading DuckDB's native
    extension, which starts its own threads. Importing it at module scope meant
    every SQLite run paid for that, and the configuration that ran 135 Spider
    tasks without trouble was precisely the one that never imported it.
    """

    name = "duckdb"
    dialect = "duckdb"

    # enable_external_access=false is the important one; see the module docstring.
    CONFIG = {"enable_external_access": "false", "lock_configuration": "true"}

    def connect(self, path: Path):
        import duckdb

        con = duckdb.connect(str(path), read_only=True, config=dict(self.CONFIG))
        # DuckDB has no statement_timeout, so the bound is a watchdog thread
        # calling interrupt(). Returned so the caller can cancel it.
        return con

    def watch(self, con, timeout: float | None):
        return _Watchdog(con, timeout) if timeout else None

    def close(self, con) -> None:
        con.close()

    def is_interrupt(self, exc: Exception) -> bool:
        import duckdb

        return isinstance(exc, duckdb.InterruptException)

    def is_engine_error(self, exc: Exception) -> bool:
        import duckdb

        return isinstance(exc, duckdb.Error)

    def describe(self, con) -> list[dict]:
        cols = con.execute(
            "SELECT table_name, column_name, data_type "
            "FROM information_schema.columns WHERE table_schema = 'main' "
            "ORDER BY table_name, ordinal_position"
        ).fetchall()
        return _group_columns(cols, con, quote='"')

    def table_names(self, con) -> set[str]:
        rows = con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main'"
        ).fetchall()
        return {r[0].lower() for r in rows}


class SQLiteBackend:
    name = "sqlite"
    dialect = "sqlite"

    def connect(self, path: Path):
        uri = f"file:{Path(path).as_posix()}?mode=ro"
        con = sqlite3.connect(uri, uri=True)
        con.row_factory = sqlite3.Row

        # mode=ro stops writes but still permits ATTACH, which reaches other
        # files -- including the benchmark's gold answers. Deny it at the
        # engine level so the connection is bound to exactly one file.
        def authorizer(action, arg1, arg2, dbname, source):
            if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        con.set_authorizer(authorizer)

        # A watchdog thread calling interrupt(), the same mechanism DuckDB uses.
        #
        # The first version used set_progress_handler, which is the documented
        # way to abort a sqlite3 statement -- but it registers a Python callable
        # that SQLite invokes from C every N opcodes, and that reproducibly
        # crashed the interpreter when a connection was collected while a
        # request was in flight:
        #
        #   PyEval_SaveThread: the function must be called with the GIL held
        #
        # Verified by disabling the timeout: the crash went away. interrupt() is
        # explicitly safe to call from another thread and needs no callback.
        return con

    def watch(self, con, timeout: float | None):
        return _Watchdog(con, timeout) if timeout else None

    def close(self, con) -> None:
        # Clear the authorizer before closing: it is a Python callable SQLite
        # invokes from C, and leaving one registered on a connection being torn
        # down is a way to get an interpreter crash rather than an exception.
        try:
            con.set_authorizer(None)
        except sqlite3.Error:
            pass
        con.close()

    def is_interrupt(self, exc: Exception) -> bool:
        return isinstance(exc, sqlite3.OperationalError) and "interrupt" in str(exc).lower()

    def is_engine_error(self, exc: Exception) -> bool:
        return isinstance(exc, sqlite3.Error)

    def describe(self, con) -> list[dict]:
        names = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()]
        out = []
        for table in names:
            columns = [
                {"name": r[1], "type": r[2] or "UNKNOWN"}
                for r in con.execute(f'PRAGMA table_info("{table}")').fetchall()
            ]
            n = con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            out.append({"table": table, "columns": columns, "row_count": n})
        return out

    def table_names(self, con) -> set[str]:
        rows = con.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
            "AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        return {r[0].lower() for r in rows}


def _group_columns(cols, con, quote: str) -> list[dict]:
    tables: dict[str, list[dict]] = {}
    for table, column, dtype in cols:
        tables.setdefault(table, []).append({"name": column, "type": dtype})
    out = []
    for table, columns in tables.items():
        n = con.execute(f"SELECT COUNT(*) FROM {quote}{table}{quote}").fetchone()[0]
        out.append({"table": table, "columns": columns, "row_count": n})
    return out


BACKENDS: dict[str, Any] = {"duckdb": DuckDBBackend, "sqlite": SQLiteBackend}

# Spider ships .sqlite; the warehouse is .duckdb. Guessing from the extension
# means a caller never has to say which engine a path is.
SUFFIXES = {
    ".duckdb": "duckdb",
    ".db": "sqlite",
    ".sqlite": "sqlite",
    ".sqlite3": "sqlite",
}


def for_path(path: Path, backend: str | None = None):
    """Pick a backend by name, or infer it from the file extension."""
    if backend:
        if backend not in BACKENDS:
            raise ValueError(f"unknown backend {backend!r}: {sorted(BACKENDS)}")
        return BACKENDS[backend]()
    guess = SUFFIXES.get(Path(path).suffix.lower())
    if guess is None:
        raise ValueError(
            f"cannot infer an engine from {Path(path).name!r}; "
            f"pass backend= explicitly ({sorted(BACKENDS)})"
        )
    return BACKENDS[guess]()
