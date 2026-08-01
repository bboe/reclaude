# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`reclaude` is a stdlib-only Python curses picker for resuming Claude Code sessions. It reads `~/.claude/history.jsonl`, shows recent project directories as an expandable tree (sessions inline), refuses to re-attach a session that is already running and asks for y/n confirmation before resuming a second session in a directory that already has one live, can resurrect sessions from deleted git worktrees, then chdirs and execs `claude`. Packaged for PyPI; installed with `uv tool install reclaude` (or `uv tool install --editable .` for hacking), which puts the `reclaude` console script on `PATH`.

## Commands

```bash
python3 -m pytest -q                                   # run all tests (from repo root)
python3 -m pytest tests/test_reclaude.py::test_mung_path -v   # single test
uv run --group dev --locked pytest -q                   # tests in an isolated env (as CI does)
uv run --group dev pre-commit run --all-files           # ruff check + ruff format (lint.select = ALL, preview)
uv build                                                # build sdist + wheel into dist/
reclaude                                                # run (needs a real TTY)
reclaude --version                                      # print version (from package metadata), then exit
```

No third-party runtime dependencies (Python 3.10+ stdlib; pytest for tests). `[tool.pytest.ini_options] pythonpath = ["src"]` in `pyproject.toml` lets `python3 -m pytest` import the package from the repo root without an install. Releases publish to PyPI via `.github/workflows/pypi-publish.yml` (trusted publishing) on a `v*` tag push.

## Architecture

`reclaude` is a `src/` package (`src/reclaude/`) of two modules: pure logic in `core.py`, curses layer in `tui.py`. `__init__.py` only re-exports `main` (keeping the `reclaude:main` entry point stable); `__main__.py` wires up `python -m reclaude`.

1. **`core.py` (pure, curses-free, unit-tested):** `parse_history` → `group_by_home` builds per-directory groups of sessions; `classify_dir` tags each dir live / orphan-worktree / gone (a frozen `Classification`); `live_sessions` finds busy dirs + running session ids; `flatten_rows` turns groups + UI state (expansion, typed filter, age window, missing toggle) into the visible row list; `row_spans` renders a row as `(text, colorkey)` spans; `clamp_scroll` keeps the selection on screen. `transcript_home` resolves which munged project dir actually holds a session's transcript; `plan_launch` turns a session + its home into the `Launch` (which dir to chdir to, whether `--worktree` is safe, whether a `git worktree add` setup step and a confirmation are needed). All filesystem/proc/git access is injectable (`branch_exists=`, `isdir=`, `proc_root=`, `run=`, `sessions_dir=`, `transcript_home=`) so tests are hermetic — keep it that way (`run=` is the ps/lsof command runner the macOS path uses). Tests import `reclaude.core`.
2. **`tui.py` (curses, no unit tests):** `init_colors` maps colorkeys → curses attrs (monochrome fallback), `_draw` renders span rows, `run_picker` is the event loop returning a `Launch`, `main` execs claude. Verified via throwaway fake-stdscr harnesses (scripted `getch`, recorded `addnstr`) in /tmp — never committed — plus manual testing by Bryce.

Data flow: `history.jsonl` → entries → groups (sessions attributed to their **home** dir, each carrying the `store` dir that holds its transcript) → rows → spans → screen; picker returns a frozen `Launch` (path, session_id, optional confirm/setup/worktree_name; `Launch.argv` builds the claude command) → optional `setup` command → `os.chdir` + `os.execvp`.

### Invariants worth protecting

