"""What shape of answer a task wants, and how to check it.

One agent, one loop, one gateway, two output contracts:

  PROSE   a warehouse question. The answer is sentences, and every number in
          them is a {{evidence_id.field}} placeholder. Verified by
          verifier/check.py: numbers backed, placeholders resolve, parts
          reconcile with the whole.

  SQL     a benchmark task. The answer is one query, scored by executing it
          and comparing rows against gold. Placeholders are meaningless here.

Both verifiers are deterministic and contain no model. The SQL one checks a
property worth naming: **the query you are submitting is one you actually
ran.** A model that explores with five queries and then writes a sixth,
unexecuted, "clean" version in its final answer is the common failure -- the
submitted query has never touched the database and nobody noticed. Matching the
final SQL against the evidence store catches it, the same way the prose mode
catches a typed digit.
"""

import hashlib
import re

import sqlglot
from dataclasses import dataclass

from ..verdicts import FAIL, NEEDS, PASS
from ..verifier import CheckResult, VerificationResult

SQL_BLOCK = re.compile(r"```sql\s*(.*?)```", re.S | re.I)


def _canonical(sql: str, dialect: str = "sqlite") -> str:
    """A parsed, canonical form of a statement -- not its text.

    Text comparison does not work here and the first version of this check
    scored 0/15 because of it. The gateway re-renders SQL through sqlglot
    before executing, so a model writing

        FROM orders o JOIN order_items oi

    is stored as

        FROM orders AS o JOIN order_items AS oi

    Identical query, different string. Comparing parse trees sees through
    aliasing, quoting, whitespace and keyword case. The appended LIMIT is
    dropped before comparing, since the gateway adds it, not the model.
    """
    try:
        tree = sqlglot.parse_one(sql, dialect=dialect)
    except Exception:
        return " ".join(sql.strip().rstrip(";").split()).lower()
    if tree is None:
        return ""
    tree = tree.copy()
    tree.set("limit", None)
    return tree.sql(dialect=dialect, normalize=True, comments=False)


def _hash(sql: str, dialect: str = "sqlite") -> str:
    return hashlib.sha256(_canonical(sql, dialect).encode("utf-8")).hexdigest()[:16]


@dataclass
class Mode:
    name: str
    system: str
    repair_preamble: str
    # What to say when the step budget runs out and one tool-free turn is left.
    forced_closing: str


PROSE_SYSTEM = """You are a data analyst answering a question about a database.

## How to work

1. Call `search_catalog` first. It knows the tables, and the metric definitions
   when the database has any.
2. Use `describe_table` and `get_join_path` before writing a join.
3. Run SQL with `run_sql`. You never get a database connection; every query is
   authorized first and may be refused. A refusal explains itself -- read it and
   repair the query.
4. For any comparison between two periods, run one query per period and then use
   `compute_delta`, `compute_contribution` or `mix_vs_rate`. Do not subtract
   numbers yourself.

## The one rule that matters

**You must never write a digit that you computed or read off a result.**

Every number in your final answer must be a placeholder of the form
`{{evidence_id.field}}` -- for example `{{e7.delta_pct}}`. A number you type
yourself is rejected by the verifier and the answer is thrown away.

Dates and period names ("2026-09", "Q3") may be written normally.

## Finishing

End with your analysis as prose containing placeholders. Say what changed, and
why, and name the mechanism -- a volume effect and a rate effect are different
answers and the tools can tell them apart."""

SQL_SYSTEM = """You are answering a question about a database by writing one SQL query.

## How to work

1. Call `search_catalog` first to find the relevant tables.
2. Use `describe_table` before writing a join. Table names are matched
   case-insensitively, so write them as the question does.
3. Explore with `run_sql`. Every query is authorized first and may be refused;
   a refusal explains itself, so read it and repair the query.
4. Build the query in steps and check the row count at each one. A query that
   returns far more rows than the question implies has a join problem.

## The one rule that matters

**Run your final query before you submit it.**

Your answer is scored by executing that exact query. A query you wrote but
never ran is rejected -- the verifier checks it against the queries you
actually executed. Explore with five queries and then invent a sixth "clean"
one in your answer, and the answer is thrown away.

## Finishing

End with the final query in a ```sql fenced block, and nothing after it. That
single query must produce the answer on its own."""

PROSE = Mode(
    name="prose",
    system=PROSE_SYSTEM,
    repair_preamble="Your previous answer was rejected by the verifier",
    forced_closing=(
        "Every number must still be a {{evidence_id.field}} placeholder. If the "
        "evidence is incomplete, say so and answer what it does support."
    ),
)

SQL = Mode(
    name="sql",
    system=SQL_SYSTEM,
    repair_preamble="Your previous query was rejected by the verifier",
    forced_closing=(
        "Give the best query you have already run, in a ```sql fenced block. "
        "Do not invent a new one -- an unexecuted query is rejected."
    ),
)

MODES = {"prose": PROSE, "sql": SQL}


def extract_sql(text: str | None) -> str | None:
    """The last fenced sql block. The last, because models revise."""
    if not text:
        return None
    blocks = SQL_BLOCK.findall(text)
    return blocks[-1].strip() if blocks else None


def verify_sql_answer(
    final_text: str | None, store, dialect: str = "sqlite"
) -> VerificationResult:
    """Deterministic checks on a SQL-mode answer. No model involved."""
    result = VerificationResult()
    sql = extract_sql(final_text)

    result.findings.append(CheckResult(
        check="sql_produced",
        verdict=PASS if sql else FAIL,
        detail="one fenced ```sql block" if sql
               else "no ```sql block in the answer; nothing can be scored",
    ))
    if not sql:
        return result

    # Was this query actually executed? Compare against what the gateway ran.
    target = _hash(sql, dialect)
    match = None
    for record in (store.get(i) for i in store.ids()):
        if record is not None and record.sql and _hash(record.sql, dialect) == target:
            match = record
            break

    result.findings.append(CheckResult(
        check="sql_was_executed",
        verdict=PASS if match else FAIL,
        detail=(f"matches evidence {match.id}" if match else
                "this query was never run. Execute it with run_sql before "
                "submitting it."),
        evidence_id=match.id if match else None,
    ))

    if match is None:
        return result

    result.findings.append(CheckResult(
        check="sql_returned_rows",
        verdict=PASS if match.row_count else NEEDS,
        detail=f"{match.row_count} row(s)" if match.row_count
               else "the query ran but returned nothing; check the filters",
        evidence_id=match.id,
    ))
    return result
