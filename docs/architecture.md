# Architecture

**Generated, not written.** See [`architecture.html`](architecture.html) — open
it in a browser.

It is built from the source tree by `build_architecture.py`, and rebuilt
automatically whenever a `.py` file changes (a `PostToolUse` hook in
`.claude/settings.json` runs `rebuild_hook.py`). Rebuild it by hand with:

```bash
python docs/build_architecture.py
```

## Why it is generated

A hand-maintained architecture page describes the system as it was on the day
someone last remembered to edit it. This one cannot drift, because everything
on it is read from the repository at build time:

- **Module descriptions** are each file's own docstring. Edit the source, and
  the page changes. There is no second copy of the explanation to fall stale.
- **Live counts** — Spider databases present, ledger findings recorded,
  experiments logged, which API keys are set — are read from disk. The page
  shows gaps in red rather than quietly implying the system is finished.
- **Git state** is stamped on it, so a page built from uncommitted work says
  so.

The one hand-written part is the per-layer blurb in `build_architecture.py`,
because "what is this layer for" is not derivable from any single file in it.

## Editing it

Change a module's docstring, not the HTML. The HTML is a build artifact and is
overwritten on every rebuild.
