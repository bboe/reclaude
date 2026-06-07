"""Curses layer: colors, drawing, the picker event loop, and main()."""
import curses
import os
import sys
import time

from reclaude.core import (_row_spans, flatten_rows, group_by_home,
                           live_sessions, parse_history, transcript_exists,
                           truncate)

# AGE_WINDOWS is in Ctrl-T cycle order (coarsest to finest), not sorted.
AGE_WINDOWS = [("all", None), ("1mo", 30 * 86400_000), ("1w", 7 * 86400_000),
               ("1d", 86400_000), ("1h", 3600_000)]
# COLOR_KEYS must cover every key that core._row_spans emits.
COLOR_KEYS = ("flash", "gone", "orphan", "path", "running", "text", "time")
FLASH_BUSY = "that directory already has a claude session running"
FLASH_GONE = "directory no longer exists"
HELP = ("↑↓ move · ⏎ resume · →/⇥ expand · ← collapse · ^W missing · "
        "^T age · type to filter · q quit")
HISTORY_PATH = os.path.expanduser("~/.claude/history.jsonl")


def _die(message):
    """Print an error to stderr and exit non-zero."""
    print(f"reclaude: {message}", file=sys.stderr)
    sys.exit(1)


def _draw(stdscr, *, attrs, footer, footer_attr, render_rows, scroll_top,
          selection, title):
    """render_rows: list of (spans, extra_attr). Returns new scroll `top`."""
    stdscr.erase()
    maxy, maxx = stdscr.getmaxyx()
    # maxy==1: header only; maxy==2: header (y=0) + footer (y=1), no body rows.
    body = max(0, maxy - 2)
    if selection < scroll_top:
        scroll_top = selection
    elif body > 0 and selection >= scroll_top + body:
        scroll_top = selection - body + 1
    try:
        stdscr.addnstr(0, 0, truncate(title, width=maxx - 1), maxx - 1,
                       curses.A_BOLD)
    except curses.error:
        pass
    visible = render_rows[scroll_top:scroll_top + body]
    for i, (spans, extra_attr) in enumerate(visible):
        row_attr = extra_attr | (curses.A_REVERSE
                                 if scroll_top + i == selection else 0)
        x = 0
        for text, key in spans:
            avail = maxx - 1 - x
            if avail <= 0:
                break
            clipped = truncate(text, width=avail)
            try:
                stdscr.addnstr(1 + i, x, clipped, avail,
                               attrs.get(key, curses.A_NORMAL) | row_attr)
            except curses.error:
                pass
            x += len(clipped)
        if x < maxx - 1:  # pad so the selection bar spans the line
            try:
                stdscr.addnstr(1 + i, x, " " * (maxx - 1 - x), maxx - 1 - x,
                               row_attr)
            except curses.error:
                pass
    if maxy >= 2:
        try:
            stdscr.addnstr(maxy - 1, 0, truncate(footer, width=maxx - 1),
                           maxx - 1, footer_attr)
        except curses.error:
            pass
    stdscr.refresh()
    return scroll_top


def _launch(row, *, session_id):
    if row["cls"] == "orphan-worktree":
        return ("worktree", row["repo"], row["name"], session_id)
    return ("resume", row["group"]["path"], session_id)


def init_colors():
    """Map color keys to curses attributes; monochrome fallback."""
    attrs = {key: curses.A_NORMAL for key in COLOR_KEYS}
    attrs["gone"] = curses.A_DIM
    attrs["path"] = curses.A_BOLD
    try:
        curses.start_color()
        curses.use_default_colors()
        if not curses.has_colors():
            return attrs
        for i, (key, color) in enumerate([
                ("flash", curses.COLOR_RED),
                ("orphan", curses.COLOR_MAGENTA),
                ("running", curses.COLOR_YELLOW),
                ("time", curses.COLOR_CYAN)], start=1):
            curses.init_pair(i, color, -1)
            attrs[key] = curses.color_pair(i)
    except curses.error:
        pass
    return attrs


