"""Curses layer: colors, drawing, the picker event loop, and main()."""

from __future__ import annotations

import argparse
import contextlib
import curses
import dataclasses
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from reclaude.core import (
    MS_PER_DAY,
    MS_PER_HOUR,
    Launch,
    RowFilter,
    clamp_scroll,
    flatten_rows,
    group_by_home,
    live_sessions,
    parse_history,
    row_spans,
    transcript_exists,
    truncate,
    version,
)

if TYPE_CHECKING:
    from typing import NoReturn

    from reclaude.core import DirRow, Group, SessionRow

_BACKSPACE_KEYS = frozenset({8, 127, curses.KEY_BACKSPACE})
_ENTER_KEYS = frozenset({10, 13, curses.KEY_ENTER})
_EXPAND_KEYS = frozenset({ord("\t"), curses.KEY_RIGHT})
_KEY_CTRL_T = 20
_KEY_CTRL_W = 23
_KEY_ESCAPE = 27
_MIN_ROWS_WITH_FOOTER = 2
_PRINTABLE_KEYS = range(32, 127)
# AGE_WINDOWS is in Ctrl-T cycle order (coarsest to finest), not sorted.
AGE_WINDOWS = [
    ("all", None),
    ("1mo", 30 * MS_PER_DAY),
    ("1w", 7 * MS_PER_DAY),
    ("1d", MS_PER_DAY),
    ("1h", MS_PER_HOUR),
]
# COLOR_KEYS must cover every key that core.row_spans emits.
COLOR_KEYS = ("flash", "gone", "orphan", "path", "running", "text", "time")
FLASH_CONFIRM = "this directory already has a running session — resume anyway? (y/n)"
FLASH_GONE = "directory no longer exists"
FLASH_RUNNING = "that session is already running"
HELP = (
    "↑↓ move · ⏎ resume · →/⇥ expand · ← collapse · ^W missing · "
    "^T age · type to filter · q quit"
)
HISTORY_PATH = Path("~/.claude/history.jsonl").expanduser()


@dataclasses.dataclass(frozen=True, kw_only=True)
class _Frame:
    """Everything _draw needs to paint one screenful."""

    footer: str
    footer_attr: int
    render_rows: list[tuple[list[tuple[str, str]], int]]
    scroll_top: int
    selection: int
    title: str


@dataclasses.dataclass(frozen=True, kw_only=True)
class _PickerContext:
    """Inputs that stay fixed for the lifetime of one picker run."""

    attrs: dict[str, int]
    busy: set[str]
    groups: list[Group]
    home: str
    running_ids: set[str]


@dataclasses.dataclass(kw_only=True)
class _PickerState:
    """Mutable UI state for one picker run."""

    age_index: int = 0
    expanded: set[str] = dataclasses.field(default_factory=set)
    filter_text: str = ""
    flash: str = ""
    pending: Launch | None = None
    scroll_top: int = 0
    selection: int = 0
    show_missing: bool = True


def _addstr(stdscr: curses.window, /, *, attr: int, text: str, x: int, y: int) -> int:
    """Write text at (y, x), clipped to the screen's last writable column.

    Centralizes the "at most maxx - 1 columns, tolerate curses errors" rule
    (tiny terminals, bottom-right cell quirk).

    Returns:
        The number of columns written (0 when nothing fits).

    """
    avail = stdscr.getmaxyx()[1] - 1 - x
    if avail <= 0:
        return 0
    clipped = truncate(text, width=avail)
    with contextlib.suppress(curses.error):
        stdscr.addnstr(y, x, clipped, avail, attr)
    return len(clipped)