- **Display = action.** A dir row shows the time/prompt of `vis_sessions[0]` and Enter resumes exactly that session id. Never reintroduce `claude --continue` — it can disagree with what's displayed. A dir row's path is the repo but its launch may land in a worktree, so the row carries that session's `[wt:<name>]` tag to keep the two in agreement.
- **Re-attaching a live session is refused; a second session in a busy dir is confirmed.** Resuming a session whose id is in `running_ids` is hard-blocked (two processes on one transcript corrupts it). Resuming a *different* session in a busy dir stashes the `Launch` in `_PickerState.pending` and arms a y/n footer prompt — only `y` proceeds, accepting the shared-working-tree risk. The confirm key is handled in `run_picker` *before* the filter, so y/n there never feed the incremental filter. A worktree can't isolate a resume: transcripts are bound to the dir the session was launched from.
- **One confirm mechanism.** A `Launch` carrying a `confirm` message arms the same `_PickerState.pending` prompt as a busy dir, and `_build_frame` shows that message in place of `FLASH_CONFIRM`. Never add a second, separate prompt path.
- **Never let claude create a worktree.** `plan_launch` passes `--worktree <name>` only when that worktree already exists, because creating one resets branch `worktree-<name>` to the base ref and orphans its commits. A missing worktree is restored from its surviving branch via `Launch.setup` instead; only a missing branch takes the lossy path, and that one must be gated by `Launch.confirm`.
- **`Launch.setup` is the only thing reclaude may run before exec** — a fixed argv built by `plan_launch`, never a shell string, and the only place reclaude modifies a repo.
- **Filters before the MAX_DIRS cap** in `flatten_rows`, so hiding noise surfaces older live dirs.
- **Worktree sessions nest under their repo.** `group_by_home` keys the group by the owning repo, and every session carries its own `home` — the dir history recorded. Anything derived from *where a session lives* (the resume command, `classify_dir`, the busy check, the `[wt:]` tag, path filtering) must read `session["home"]`, never `group["path"]`; one row routinely holds sessions homed in the repo and in several worktrees.
- **`busy` gates, `busy_any` informs.** A dir row's `busy` is the launch target's (`vis_sessions[0]`) and decides the confirm prompt; `busy_any` covers every visible session so a live claude anywhere under the row still shows `[running]`. A worktree session's claude often registers its cwd as the *repo*, so per-session busy alone would miss it.
- **Printable keys feed the incremental filter.** New shortcuts must be control keys (Ctrl-W=23 toggles missing dirs, Ctrl-T=20 cycles the age window); `q` quits only when the filter is empty.
- `tui.COLOR_KEYS` must cover every key `core.row_spans` emits.
- **Rendering stays cheap** — `row_spans` runs per visible row every keypress, so badges may only use what `classify_dir` already knows. The `[worktree gone]` badge is therefore store-based, never branch-based; the `git` branch check belongs in `plan_launch`, which runs once on Enter.
- All screen writes go through `tui._addstr`, which clips to `maxx - 1` columns and tolerates `curses.error` (tiny terminals, bottom-right quirk).

## Empirically verified Claude Code facts (the whole design rests on these)