def main():
    try:
        with open(HISTORY_PATH, encoding="utf-8") as file:
            entries = parse_history(file)
    except OSError as error:
        _die(f"cannot read {HISTORY_PATH}: {error}")
    groups = group_by_home(entries, transcript_exists=transcript_exists)
    if not groups:
        _die("no resumable sessions found in history")
    busy, running_ids = live_sessions()
    if not sys.stdout.isatty():
        _die("needs an interactive terminal")
    os.environ.setdefault("ESCDELAY", "25")
    result = curses.wrapper(lambda stdscr: run_picker(
        stdscr, busy=busy, groups=groups, running_ids=running_ids))
    if result is None:
        return
    if result[0] == "worktree":
        _, path, name, session_id = result
        argv = ["claude", "--worktree", name, "--resume", session_id]
    else:
        _, path, session_id = result
        argv = ["claude", "--resume", session_id]
    for value in argv[2::2]:  # option values come from history.jsonl;
        if value.startswith("-"):  # never let one be parsed as an option
            _die(f"refusing option-like argument {value!r}")
    try:
        os.chdir(path)
    except OSError as error:
        _die(f"cannot chdir to {path}: {error}")
    try:
        os.execvp("claude", argv)
    except OSError as error:
        _die(f"cannot exec claude: {error}")


def run_picker(stdscr, *, busy, groups, running_ids):
    """Returns ('resume', path, id) | ('worktree', repo, name, id) | None."""
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    stdscr.keypad(True)
    attrs = init_colors()
    home = os.path.expanduser("~")

    selection, scroll_top, filter_text, flash = 0, 0, "", ""
    show_missing, age_index = True, 0
    expanded = set()

    while True:
        now_ms = int(time.time() * 1000)
        label, window = AGE_WINDOWS[age_index]
        min_ts = now_ms - window if window is not None else None
        rows = flatten_rows(groups, busy=busy, expanded=expanded,
                            filter_text=filter_text, home=home,
                            min_ts=min_ts, running_ids=running_ids,
                            show_missing=show_missing)
        render = [(_row_spans(row, home=home, now_ms=now_ms),
                    curses.A_DIM if (row["busy"] or row["cls"] == "gone")
                    else 0)
                  for row in rows]
        if flash:
            footer, footer_attr, flash = flash, attrs["flash"], ""
        elif filter_text:
            footer, footer_attr = f"filter: {filter_text}▏", curses.A_DIM
        else:
            footer, footer_attr = HELP, curses.A_DIM
        num_rows = len(rows)
        selection = max(0, min(selection, num_rows - 1)) if num_rows else 0
        title = "reclaude — recent sessions"
        if window is not None:
            title += f" · ≤{label}"
        if not show_missing:
            title += " · missing hidden"
        scroll_top = _draw(stdscr, attrs=attrs, footer=footer,
                           footer_attr=footer_attr, render_rows=render,
                           scroll_top=scroll_top, selection=selection,
                           title=title)

        key = stdscr.getch()
        if key == curses.KEY_UP:
            selection = max(0, selection - 1)
        elif key == curses.KEY_DOWN:
            selection = min(num_rows - 1, selection + 1) if num_rows else 0
        elif key in (10, 13, curses.KEY_ENTER) and num_rows:
            row = rows[selection]
            if row["busy"]:
                flash = FLASH_BUSY
            elif row["cls"] == "gone":
                flash = FLASH_GONE
            elif row["kind"] == "session":
                return _launch(row, session_id=row["session"]["session_id"])
            else:
                return _launch(
                    row, session_id=row["vis_sessions"][0]["session_id"])
        elif key in (ord("\t"), curses.KEY_RIGHT) and num_rows:
            if rows[selection]["kind"] == "dir":
                expanded.add(rows[selection]["group"]["path"])
        elif key == curses.KEY_LEFT and num_rows:
            row = rows[selection]
            if row["kind"] == "session":
                for i in range(selection - 1, -1, -1):
                    if (rows[i]["kind"] == "dir"
                            and rows[i]["group"] is row["group"]):
                        selection = i
                        break
            else:
                expanded.discard(row["group"]["path"])
        elif key == 27:  # Esc
            if filter_text:
                filter_text, selection, scroll_top = "", 0, 0
            else:
                return None
        elif key in (8, 127, curses.KEY_BACKSPACE):
            if filter_text:
                filter_text, selection, scroll_top = filter_text[:-1], 0, 0
        elif key == 23:  # Ctrl-W: toggle missing dirs
            show_missing = not show_missing
            selection, scroll_top = 0, 0
        elif key == 20:  # Ctrl-T: cycle age filter
            age_index = (age_index + 1) % len(AGE_WINDOWS)
            selection, scroll_top = 0, 0
        elif 32 <= key < 127:
            character = chr(key)
            if character == "q" and not filter_text:
                return None
            filter_text, selection, scroll_top = filter_text + character, 0, 0
