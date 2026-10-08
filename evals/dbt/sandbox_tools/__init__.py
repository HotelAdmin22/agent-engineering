"""Container side of the Spider2-DBT harness. Copied into the sandbox image.

Everything here runs INSIDE a task container: no network, no API key, only the
task's workspace mounted at /workspace. The host never executes a file, SQL or
dbt tool itself; it sends one JSON request to `toolhost` per call.

Imports are relative on purpose. On the host these modules are
`evals.dbt.sandbox_tools.*` (for unit tests); in the image they are
`sandbox_tools.*`. Nothing here may import duckdb, openai or anthropic --
SQL goes through agent.gateway, which the image installs from src/.
"""
