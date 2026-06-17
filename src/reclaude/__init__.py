"""reclaude: curses picker for recent Claude Code sessions.

Reads ~/.claude/history.jsonl, shows recent project directories as an
expandable tree (sessions inline under each directory), marks directories
with a running claude session as locked, resurrects sessions from deleted
git worktrees via `claude --worktree`, then chdirs and execs claude.
"""

from reclaude.core import version
from reclaude.tui import main

__all__ = ["__version__", "main"]
__version__ = version()
