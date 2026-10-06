# tests

Unit and integration tests.

Empty on purpose. Phase 0 has nothing worth asserting about beyond "the agent
runs", which is verified by running it and watching.

Tests get written when there is behavior that can regress — the manual loop
(Phase 1), tool implementations (Phase 4), and the eval runner (Phase 5) are
the first real candidates.

Run with:

    uv run pytest
