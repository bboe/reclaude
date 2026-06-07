# reclaude

A keyboard-driven curses picker for resuming [Claude Code](https://claude.com/claude-code)
sessions. It reads `~/.claude/history.jsonl` and shows your recent project directories as an
expandable tree with sessions inline, so you can jump back into a conversation without
remembering its session id.

- **Tree view** of recent directories; expand one to see its individual sessions, each shown
  with its time and opening prompt.
- **Live-session locks** — directories with a running `claude` process are marked so you don't
  collide with an active session.
- **Worktree resurrection** — sessions from deleted git worktrees can be brought back; reclaude
  re-runs them via `claude --worktree`, which recreates the worktree and finds the transcript.
- On selection it `chdir`s into the directory and `exec`s `claude --resume <id>` (display always
  matches the action — the row you see is the session you get).

## Install

```bash
uv tool install reclaude     # recommended
pipx install reclaude
pip install reclaude
```

## Usage

```bash
reclaude          # or: python -m reclaude
```

| Key            | Action                                            |
| -------------- | ------------------------------------------------- |
| `↑` / `↓`      | Move selection                                    |
| `Enter`        | Expand a directory, or resume the selected session |
| *type*         | Incrementally filter by directory / prompt text   |
| `Ctrl-W`       | Toggle showing directories whose path is gone     |
| `Ctrl-T`       | Cycle the age window (how far back to look)        |
| `Backspace`    | Delete a filter character                         |
| `q` / `Esc`    | Quit (`q` quits only when the filter is empty)    |

## Requirements

- A POSIX system with an interactive terminal (TTY).
- [Claude Code](https://claude.com/claude-code) installed and on your `PATH`.
- Python 3.10+. No third-party dependencies (Python standard library only).

## License

BSD-2-Clause. See [LICENSE](LICENSE).
