"""Fetch everything Spider 2.0-Lite needs that is NOT behind Google Drive.

The databases themselves are a Google Drive zip and cannot be scripted. Every
other dependency lives in the public xlang-ai/Spider2 repository, so there is no
reason to download those by hand:

  - the external-knowledge documents the tasks cite
  - Spider's own evaluate.py and evaluate_utils.py (never write your own scorer)
  - the gold execution results those scripts compare against
  - per-database schema metadata (DDL.csv and column JSON)

Run it, then download the one zip it tells you about.

    uv run python evals/spider/fetch.py
    uv run python evals/spider/fetch.py --schema      # also the 30 schema dirs
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAW = "https://raw.githubusercontent.com/xlang-ai/Spider2/main/spider2-lite"
TREE = "https://api.github.com/repos/xlang-ai/Spider2/git/trees/main?recursive=1"

DATABASES_ZIP = (
    "https://drive.usercontent.google.com/download"
    "?id=1coEVsCZq-Xvj9p2TnhBFoFTsY-UoYGmG&export=download"
)


def get(url: str) -> bytes | None:
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            return response.read()
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        return None


def save(data: bytes, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def tasks() -> list[dict]:
    return [
        json.loads(line)
        for line in (HERE / "tasks.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def fetch_documents(rows: list[dict]) -> tuple[int, int]:
    """Only the documents our 135 tasks actually cite. The upstream has 69."""
    wanted = sorted({r["external_knowledge"] for r in rows if r.get("external_knowledge")})
    ok = 0
    for name in wanted:
        target = HERE / "documents" / name
        if target.exists():
            ok += 1
            continue
        data = get(f"{RAW}/resource/documents/{name}")
        if data:
            save(data, target)
            ok += 1
        else:
            print(f"    MISSING {name}")
    return ok, len(wanted)


def fetch_scorer() -> tuple[int, int]:
    """Spider's own evaluator. A home-grown scorer invents improvements."""
    names = ["evaluate.py", "evaluate_utils.py", "README.md"]
    ok = 0
    for name in names:
        data = get(f"{RAW}/evaluation_suite/{name}")
        if data:
            save(data, HERE / "evaluation_suite" / name)
            ok += 1

    # evaluate.py reads this from the suite's PARENT directory for task
    # metadata. All 547 tasks, not just the 135 local ones -- it indexes into
    # it by instance_id.
    metadata = get(f"{RAW}/spider2-lite.jsonl")
    if metadata:
        save(metadata, HERE / "spider2-lite.jsonl")
        ok += 1
    return ok, len(names) + 1


def fetch_gold_results(rows: list[dict]) -> tuple[int, int]:
    """Gold CSVs for our 135 local tasks only, not all 1000."""
    listing = get(TREE)
    if not listing:
        print("    could not list the repo tree (rate limited?); skipping gold results")
        return 0, 0

    tree = json.loads(listing).get("tree", [])
    prefix = "spider2-lite/evaluation_suite/gold/exec_result/"
    ids = {r["instance_id"] for r in rows}
    paths = [
        e["path"] for e in tree
        if e["path"].startswith(prefix)
        and e["path"].rsplit("/", 1)[-1].split("_")[0].removesuffix(".csv") in ids
    ]

    ok = 0
    for path in paths:
        name = path.rsplit("/", 1)[-1]
        target = HERE / "gold" / "exec_result" / name
        if target.exists():
            ok += 1
            continue
        data = get(f"https://raw.githubusercontent.com/xlang-ai/Spider2/main/{path}")
        if data:
            save(data, target)
            ok += 1
    return ok, len(paths)


def fetch_schema(rows: list[dict]) -> tuple[int, int]:
    """DDL and column metadata per database.

    Worth having even before the .sqlite files land: it is what a catalog is
    built from, and reading it costs the agent no tool calls at run time.
    """
    listing = get(TREE)
    if not listing:
        return 0, 0
    tree = json.loads(listing).get("tree", [])
    prefix = "spider2-lite/resource/databases/sqlite/"
    dbs = {r["db"] for r in rows}
    paths = [
        e["path"] for e in tree
        if e["path"].startswith(prefix)
        and e["type"] == "blob"
        and e["path"][len(prefix):].split("/")[0] in dbs
    ]
    ok = 0
    for path in paths:
        rel = path[len(prefix):]
        target = HERE / "schema" / rel
        if target.exists():
            ok += 1
            continue
        data = get(f"https://raw.githubusercontent.com/xlang-ai/Spider2/main/{path}")
        if data:
            save(data, target)
            ok += 1
    return ok, len(paths)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--schema", action="store_true", help="also fetch schema metadata")
    args = ap.parse_args()

    rows = tasks()
    print(f"{len(rows)} tasks across {len({r['db'] for r in rows})} databases\n")

    print("documents ...")
    got, want = fetch_documents(rows)
    print(f"  {got}/{want}")

    print("scorer ...")
    got, want = fetch_scorer()
    print(f"  {got}/{want}")

    print("gold execution results ...")
    got, want = fetch_gold_results(rows)
    print(f"  {got}/{want}")

    if args.schema:
        print("schema metadata ...")
        got, want = fetch_schema(rows)
        print(f"  {got}/{want}")

    have = len(list((HERE / "databases").glob("*.sqlite")))
    print(f"\ndatabases: {have}/30")
    if have < 30:
        print(
            "\nThe .sqlite files are the one thing that cannot be scripted -- the\n"
            "upstream repo ships schema metadata, not the databases. Download:\n"
            f"\n  {DATABASES_ZIP}\n\n"
            f"Unzip so the files land directly in {HERE / 'databases'}\n"
            "as <db>.sqlite, one per task's `db` field."
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
