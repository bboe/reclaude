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


def _draw(stdscr, *, attrs, footer, footer_attr, render_rows, sel, title, top):
    """render_rows: list of (spans, extra_attr). Returns new scroll `top`."""
    stdscr.erase()
    maxy, maxx = stdscr.getmaxyx()
    # maxy==1: header only; maxy==2: header (y=0) + footer (y=1), no body rows.
    body = max(0, maxy - 2)
    if sel < top:
        top = sel
    elif body > 0 and sel >= top + body:
        top = sel - body + 1
    try:
        stdscr.addnstr(0, 0, truncate(title, width=maxx - 1), maxx - 1,
                       curses.A_BOLD)
    except curses.error:
        pass
    for i, (spans, extra) in enumerate(render_rows[top:top + body]):
        row_attr = extra | (curses.A_REVERSE if top + i == sel else 0)
        x = 0
        for text, key in spans:
            avail = maxx - 1 - x
            if avail <= 0:
                break
            t = truncate(text, width=avail)
            try:
                stdscr.addnstr(1 + i, x, t, avail, attrs.get(key, curses.A_NORMAL) | row_attr)
            except curses.error:
                pass
            x += len(t)
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
    return top


def _launch(row, *, session_id):
    if row["cls"] == "orphan-worktree":
        return ("worktree", row["repo"], row["name"], session_id)
    return ("resume", row["group"]["path"], session_id)


def init_colors():
    """Map color keys to curses attributes; monochrome fallback."""
    attrs = {k: curses.A_NORMAL for k in COLOR_KEYS}
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
        with open(HISTORY_PATH, encoding="utf-8") as f:
            entries = parse_history(f)
    except OSError as e:
        sys.exit(f"reclaude: cannot read {HISTORY_PATH}: {e}")
    groups = group_by_home(entries, transcript_exists=transcript_exists)
    if not groups:
        sys.exit("reclaude: no resumable sessions found in history")
    busy, running_ids = live_sessions()
    if not sys.stdout.isatty():
        sys.exit("reclaude: needs an interactive terminal")
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
    try:
        os.chdir(path)
    except OSError as e:
        sys.exit(f"reclaude: cannot chdir to {path}: {e}")
    try:
        os.execvp("claude", argv)
    except OSError as e:
        sys.exit(f"reclaude: cannot exec claude: {e}")


def run_picker(stdscr, *, busy, groups, running_ids):
    """Returns ('resume', path, id) | ('worktree', repo, name, id) | None."""
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    stdscr.keypad(True)
    attrs = init_colors()
    home = os.path.expanduser("~")

    sel, top, filt, flash = 0, 0, "", ""
    show_missing, age_idx = True, 0
    expanded = set()

    while True:
        now_ms = int(time.time() * 1000)
        label, window = AGE_WINDOWS[age_idx]
        min_ts = now_ms - window if window is not None else None
        rows = flatten_rows(groups, busy=busy, expanded=expanded, filt=filt,
                            home=home, min_ts=min_ts,
                            running_ids=running_ids,
                            show_missing=show_missing)
        render = [(_row_spans(r, home=home, now_ms=now_ms),
                    curses.A_DIM if (r["busy"] or r["cls"] == "gone") else 0)
                  for r in rows]
        if flash:
            footer, footer_attr, flash = flash, attrs["flash"], ""
        elif filt:
            footer, footer_attr = f"filter: {filt}▏", curses.A_DIM
        else:
            footer, footer_attr = HELP, curses.A_DIM
        n = len(rows)
        sel = max(0, min(sel, n - 1)) if n else 0
        title = "reclaude — recent sessions"
        if window is not None:
            title += f" · ≤{label}"
        if not show_missing:
            title += " · missing hidden"
        top = _draw(stdscr, attrs=attrs, footer=footer,
                    footer_attr=footer_attr, render_rows=render, sel=sel,
                    title=title, top=top)

        key = stdscr.getch()
        if key == curses.KEY_UP:
            sel = max(0, sel - 1)
        elif key == curses.KEY_DOWN:
            sel = min(n - 1, sel + 1) if n else 0
        elif key in (10, 13, curses.KEY_ENTER) and n:
            r = rows[sel]
            if r["busy"]:
                flash = FLASH_BUSY
            elif r["cls"] == "gone":
                flash = FLASH_GONE
            elif r["kind"] == "session":
                return _launch(r, session_id=r["session"]["session_id"])
            else:
                return _launch(r, session_id=r["vis_sessions"][0]["session_id"])
        elif key in (ord("\t"), curses.KEY_RIGHT) and n:
            if rows[sel]["kind"] == "dir":
                expanded.add(rows[sel]["group"]["path"])
        elif key == curses.KEY_LEFT and n:
            r = rows[sel]
            if r["kind"] == "session":
                for i in range(sel - 1, -1, -1):
                    if rows[i]["kind"] == "dir" and rows[i]["group"] is r["group"]:
                        sel = i
                        break
            else:
                expanded.discard(r["group"]["path"])
        elif key == 27:  # Esc
            if filt:
                filt, sel, top = "", 0, 0
            else:
                return None
        elif key in (8, 127, curses.KEY_BACKSPACE):
            if filt:
                filt, sel, top = filt[:-1], 0, 0
        elif key == 23:  # Ctrl-W: toggle missing dirs
            show_missing = not show_missing
            sel, top = 0, 0
        elif key == 20:  # Ctrl-T: cycle age filter
            age_idx = (age_idx + 1) % len(AGE_WINDOWS)
            sel, top = 0, 0
        elif 32 <= key < 127:
            ch = chr(key)
            if ch == "q" and not filt:
                return None
            filt, sel, top = filt + ch, 0, 0
