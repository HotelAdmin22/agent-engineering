# Node, not Python, is the base image on purpose.
#
# The Claude Agent SDK does not call the API directly — it spawns the Claude
# Code CLI as a subprocess (see claude_agent_sdk/_internal/transport/
# subprocess_cli.py, which searches PATH for `claude` and raises
# CLINotFoundError telling you to `npm install -g @anthropic-ai/claude-code`).
# So the CLI, and therefore Node, has to be in the image. uv supplies Python.
FROM node:20-slim

# uv, copied from its official image — pinned, no installer script.
COPY --from=ghcr.io/astral-sh/uv:0.12.13 /uv /uvx /usr/local/bin/

# The CLI the SDK shells out to.
RUN npm install -g @anthropic-ai/claude-code && npm cache clean --force

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_FROZEN=1 \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Dependencies first, so editing source does not re-resolve them. --frozen uses
# uv.lock exactly as committed and never re-resolves: the image gets the same
# versions the host did.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-install-project --no-dev

# Everything .dockerignore permits — source, prompts, docs, placeholders.
COPY . .
RUN uv sync --frozen --no-dev

# No ENTRYPOINT on purpose: `docker compose run agent <anything>` replaces this
# CMD wholesale, so both of these work —
#     docker compose run --rm agent
#     docker compose run --rm agent uv run agent "your prompt"
# `agent` is the console script declared in pyproject.toml.
CMD ["agent", "Read the README and tell me what this project is for."]
