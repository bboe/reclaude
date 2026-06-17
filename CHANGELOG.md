# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `reclaude --version` prints the installed version (and `reclaude.__version__`
  exposes it), sourced from the package metadata so there is a single source of
  truth.

### Changed
- A directory that already has a running `claude` is no longer hard-locked.
  Resuming a *different* session there now asks for `y/n` confirmation before
  launching, so you can deliberately run a second session in the same working
  tree. Resuming the session that is *itself* already running is still refused
  outright, since two processes sharing one transcript would corrupt it.

## [0.2.0] - 2026-06-08

### Added
- Live-session detection now works on macOS (and other POSIX systems without
  `/proc`): directories with a running `claude` are marked busy and their
  session ids surfaced, using `ps`/`lsof` instead of reading `/proc`. The Linux
  `/proc` path is unchanged.

### Fixed
- Directories whose path contains an underscore (e.g.
  `~/src/claude_throwaway_session`) now resolve. Path munging maps `_` → `-` to
  match Claude Code's `~/.claude/projects/<dir>` naming, so their sessions show
  up in the picker instead of silently never appearing.

## [0.1.0] - 2026-06-06

### Added
- Initial release: a keyboard-driven curses picker for resuming Claude Code
  sessions, reading `~/.claude/history.jsonl`.
- Tree view of recent project directories; expand one to see its sessions
  inline, each shown with its time and opening prompt.
- Live-session locks — directories with a running `claude` process are marked
  so you don't collide with an active session.
- Worktree resurrection — sessions from deleted git worktrees can be brought
  back, re-run via `claude --worktree`, which recreates the worktree and finds
  the transcript.
- On selection, `chdir`s into the chosen directory and execs
  `claude --resume <id>`.

[Unreleased]: https://github.com/bboe/reclaude/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/bboe/reclaude/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/bboe/reclaude/releases/tag/v0.1.0
