# The Spider2-DBT task sandbox. Separate from the root Dockerfile, which runs
# the Spider2-Lite harness and is not touched by this one.
#
# One container per task attempt runs from this image with --network none, a
# read-only root, no environment and only the task workspace mounted. It holds
# the tool host (sandbox_tools), the SQL gateway (agent.gateway, from src/),
# dbt-duckdb, and Spider's evaluator dependencies (the evaluator container runs
# from the same image).
#
# Build with `uv run python -m evals.dbt.scripts.build_image`, which exports
# requirements.txt from uv.lock first and tags the image by content hash.
#
# The build context is the repo root, filtered by Dockerfile.dockerignore next
# to this file: an allowlist, so .env and the benchmark cannot enter it.

FROM python:3.12-slim@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f

RUN useradd --create-home --uid 1000 agent

COPY docker/dbt-sandbox/requirements.txt /opt/sandbox/requirements.txt
RUN pip install --no-cache-dir -r /opt/sandbox/requirements.txt

# agent.gateway and its evidence store. README.md is there because the build
# backend reads it for the package metadata.
COPY pyproject.toml README.md /opt/project/
COPY src/ /opt/project/src/
RUN pip install --no-cache-dir --no-deps /opt/project

COPY evals/dbt/sandbox_tools/ /opt/sandbox/sandbox_tools/

# HOME on the tmpfs: dbt and pandas write caches under it, and the root is
# read-only. No usage stats, and nothing would reach the network anyway.
ENV HOME=/tmp \
    PYTHONPATH=/opt/sandbox \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SANDBOX_WORKSPACE=/workspace \
    DBT_SEND_ANONYMOUS_USAGE_STATS=false \
    DO_NOT_TRACK=1

USER agent
WORKDIR /workspace
