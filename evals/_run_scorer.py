"""Run Spider's evaluate.py unmodified, with the cloud backends stubbed out.

evaluate.py imports `google.cloud.bigquery` at module scope even when scoring
local SQLite tasks in exec_result mode, which is the only mode used here. The
choices were:

  - install google-cloud-bigquery: ~150 MB and a credentials file, for an
    import that is never called on a `local*` task
  - edit evaluate.py: it is the one thing in this project deliberately NOT
    written or modified here, because a scorer you adjust is a scorer you can
    talk yourself into

So the import is satisfied with an empty module and evaluate.py runs byte for
byte as published. If a BigQuery or Snowflake task were ever scored this would
fail loudly on first use rather than quietly return a wrong number -- which is
the right failure mode.

Not called directly; evals/score.py invokes it.
"""

import runpy
import sys
import types
from pathlib import Path

# Backends this project never scores against. Spider 2.0-Lite's `local*` tasks
# are SQLite; bq* and sf* tasks need real cloud credentials and are out of scope.
STUBBED = [
    "google",
    "google.cloud",
    "google.cloud.bigquery",
    "snowflake",
    "snowflake.connector",
]


def main() -> None:
    for name in STUBBED:
        if name not in sys.modules:
            module = types.ModuleType(name)
            # Attribute access returns another stub rather than raising, so a
            # module-scope `from x import y` succeeds and only a real CALL fails.
            module.__getattr__ = lambda attr, _n=name: types.SimpleNamespace()
            sys.modules[name] = module
    sys.modules["google"].cloud = sys.modules["google.cloud"]
    sys.modules["google.cloud"].bigquery = sys.modules["google.cloud.bigquery"]

    evaluate = Path(__file__).resolve().parent / "spider" / "evaluation_suite" / "evaluate.py"
    sys.argv = [str(evaluate)] + sys.argv[1:]
    runpy.run_path(str(evaluate), run_name="__main__")


if __name__ == "__main__":
    main()
