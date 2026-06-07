# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`reclaude` is a stdlib-only Python curses picker for resuming Claude Code sessions. It reads `~/.claude/history.jsonl`, shows recent project directories as an expandable tree (sessions inline), locks directories that already have a running claude session, can resurrect sessions from deleted git worktrees, then chdirs and execs `claude`. Packaged for PyPI; installed with `uv tool install reclaude` (or `uv tool install --editable .` for hacking), which puts the `reclaude` console script on `PATH`.

## Commands

```bash
python3 -m pytest -q                                   # run all tests (from repo root)
python3 -m pytest tests/test_reclaude.py::test_mung_path -v   # single test
uv run --group dev --locked pytest -q                   # tests in an isolated env (as CI does)
uv build                                                # build sdist + wheel into dist/
reclaude                                                # run (needs a real TTY)
```

No third-party runtime dependencies (Python 3.10+ stdlib; pytest for tests). `[tool.pytest.ini_options] pythonpath = ["src"]` in `pyproject.toml` lets `python3 -m pytest` import the package from the repo root without an install. Releases publish to PyPI via `.github/workflows/publish.yml` (trusted publishing) on a `v*` tag push.

## Architecture

`reclaude` is a `src/` package (`src/reclaude/`) of two modules: pure logic in `core.py`, curses layer in `tui.py`. `__init__.py` only re-exports `main` (keeping the `reclaude:main` entry point stable); `__main__.py` wires up `python -m reclaude`.

1. **`core.py` (pure, curses-free, unit-tested):** `parse_history` → `group_by_home` builds per-directory groups of sessions; `classify_dir` tags each dir live / orphan-worktree / gone; `live_sessions` finds busy dirs + running session ids; `flatten_rows` turns groups + UI state (expansion, typed filter, age window, missing toggle) into the visible row list; `_row_spans` renders a row as `(text, colorkey)` spans. All filesystem/proc access is injectable (`isdir=`, `transcript_exists=`, `proc_root=`, `sessions_dir=`) so tests are hermetic — keep it that way. Tests import `reclaude.core`.
2. **`tui.py` (curses, no unit tests):** `init_colors` maps colorkeys → curses attrs (monochrome fallback), `_draw` renders span rows, `run_picker` is the event loop returning a launch tuple, `main` execs claude. Verified via throwaway fake-stdscr harnesses (scripted `getch`, recorded `addnstr`) in /tmp — never committed — plus manual testing by Bryce.

Data flow: `history.jsonl` → entries → groups (sessions attributed to their **home** dir) → rows → spans → screen; picker returns `("resume", path, id)` or `("worktree", repo, name, id)` → `os.chdir` + `os.execvp`.

### Invariants worth protecting

- **Display = action.** A dir row shows the time/prompt of `vis_sessions[0]` and Enter resumes exactly that session id. Never reintroduce `claude --continue` — it can disagree with what's displayed.
- **Filters before the MAX_DIRS cap** in `flatten_rows`, so hiding noise surfaces older live dirs.
- **Printable keys feed the incremental filter.** New shortcuts must be control keys (Ctrl-W=23 toggles missing dirs, Ctrl-T=20 cycles the age window); `q` quits only when the filter is empty.
- `tui.COLOR_KEYS` must cover every key `core._row_spans` emits.
- All `addnstr` calls write at most `maxx - 1` columns and are wrapped in `try/except curses.error` (tiny terminals, bottom-right quirk).

## Empirically verified Claude Code facts (the whole design rests on these)

- `claude --resume <id>` / `--continue` only find transcripts under `~/.claude/projects/<munged-cwd>/` for the **current** directory. Munging: `/` and `.` → `-` (deterministic; un-munging is ambiguous — only ever mung).
- A session's transcript lives under the directory the session **started** in and never moves, even if the session later changed cwd (EnterWorktree etc.). Hence `group_by_home`: first project in history = the only resumable location.
- Deleted worktree sessions resurrect via `cd <repo> && claude --worktree <name> --resume <id>` — claude recreates `<repo>/.claude/worktrees/<name>` (branch `worktree-<name>`, base per `worktree.baseRef`) and finds the transcript because the path matches again.
- `~/.claude/sessions/<pid>.json` describes live claude processes (`{pid, sessionId, cwd, ...}`). Stale files survive crashes — always validate `/proc/<pid>/comm == "claude"` before trusting one.
- `history.jsonl` lines: `{"display", "pastedContents", "timestamp"(ms), "project", "sessionId"}`; `display` can contain newlines/tabs (flattened in `parse_history`).

If claude changes any of this, re-verify empirically (cheap probe: `claude --resume <id> --fork-session --model haiku --print "Reply with only the word ok"` from the directory under test; clean up the forked transcript afterwards).

## Workflow conventions

- TDD for pure functions; the curses layer changes get fake-stdscr smoke tests instead.
- **Everything sortable is sorted.** Functions lexicographically within each module (ASCII order, so `_private` first); dict keys, container items, TOML/YAML keys, and constants wherever order has no semantic meaning. Parameters after the first are keyword-only (`*`), declared and passed in alphabetical order. Workflow YAML keys are fully sorted, including top-level (`jobs`/`name`/`on`) and job-level keys. Deliberate exceptions: `AGE_WINDOWS` (Ctrl-T cycle order), `classify_dir`'s return tuple, span lists, and workflow `steps` (execution order).
- Commits are conventional-commit style (`feat:`, `fix:`, `chore:`, `polish:`). No standalone `docs:` or `test:` commits — documentation and test changes ride along in the feature or bugfix commit they belong to.
