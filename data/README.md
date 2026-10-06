# data

Local test databases and reference material.

Empty on purpose. Phase 0 does not need a database.

## What belongs in Git

Generated and local database files should **not** be committed. `.gitignore`
excludes `*.db`, `*.sqlite`, `*.sqlite3`, and `*.duckdb` in this directory.

The exception is a **fixture**: a small, stable database used deliberately by
tests or evals, where the exact contents are part of the test. Commit one of
those on purpose:

    git add -f data/fixture.db

and say in this README why it exists, how it was generated, and what depends on
it. If you cannot explain why a binary file is in the repository, it should not
be in the repository.
