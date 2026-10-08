"""Build spider2-dbt-sandbox:<content hash>.

    uv run python -m evals.dbt.scripts.build_image

1. Export docker/dbt-sandbox/requirements.txt from uv.lock (main dependencies +
   the dbt-sandbox group). Written only if it changed, so a no-op build leaves
   the tree clean; a change shows up in `git status` and must be committed.
2. Compute the content tag (evals/dbt/image.py) over the inputs.
3. docker build with the allowlisted context and print the image ID.

Nothing is written to configs: the runner derives the same tag itself.
"""

import argparse
import subprocess
import sys
from pathlib import Path

from evals.dbt.image import ROOT, content_tag, inputs_hash

DOCKERFILE = ROOT / "docker" / "dbt-sandbox" / "Dockerfile"
REQUIREMENTS = ROOT / "docker" / "dbt-sandbox" / "requirements.txt"

EXPORT = ["uv", "export", "--frozen", "--no-dev", "--group", "dbt-sandbox",
          "--no-hashes", "--no-emit-project", "--format", "requirements-txt"]


def export_requirements() -> bool:
    """Returns True if requirements.txt changed."""
    out = subprocess.run(EXPORT, cwd=ROOT, check=True, capture_output=True, text=True).stdout
    out = out.replace("\r\n", "\n")
    current = REQUIREMENTS.read_text(encoding="utf-8") if REQUIREMENTS.exists() else None
    if current is not None and current.replace("\r\n", "\n") == out:
        return False
    REQUIREMENTS.write_bytes(out.encode("utf-8"))
    return True


def image_id(tag: str) -> str | None:
    proc = subprocess.run(["docker", "image", "inspect", tag, "--format", "{{.Id}}"],
                          capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()

    changed = export_requirements()
    if changed:
        print(f"requirements.txt regenerated from uv.lock -- commit {REQUIREMENTS.relative_to(ROOT)}")
    tag = content_tag()
    print(f"tag: {tag}")

    cmd = ["docker", "build", "-f", str(DOCKERFILE), "-t", tag,
           "--label", f"s2dbt.inputs_sha256={inputs_hash()}", str(ROOT)]
    if args.no_cache:
        cmd.insert(2, "--no-cache")
    proc = subprocess.run(cmd, cwd=ROOT, env={**_build_env()})
    if proc.returncode != 0:
        print("docker build failed", file=sys.stderr)
        return proc.returncode
    print(f"image id: {image_id(tag)}")
    return 0


def _build_env() -> dict:
    import os
    env = dict(os.environ)
    # The key is never needed to build; keep it out of the build's environment
    # so no RUN step could see it even by accident.
    env.pop("OPENAI_API_KEY", None)
    env["DOCKER_BUILDKIT"] = "1"
    return env


if __name__ == "__main__":
    raise SystemExit(main())
