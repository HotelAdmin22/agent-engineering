# A Spider sweep takes hours and has to be reproducible. The image pins Python
# and every dependency from uv.lock; the host supplies only the credential and
# the databases.
#
# This used to be a Node image carrying the Claude Code CLI, because the Phase 0
# agent used claude-agent-sdk, which does not call the API -- it spawns the CLI
# as a subprocess. Nothing does that any more: the agent talks to the OpenAI
# Responses API directly. Node and the CLI are gone, and so is ~400 MB.
FROM python:3.12-slim

# python:*-slim ships with NO CA certificates. Without this every TLS connection
# fails with CERTIFICATE_VERIFY_FAILED, which looks exactly like a network or
# proxy problem and is neither. Found the hard way: the first container sweep
# died on APIConnectionError with a valid key and working DNS.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# uv, copied from its official image -- pinned, no installer script.
COPY --from=ghcr.io/astral-sh/uv:0.12.13 /uv /uvx /usr/local/bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_FROZEN=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Dependencies first, so editing source does not re-resolve them. --frozen uses
# uv.lock exactly as committed and never re-resolves: the image gets the same
# versions the host did.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-install-project --no-dev

# Everything .dockerignore permits. Note what it does NOT permit: the Spider
# gold answers, the warehouse gold set, the seeded-anomaly key, .env, and the
# 1.6 GB of databases. The container produces predictions; the host scores them.
# The thing being measured cannot reach the answers even if every sandbox rule
# failed.
COPY . .
RUN uv sync --frozen --no-dev

# An ENTRYPOINT so flags pass straight through:
#
#     docker compose run --rm sweep --limit 5
#     docker compose run --rm sweep --limit 135 --resume --max-tokens 2000000
#
# Override it for anything else:
#     docker compose run --rm --entrypoint sh sweep
ENTRYPOINT ["uv", "run", "python", "-u", "-m", "evals.runner"]

# A small, safe default.
CMD ["--limit", "5"]