def _build_frame(
    *,
    context: _PickerContext,
    now_ms: float,
    rows: list[DirRow | SessionRow],
    state: _PickerState,
) -> _Frame:
    """Assemble the next frame from the visible rows and UI state.

    Consumes state.flash: a pending flash message becomes this frame's
    footer, then clears.

    Returns:
        The frame for _draw.

    """
    render_rows = [
        (
            row_spans(row, home=context.home, now_ms=now_ms),
            curses.A_DIM if (row["busy"] or row["cls"].kind == "gone") else 0,
        )
        for row in rows
    ]
    if state.pending is not None:
        footer, footer_attr = FLASH_CONFIRM, context.attrs["flash"]
    elif state.flash:
        footer, footer_attr = state.flash, context.attrs["flash"]
        state.flash = ""
    elif state.filter_text:
        footer, footer_attr = f"filter: {state.filter_text}▏", curses.A_DIM
    else:
        footer, footer_attr = HELP, curses.A_DIM
    title = "reclaude — recent sessions"
    label, window = AGE_WINDOWS[state.age_index]
    if window is not None:
        title += f" · ≤{label}"
    if not state.show_missing:
        title += " · missing hidden"
    return _Frame(
        footer=footer,
        footer_attr=footer_attr,
        render_rows=render_rows,
        scroll_top=state.scroll_top,
        selection=state.selection,
        title=title,
    )


def _collapse_row(*, rows: list[DirRow | SessionRow], state: _PickerState) -> None:
    """Collapse the selected dir, or jump from a session row to its dir."""
    row = rows[state.selection]
    if row["kind"] == "session":
        for index in range(state.selection - 1, -1, -1):
            if rows[index]["kind"] == "dir" and rows[index]["group"] is row["group"]:
                state.selection = index
                break
    else:
        state.expanded.discard(row["group"]["path"])


def _die(message: str, /) -> NoReturn:
    """Print an error to stderr and exit non-zero."""
    sys.stderr.write(f"reclaude: {message}\n")
    sys.exit(1)


def _draw(stdscr: curses.window, /, *, attrs: dict[str, int], frame: _Frame) -> int:
    """Paint the frame: title, span rows with selection bar, footer.

    Returns:
        The new scroll offset after keeping the selection visible.

    """
    stdscr.erase()
    maxy, maxx = stdscr.getmaxyx()
    # maxy==1: header only; maxy==2: header (y=0) + footer (y=1), no body rows.
    body = max(0, maxy - 2)
    scroll_top = clamp_scroll(
        body=body, scroll_top=frame.scroll_top, selection=frame.selection
    )
    _addstr(stdscr, attr=curses.A_BOLD, text=frame.title, x=0, y=0)
    visible = frame.render_rows[scroll_top : scroll_top + body]
    for index, (spans, extra_attr) in enumerate(visible):
        row_attr = extra_attr | (
            curses.A_REVERSE if scroll_top + index == frame.selection else 0
        )
        x = 0
        for text, key in spans:
            if x >= maxx - 1:
                break
            x += _addstr(
                stdscr,
                attr=attrs.get(key, curses.A_NORMAL) | row_attr,
                text=text,
                x=x,
                y=1 + index,
            )
        if x < maxx - 1:  # pad so the selection bar spans the line
            _addstr(stdscr, attr=row_attr, text=" " * (maxx - 1 - x), x=x, y=1 + index)
    if maxy >= _MIN_ROWS_WITH_FOOTER:
        _addstr(stdscr, attr=frame.footer_attr, text=frame.footer, x=0, y=maxy - 1)
    stdscr.refresh()
    return scroll_top


def _exec_claude(*, launch: Launch) -> NoReturn:
    """Chdir to the picked directory and exec claude on the picked session."""
    for value in (launch.session_id, launch.worktree_name):
        # Option values come from history.jsonl; never let one be parsed as
        # an option.
        if value is not None and value.startswith("-"):
            _die(f"refusing option-like argument {value!r}")
    try:
        os.chdir(launch.path)
    except OSError as error:
        _die(f"cannot chdir to {launch.path}: {error}")
    try:
        # exec'ing claude from PATH is this program's entire purpose.
        os.execvp("claude", launch.argv)  # noqa: S606, S607
    except OSError as error:
        _die(f"cannot exec claude: {error}")


def _handle_confirm_key(key: int, /, *, state: _PickerState) -> Launch | None:
    """Resolve the pending busy-dir confirmation against one keypress.

    Clears state.pending unconditionally: 'y'/'Y' confirms the stashed launch,
    any other key (including Esc, navigation, or a resize) cancels it.

    Returns:
        The confirmed Launch, or None when the prompt was cancelled.

    """
    pending = state.pending
    state.pending = None
    if key in {ord("Y"), ord("y")}:
        return pending
    return None


