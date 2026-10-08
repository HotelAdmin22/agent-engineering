"""Score one prediction with Spider's own evaluate.py, on scratch copies.

    docker run --rm --network none --read-only --tmpfs /tmp --user 1000:1000 \\
      -v <evaluation_suite>:/eval:ro -v <scoring>:/score:rw -w /eval \\
      <image> python evaluate.py --result_dir /score/submission --gold_dir /score/gold

Gold reaches only this container, only as a copy, only after the task
container is gone, and the copy is deleted afterwards. duckdb_match opens its
inputs read-only (V1), but copies make that irrelevant: neither the stored
submission nor the clone's gold can be changed by scoring.

evaluate.py prints `<accuracy> <passed> <total>` as its last line. That line is
the result; anything else is a harness error, never a fail -- a scorer that
silently turns its own breakage into "0" invents regressions.

    uv run python -m evals.dbt.scoring --run runs/dbt/<run_id>    # re-score a run
"""

import argparse
import json
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from . import records
from .docker_exec import docker
from .tasks import eval_suite, gold_dir

FINAL_LINE = re.compile(r"^\s*([0-9.]+(?:e-?\d+)?)\s+(\d+)\s+(\d+)\s*$")
EVAL_TIMEOUT_S = 600


@dataclass
class Score:
    passed: bool | None          # None = could not be determined (harness_error)
    stdout: str
    detail: str = ""


def parse(stdout: str) -> tuple[int, int] | None:
    for line in reversed((stdout or "").strip().splitlines()):
        m = FINAL_LINE.match(line)
        if m:
            return int(m.group(2)), int(m.group(3))
    return None


def _eval_lines(spider2_root: Path, instance_id: str) -> str:
    src = eval_suite(spider2_root) / "gold" / "spider2_eval.jsonl"
    for line in src.read_text(encoding="utf-8").splitlines():
        if line.strip() and json.loads(line)["instance_id"] == instance_id:
            return line.strip() + "\n"
    raise KeyError(f"{instance_id} not in spider2_eval.jsonl")


def score_submission(submission: Path, instance_id: str, spider2_root: Path,
                     image: str, scoring_dir: Path, gold_override: Path | None = None,
                     user: str = "1000:1000") -> Score:
    """Copy gold + submission into scoring_dir, evaluate, delete scoring_dir.

    gold_override is for the evaluator self-test only (a prediction that IS
    the gold file); real scoring always copies the task's gold directory.
    """
    scoring_dir = Path(scoring_dir)
    if scoring_dir.exists():
        shutil.rmtree(scoring_dir)
    try:
        g = scoring_dir / "gold"
        shutil.copytree(gold_override or gold_dir(spider2_root, instance_id), g / instance_id)
        # Only this task's line: evaluate.py scores the intersection of gold
        # and result ids, and the denominator must be this one task.
        (g / "spider2_eval.jsonl").write_text(_eval_lines(spider2_root, instance_id),
                                              encoding="utf-8")
        shutil.copytree(submission, scoring_dir / "submission")

        proc = docker(
            "run", "--rm", "--network", "none", "--read-only", "--tmpfs", "/tmp",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", user,
            "--mount", f"type=bind,source={eval_suite(spider2_root).resolve()},target=/eval,readonly",
            "--mount", f"type=bind,source={scoring_dir.resolve()},target=/score",
            "-w", "/eval", image,
            "python", "evaluate.py", "--result_dir", "/score/submission",
            "--gold_dir", "/score/gold",
            timeout=EVAL_TIMEOUT_S,
        )
        out = (proc.stdout or "") + ("\n--- stderr ---\n" + proc.stderr if proc.stderr else "")
        parsed = parse(proc.stdout)
        if proc.returncode != 0 or parsed is None:
            return Score(None, out, f"evaluator exit {proc.returncode}, final line "
                                    f"{'unparseable' if parsed is None else 'ok'}")
        passed, total = parsed
        if total != 1:
            return Score(None, out, f"expected 1 task scored, got {total}")
        return Score(passed == 1, out)
    finally:
        shutil.rmtree(scoring_dir, ignore_errors=True)


def score_attempt(attempt_dir: Path, instance_id: str, spider2_root: Path, image: str) -> Score:
    attempt_dir = Path(attempt_dir)
    score = score_submission(attempt_dir / "submission", instance_id, spider2_root, image,
                             attempt_dir / "scoring")
    (attempt_dir / "evaluate_stdout.txt").write_text(score.stdout, encoding="utf-8")
    return score


# --- re-score CLI ----------------------------------------------------------

def rescore_run(run_dir: Path) -> dict:
    manifest = records.read_json(run_dir / "run_manifest.json")
    spider2_root = Path(manifest["config"]["spider2_root_resolved"])
    image = manifest["sandbox"]["image_id"]
    changed, same, errors = [], 0, []
    for result_path in sorted(run_dir.glob("attempt_*/*/result.json")):
        result = records.read_json(result_path)
        if result.get("status") == "harness_error":
            continue
        attempt_dir = result_path.parent
        sub = attempt_dir / "submission"
        if not (sub / "results_metadata.jsonl").exists():
            errors.append(f"{attempt_dir.name}: no submission")
            continue
        s = score_submission(sub, result["instance_id"], spider2_root, image,
                             attempt_dir / "scoring")
        if s.passed is None:
            errors.append(f"{result['instance_id']} a{result['attempt']}: {s.detail}")
            continue
        if s.passed != result["passed"]:
            changed.append({"instance_id": result["instance_id"], "attempt": result["attempt"],
                            "stored": result["passed"], "rescored": s.passed})
        else:
            same += 1
    return {"identical": same, "changed": changed, "errors": errors}


def main() -> int:
    ap = argparse.ArgumentParser(description="Re-score a run from its stored submissions")
    ap.add_argument("--run", type=Path, required=True)
    args = ap.parse_args()
    report = rescore_run(args.run)
    print(json.dumps(report, indent=2))
    records.write_json_atomic(args.run / "rescore.json", report)
    return 0 if not report["changed"] and not report["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