- `claude --resume <id>` / `--continue` only find transcripts under `~/.claude/projects/<munged-cwd>/` for the **current** directory. Munging: `/`, `.`, and `_` → `-` (deterministic; un-munging is ambiguous — only ever mung). Resuming from an unrelated dir fails with `No conversation found with session ID`.
- A session's transcript lives under the directory claude was **launched** from — not the session's cwd, which `--worktree` and the in-session EnterWorktree tool both change without moving the transcript. So a worktree session's transcript is under `mung(worktree)` when claude was started *inside* the worktree, and under `mung(repo)` when it was started from the repo with `--worktree` or entered one mid-session. Verified on real sessions of both kinds.
- `history.jsonl`'s `project` is the cwd a prompt was typed in, so for **every** worktree session it is the worktree — it cannot tell you which of the two locations holds the transcript. `transcript_home` probes both; the answer is kept per-session as `store` and decides the resume command. Headless `--print` runs resolve this differently from interactive ones, so never verify this rule with `--print` alone.
- Worktree sessions store **memory** under `mung(repo)` regardless of all the above — memory is repo-scoped, transcripts are launch-dir-scoped. No worktree-munged project dir on a real machine has ever held a `memory/`.
- Claude **auto-deletes** a worktree on exit when there is nothing to preserve, including its `worktree-<name>` branch — and this can discard commits (observed: a committed `scratch.txt` left reachable from no ref). So "worktree missing" is common and usually not user action.
- `claude --worktree <name>` **resets** branch `worktree-<name>` to `worktree.baseRef` whenever it has to *create* the worktree, orphaning any commits on that branch. It leaves an already-existing worktree completely alone. Hence the invariant below: only ever pass `--worktree` when the worktree is already there.
- Resurrecting a deleted worktree with `--worktree <name> --resume <id>` brings back the **conversation only** — the worktree comes back empty at the base ref, so claude then "remembers" files that do not exist on disk. To bring the work back too, recreate the worktree from its surviving branch first (`git worktree add <path> worktree-<name>`) and then plain `--resume` from inside it; the branch is left untouched.
- `~/.claude/sessions/<pid>.json` describes live claude processes (`{pid, sessionId, cwd, procStart, startedAt(ms), ...}`). Stale files survive crashes — always validate the pid is a live claude before trusting one. Linux reads `/proc/<pid>/comm` (the literal `claude`); macOS/BSD have no `/proc`, so `ps -o comm= -p <pid>` is used instead — it's empty once the pid exits, and its output is the versioned binary path (`…/claude/versions/<version>`), which `_looks_like_claude` matches without matching `reclaude` itself. `find_busy_dirs`'s fallback scan is `/proc` on Linux, else `ps -axo pid=,comm=` + `lsof` for each cwd. Selection is `core.PROC_ROOT` (None ⇒ the ps/lsof backend), set once by `sys.platform`.
- `history.jsonl` lines: `{"display", "pastedContents", "timestamp"(ms), "project", "sessionId"}`; `display` can contain newlines/tabs (flattened in `parse_history`).
- Session names live in the transcript itself, as appended `{"type":"ai-title","aiTitle":...,"sessionId":...}` (auto-generated, re-appended when it changes) and `{"type":"custom-title","customTitle":...}` (`/rename`) lines. Newest custom title beats newest AI title. `core.session_title` extracts them with a backwards raw-byte search for line-anchored markers (never a per-line JSON parse — transcripts reach tens of MB); a title record merely *quoted* inside message content can't false-match because JSON escaping mangles the quotes mid-line.

If claude changes any of this, re-verify empirically (cheap probe: `claude --resume <id> --fork-session --model haiku --print "Reply with only the word ok"` from the directory under test; clean up the forked transcript afterwards).

## Workflow conventions

- TDD for pure functions; the curses layer changes get fake-stdscr smoke tests instead.
- ruff runs with `lint.select = ["ALL"]` + `preview = true`; formatting is ruff-format-owned. The small ignore list and the tests' per-file-ignores live in `pyproject.toml`, each entry justified by a comment — extend them only with a reason, never to dodge a fixable finding. Everything is type-annotated (rows/groups are TypedDicts in `core.py`); `flatten_rows` takes its criteria as a frozen `RowFilter` dataclass and the picker loop is split into small `_handle_*`/`_build_frame` helpers to satisfy the complexity rules.
- **Everything sortable is sorted** — enforced automatically by the [codesorter](https://github.com/praw-dev/CodeSorter) pre-commit hook, which orders functions and classes, constants, dict keys, keyword arguments, and keyword-only parameters within each module, with `_`-prefixed names first. Parameters are mandatory-keyword (`*`), declared and passed in alphabetical order; the only exception is when the function name makes a positional argument's meaning 100% obvious (`mung_path(path)`, `truncate(text)`, `_die(message)`...), in which case it is mandatory-positional (`/`). Plain positional-or-keyword parameters never appear (lambdas excepted — they can't express `/`). Deliberate exceptions such as `AGE_WINDOWS` (Ctrl-T cycle order), span lists, and workflow `steps` (execution order) are preserved, since the hook never reorders list contents.
- Commits are conventional-commit style (`feat:`, `fix:`, `chore:`, `polish:`). No standalone `docs:` or `test:` commits — documentation and test changes ride along in the feature or bugfix commit they belong to.