def _handle_filter_key(key: int, /, *, state: _PickerState) -> bool:
    """Apply a filter/toggle key (Esc, backspace, ^W, ^T, printable) to state.

    Any change to the filter criteria resets the selection and scroll.

    Returns:
        True when the picker should quit (Esc or q with an empty filter).

    """
    before = (state.age_index, state.filter_text, state.show_missing)
    if key == _KEY_ESCAPE:
        if not state.filter_text:
            return True
        state.filter_text = ""
    elif key in _BACKSPACE_KEYS:
        state.filter_text = state.filter_text[:-1]
    elif key == _KEY_CTRL_W:  # toggle missing dirs
        state.show_missing = not state.show_missing
    elif key == _KEY_CTRL_T:  # cycle age filter
        state.age_index = (state.age_index + 1) % len(AGE_WINDOWS)
    elif key in _PRINTABLE_KEYS:
        character = chr(key)
        if character == "q" and not state.filter_text:
            return True
        state.filter_text += character
    if (state.age_index, state.filter_text, state.show_missing) != before:
        state.scroll_top = 0
        state.selection = 0
    return False


def _handle_nav_key(
    key: int,
    /,
    *,
    rows: list[DirRow | SessionRow],
    running_ids: set[str],
    state: _PickerState,
) -> Launch | None:
    """Apply a navigation key (arrows, enter, tab) to state.

    Returns:
        A Launch when enter resumes a session, else None.

    """
    if key == curses.KEY_UP:
        state.selection = max(0, state.selection - 1)
    elif key == curses.KEY_DOWN and rows:
        state.selection = min(len(rows) - 1, state.selection + 1)
    elif key in _ENTER_KEYS and rows:
        return _select_row(rows=rows, running_ids=running_ids, state=state)
    elif key in _EXPAND_KEYS and rows:
        if rows[state.selection]["kind"] == "dir":
            state.expanded.add(rows[state.selection]["group"]["path"])
    elif key == curses.KEY_LEFT and rows:
        _collapse_row(rows=rows, state=state)
    return None


def _launch(*, row: DirRow | SessionRow, session_id: str) -> Launch:
    """Build the Launch for a row's session.

    Returns:
        A worktree-resurrecting Launch for orphaned worktrees, else a plain
        resume in the row's directory.

    """
    classification = row["cls"]
    if classification.kind == "orphan-worktree":
        return Launch(
            path=classification.repo,
            session_id=session_id,
            worktree_name=classification.name,
        )
    return Launch(path=row["group"]["path"], session_id=session_id)


def _load_groups() -> list[Group]:
    """Read history.jsonl and group resumable sessions by home directory.

    Returns:
        The non-empty group list; exits with an error otherwise.

    """
    try:
        with HISTORY_PATH.open(encoding="utf-8") as file:
            entries = parse_history(file)
    except OSError as error:
        _die(f"cannot read {HISTORY_PATH}: {error}")
    groups = group_by_home(entries=entries, transcript_exists=transcript_exists)
    if not groups:
        _die("no resumable sessions found in history")
    return groups


def _parse_args() -> None:
    """Handle --version/--help; the picker itself takes no other arguments.

    argparse exits the process for --version, --help, or an unknown argument,
    so a normal return means there is nothing to do and main() proceeds.
    """
    parser = argparse.ArgumentParser(
        description="Curses picker for resuming Claude Code sessions.",
        prog="reclaude",
    )
    parser.add_argument("--version", action="version", version=f"reclaude {version()}")
    parser.parse_args()


