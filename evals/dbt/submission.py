"""Place a task attempt's prediction where Spider's evaluate.py expects it.

    attempt_<n>/<id>/submission/results_metadata.jsonl
    attempt_<n>/<id>/submission/<id>/<start_db>

The prediction is the database dbt built into, moved out of the workspace once
the container is gone. It is never modified afterwards; scoring works on a
scratch copy, so re-scoring always starts from the same bytes.
"""

import json
import shutil
from pathlib import Path


def place(attempt_dir: Path, instance_id: str, start_db: str,
          source_db: Path | None = None) -> Path | None:
    """Move the prediction into submission/. Returns its path, or None if the
    workspace has no database (counted as a fail, not a harness error)."""
    sub = Path(attempt_dir) / "submission"
    pred_dir = sub / instance_id
    pred_dir.mkdir(parents=True, exist_ok=True)
    src = Path(source_db) if source_db else Path(attempt_dir) / "workspace" / start_db
    dest = pred_dir / start_db
    if src.exists():
        if src.resolve() != dest.resolve():
            shutil.move(str(src), str(dest))
    write_metadata(sub, instance_id, start_db)
    return dest if dest.exists() else None


def write_metadata(sub: Path, instance_id: str, start_db: str) -> None:
    line = {"instance_id": instance_id, "answer_type": "file", "answer_or_path": start_db}
    (Path(sub) / "results_metadata.jsonl").write_text(json.dumps(line) + "\n",
                                                      encoding="utf-8")
