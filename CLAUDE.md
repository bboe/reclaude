# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`reclaude` is a stdlib-only Python curses picker for resuming Claude Code sessions. It reads `~/.claude/history.jsonl`, shows recent project directories as an expandable tree (sessions inline), locks directories that already have a running claude session, can resurrect sessions from deleted git worktrees, then chdirs and execs `claude`. Packaged for PyPI; installed with `uv tool install reclaude` (or `uv tool install --editable .` for hacking), which puts the `reclaude` console script on `PATH`.

## Commands

```bash
python3 -m pytest -q                                   # run all tests (from repo root)
python3 -m pytest tests/test_reclaude.py::test_mung_path -v   # single test
uv run --group dev --locked pytest -q                   # tests in an isolated env (as CI does)
uv run --group dev pre-commit run --all-files           # ruff check + ruff format (lint.select = ALL, preview)
uv build                                                # build sdist + wheel into dist/
reclaude                                                # run (needs a real TTY)
```

No third-party runtime dependencies (Python 3.10+ stdlib; pytest for tests). `[tool.pytest.ini_options] pythonpath = ["src"]` in `pyproject.toml` lets `python3 -m pytest` import the package from the repo root without an install. Releases publish to PyPI via `.github/workflows/publish.yml` (trusted publishing) on a `v*` tag push.

## Architecture

`reclaude` is a `src/` package (`src/reclaude/`) of two modules: pure logic in `core.py`, curses layer in `tui.py`. `__init__.py` only re-exports `main` (keeping the `reclaude:main` entry point stable); `__main__.py` wires up `python -m reclaude`.

1. **`core.py` (pure, curses-free, unit-tested):** `parse_history` → `group_by_home` builds per-directory groups of sessions; `classify_dir` tags each dir live / orphan-worktree / gone (a frozen `Classification`); `live_sessions` finds busy dirs + running session ids; `flatten_rows` turns groups + UI state (expansion, typed filter, age window, missing toggle) into the visible row list; `row_spans` renders a row as `(text, colorkey)` spans; `clamp_scroll` keeps the selection on screen. All filesystem/proc access is injectable (`isdir=`, `transcript_exists=`, `proc_root=`, `run=`, `sessions_dir=`) so tests are hermetic — keep it that way (`run=` is the ps/lsof command runner the macOS path uses). Tests import `reclaude.core`.
2. **`tui.py` (curses, no unit tests):** `init_colors` maps colorkeys → curses attrs (monochrome fallback), `_draw` renders span rows, `run_picker` is the event loop returning a `Launch`, `main` execs claude. Verified via throwaway fake-stdscr harnesses (scripted `getch`, recorded `addnstr`) in /tmp — never committed — plus manual testing by Bryce.

Data flow: `history.jsonl` → entries → groups (sessions attributed to their **home** dir) → rows → spans → screen; picker returns a frozen `Launch` (path, session_id, optional worktree_name; `Launch.argv` builds the claude command) → `os.chdir` + `os.execvp`.

### Invariants worth protecting

- **Display = action.** A dir row shows the time/prompt of `vis_sessions[0]` and Enter resumes exactly that session id. Never reintroduce `claude --continue` — it can disagree with what's displayed.
- **Filters before the MAX_DIRS cap** in `flatten_rows`, so hiding noise surfaces older live dirs.
- **Printable keys feed the incremental filter.** New shortcuts must be control keys (Ctrl-W=23 toggles missing dirs, Ctrl-T=20 cycles the age window); `q` quits only when the filter is empty.
- `tui.COLOR_KEYS` must cover every key `core.row_spans` emits.
- All screen writes go through `tui._addstr`, which clips to `maxx - 1` columns and tolerates `curses.error` (tiny terminals, bottom-right quirk).

## Empirically verified Claude Code facts (the whole design rests on these)

- `claude --resume <id>` / `--continue` only find transcripts under `~/.claude/projects/<munged-cwd>/` for the **current** directory. Munging: `/`, `.`, and `_` → `-` (deterministic; un-munging is ambiguous — only ever mung).
- A session's transcript lives under the directory the session **started** in and never moves, even if the session later changed cwd (EnterWorktree etc.). Hence `group_by_home`: first project in history = the only resumable location.
- Deleted worktree sessions resurrect via `cd <repo> && claude --worktree <name> --resume <id>` — claude recreates `<repo>/.claude/worktrees/<name>` (branch `worktree-<name>`, base per `worktree.baseRef`) and finds the transcript because the path matches again.
- `~/.claude/sessions/<pid>.json` describes live claude processes (`{pid, sessionId, cwd, procStart, startedAt(ms), ...}`). Stale files survive crashes — always validate the pid is a live claude before trusting one. Linux reads `/proc/<pid>/comm` (the literal `claude`); macOS/BSD have no `/proc`, so `ps -o comm= -p <pid>` is used instead — it's empty once the pid exits, and its output is the versioned binary path (`…/claude/versions/<version>`), which `_looks_like_claude` matches without matching `reclaude` itself. `find_busy_dirs`'s fallback scan is `/proc` on Linux, else `ps -axo pid=,comm=` + `lsof` for each cwd. Selection is `core.PROC_ROOT` (None ⇒ the ps/lsof backend), set once by `sys.platform`.
- `history.jsonl` lines: `{"display", "pastedContents", "timestamp"(ms), "project", "sessionId"}`; `display` can contain newlines/tabs (flattened in `parse_history`).

If claude changes any of this, re-verify empirically (cheap probe: `claude --resume <id> --fork-session --model haiku --print "Reply with only the word ok"` from the directory under test; clean up the forked transcript afterwards).

## Workflow conventions

- TDD for pure functions; the curses layer changes get fake-stdscr smoke tests instead.
- ruff runs with `lint.select = ["ALL"]` + `preview = true`; formatting is ruff-format-owned. The small ignore list and the tests' per-file-ignores live in `pyproject.toml`, each entry justified by a comment — extend them only with a reason, never to dodge a fixable finding. Everything is type-annotated (rows/groups are TypedDicts in `core.py`); `flatten_rows` takes its criteria as a frozen `RowFilter` dataclass and the picker loop is split into small `_handle_*`/`_build_frame` helpers to satisfy the complexity rules.
- **Everything sortable is sorted** — enforced automatically by the [codesorter](https://github.com/praw-dev/CodeSorter) pre-commit hook, which orders functions and classes, constants, dict keys, keyword arguments, and keyword-only parameters within each module, with `_`-prefixed names first. Parameters are mandatory-keyword (`*`), declared and passed in alphabetical order; the only exception is when the function name makes a positional argument's meaning 100% obvious (`mung_path(path)`, `truncate(text)`, `_die(message)`...), in which case it is mandatory-positional (`/`). Plain positional-or-keyword parameters never appear (lambdas excepted — they can't express `/`). Deliberate exceptions such as `AGE_WINDOWS` (Ctrl-T cycle order), span lists, and workflow `steps` (execution order) are preserved, since the hook never reorders list contents.
- Commits are conventional-commit style (`feat:`, `fix:`, `chore:`, `polish:`). No standalone `docs:` or `test:` commits — documentation and test changes ride along in the feature or bugfix commit they belong to.