def _select_row(
    *, rows: list[DirRow | SessionRow], running_ids: set[str], state: _PickerState
) -> Launch | None:
    """Resolve enter on the selected row.

    A dir row launches its newest visible session (display = action). Resuming
    the session that is itself already running is refused outright (two
    processes on one transcript would corrupt it); resuming a *different*
    session in a busy dir arms a y/n confirmation (state.pending) instead of
    launching — the shared working tree is the caller's risk to accept.

    Returns:
        The Launch when the row resumes immediately, or None when it is gone,
        already running, or now awaiting confirmation.

    """
    row = rows[state.selection]
    if row["cls"].kind == "gone":
        state.flash = FLASH_GONE
        return None
    if row["kind"] == "session":
        session_id = row["session"]["session_id"]
        running = row["running"]
    else:
        session_id = row["vis_sessions"][0]["session_id"]
        running = session_id in running_ids
    if running:
        state.flash = FLASH_RUNNING
        return None
    launch = _launch(row=row, session_id=session_id)
    if row["busy"]:
        state.pending = launch
        return None
    return launch


def _visible_rows(
    *, context: _PickerContext, now_ms: float, state: _PickerState
) -> list[DirRow | SessionRow]:
    """Compute the currently visible rows for the picker state.

    Returns:
        The flatten_rows result for the current filter, age window, and
        expansion state.

    """
    _, window = AGE_WINDOWS[state.age_index]
    min_ts = now_ms - window if window is not None else None
    return flatten_rows(
        criteria=RowFilter(
            busy=context.busy,
            expanded=state.expanded,
            filter_text=state.filter_text,
            home=context.home,
            min_ts=min_ts,
            running_ids=context.running_ids,
            show_missing=state.show_missing,
        ),
        groups=context.groups,
    )


def init_colors() -> dict[str, int]:
    """Map color keys to curses attributes; monochrome fallback.

    Returns:
        A curses attribute per COLOR_KEYS entry.

    """
    attrs = dict.fromkeys(COLOR_KEYS, curses.A_NORMAL)
    attrs["gone"] = curses.A_DIM
    attrs["path"] = curses.A_BOLD
    try:
        curses.start_color()
        curses.use_default_colors()
        if not curses.has_colors():
            return attrs
        for index, (key, color) in enumerate(
            [
                ("flash", curses.COLOR_RED),
                ("orphan", curses.COLOR_MAGENTA),
                ("running", curses.COLOR_YELLOW),
                ("time", curses.COLOR_CYAN),
            ],
            start=1,
        ):
            curses.init_pair(index, color, -1)
            attrs[key] = curses.color_pair(index)
    except curses.error:
        pass
    return attrs


def main() -> None:
    """Pick a recent Claude Code session and exec claude on it."""
    _parse_args()
    groups = _load_groups()
    busy, running_ids = live_sessions()
    if not sys.stdout.isatty():
        _die("needs an interactive terminal")
    os.environ.setdefault("ESCDELAY", "25")
    result = curses.wrapper(
        lambda stdscr: run_picker(
            stdscr, busy=busy, groups=groups, running_ids=running_ids
        )
    )
    if result is None:
        return
    _exec_claude(launch=result)


def run_picker(
    stdscr: curses.window,
    /,
    *,
    busy: set[str],
    groups: list[Group],
    running_ids: set[str],
) -> Launch | None:
    """Run the picker event loop.

    Returns:
        The Launch for the picked session, or None when the user quit.

    """
    with contextlib.suppress(curses.error):
        curses.curs_set(0)
    stdscr.keypad(True)  # noqa: FBT003 — the curses API is positional-only
    context = _PickerContext(
        attrs=init_colors(),
        busy=busy,
        groups=groups,
        home=str(Path.home()),
        running_ids=running_ids,
    )
    state = _PickerState()
    while True:
        now_ms = int(time.time() * 1000)
        rows = _visible_rows(context=context, now_ms=now_ms, state=state)
        state.selection = min(state.selection, len(rows) - 1) if rows else 0
        frame = _build_frame(context=context, now_ms=now_ms, rows=rows, state=state)
        state.scroll_top = _draw(stdscr, attrs=context.attrs, frame=frame)
        key = stdscr.getch()
        if state.pending is not None:
            launch = _handle_confirm_key(key, state=state)
            if launch is not None:
                return launch
            continue
        launch = _handle_nav_key(
            key, rows=rows, running_ids=context.running_ids, state=state
        )
        if launch is not None:
            return launch
        if _handle_filter_key(key, state=state):
            return None
