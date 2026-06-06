#!/usr/bin/env python3
"""reclaude: curses picker for recent Claude Code sessions.

Reads ~/.claude/history.jsonl, shows recent project directories (drill-down
to individual sessions), refuses directories that already have a running
claude process, then chdirs and execs claude.
"""
import curses
import json
import os
import sys
import time

HISTORY_PATH = os.path.expanduser("~/.claude/history.jsonl")
MAX_DIRS = 30


def parse_history(lines):
    """Parse history.jsonl lines into entry dicts, skipping malformed lines."""
    entries = []
    for line in lines:
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        project = obj.get("project")
        session_id = obj.get("sessionId")
        ts = obj.get("timestamp")
        display = obj.get("display")
        if not (isinstance(project, str) and isinstance(session_id, str)
                and isinstance(ts, (int, float)) and not isinstance(ts, bool)):
            continue
        if isinstance(display, str):
            display = " ".join(display.split())
        else:
            display = ""
        entries.append({
            "project": project,
            "session_id": session_id,
            "ts": ts,
            "display": display,
        })
    return entries


def group_by_project(entries, dir_exists=os.path.isdir):
    """Group entries into per-directory groups, newest activity first.

    Each group: {"path", "last_ts", "sessions": [{"session_id", "ts", "display"}, ...]}
    with sessions newest-first. Directories failing dir_exists are dropped.
    """
    dirs = {}
    for e in entries:
        d = dirs.setdefault(e["project"], {"last_ts": 0, "sessions": {}})
        s = d["sessions"].setdefault(e["session_id"], {"ts": 0, "display": ""})
        if e["ts"] >= s["ts"]:
            s["ts"] = e["ts"]
            s["display"] = e["display"]
        d["last_ts"] = max(d["last_ts"], e["ts"])
    groups = []
    for path, d in dirs.items():
        if not dir_exists(path):
            continue
        sessions = [
            {"session_id": sid, "ts": s["ts"], "display": s["display"]}
            for sid, s in d["sessions"].items()
        ]
        sessions.sort(key=lambda s: -s["ts"])
        groups.append({"path": path, "last_ts": d["last_ts"], "sessions": sessions})
    groups.sort(key=lambda g: -g["last_ts"])
    return groups


def find_busy_dirs(proc_root="/proc"):
    """Return the set of realpath cwds of running `claude` processes."""
    busy = set()
    try:
        names = os.listdir(proc_root)
    except OSError:
        return busy
    for name in names:
        if not name.isdigit():
            continue
        base = os.path.join(proc_root, name)
        try:
            with open(os.path.join(base, "comm")) as f:
                if f.read().strip() != "claude":
                    continue
            busy.add(os.path.realpath(os.path.join(base, "cwd")))
        except OSError:
            continue  # process exited, or not ours to read
    return busy


def relative_time(ts_ms, now_ms):
    """Compact age like '5s', '3m', '7h', '2d'."""
    secs = max(0, int((now_ms - ts_ms) / 1000))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def abbreviate_path(path, home):
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


def truncate(s, width):
    if width <= 0:
        return ""
    if len(s) <= width:
        return s
    return s[: width - 1] + "…"


def _is_busy(path, busy):
    return os.path.realpath(path) in busy


def _draw(stdscr, title, rows, sel, top, footer):
    """rows: list of (text, busy). Returns new `top` after scroll adjustment."""
    stdscr.erase()
    maxy, maxx = stdscr.getmaxyx()
    # With maxy==1 there is no room for body rows or footer; with maxy==2 there
    # is room for the header (y=0) and footer (y=1) but no body rows.
    body = max(0, maxy - 2)  # rows available between header and footer
    if sel < top:
        top = sel
    elif body > 0 and sel >= top + body:
        top = sel - body + 1
    try:
        stdscr.addnstr(0, 0, truncate(title, maxx - 1), maxx - 1, curses.A_BOLD)
    except curses.error:
        pass
    for i, (text, is_busy) in enumerate(rows[top:top + body]):
        idx = top + i
        attr = curses.A_REVERSE if idx == sel else curses.A_NORMAL
        if is_busy:
            attr |= curses.A_DIM
        try:
            stdscr.addnstr(1 + i, 0, truncate(text, maxx - 1).ljust(maxx - 1), maxx - 1, attr)
        except curses.error:
            pass
    if maxy >= 2:
        try:
            stdscr.addnstr(maxy - 1, 0, truncate(footer, maxx - 1), maxx - 1, curses.A_DIM)
        except curses.error:
            pass
    stdscr.refresh()
    return top


def _dir_row(g, busy, now_ms, home):
    badge = " [running]" if _is_busy(g["path"], busy) else ""
    last = g["sessions"][0]["display"] if g["sessions"] else ""
    return (f"{relative_time(g['last_ts'], now_ms):>4}  "
            f"{abbreviate_path(g['path'], home)}{badge}  —  {last}")


def _session_row(s, now_ms):
    return f"{relative_time(s['ts'], now_ms):>4}  {s['display'] or '(no prompt)'}"


def run_picker(stdscr, groups, busy):
    """Returns ('continue', path) | ('resume', path, session_id) | None."""
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    stdscr.keypad(True)

    home = os.path.expanduser("~")

    HELP_DIRS = "↑↓ move · ⏎ continue · → sessions · type to filter · q quit"
    HELP_SESS = "↑↓ move · ⏎ resume · ←/esc back · q quit"

    # State
    view = "dirs"   # "dirs" or "sessions"
    sel = 0
    top = 0
    filt = ""
    flash = ""
    cur_group = None  # set when view == "sessions"

    while True:
        now_ms = int(time.time() * 1000)

        if view == "dirs":
            filtered = [
                g for g in groups
                if filt.lower() in abbreviate_path(g["path"], home).lower()
            ][:MAX_DIRS]
            rows = [(_dir_row(g, busy, now_ms, home), _is_busy(g["path"], busy))
                    for g in filtered]
            title = "reclaude — recent directories"
            if flash:
                footer = flash
                flash = ""
            elif filt:
                footer = f"filter: {filt}▏"
            else:
                footer = HELP_DIRS
        else:
            # sessions view
            sessions = cur_group["sessions"]
            dir_busy = _is_busy(cur_group["path"], busy)
            rows = [(_session_row(s, now_ms), dir_busy) for s in sessions]
            abbrev = abbreviate_path(cur_group["path"], home)
            title = f"sessions in {abbrev}"
            if dir_busy:
                title += "  [running — locked]"
            if flash:
                footer = flash
                flash = ""
            else:
                footer = HELP_SESS

        # Clamp selection
        n = len(rows)
        if n == 0:
            sel = 0
        else:
            sel = max(0, min(sel, n - 1))

        top = _draw(stdscr, title, rows, sel, top, footer)

        key = stdscr.getch()

        if view == "dirs":
            if key in (curses.KEY_UP,):
                if n > 0:
                    sel = max(0, sel - 1)
            elif key in (curses.KEY_DOWN,):
                if n > 0:
                    sel = min(n - 1, sel + 1)
            elif key in (curses.KEY_ENTER, 10, 13):
                if n == 0:
                    pass
                elif rows[sel][1]:  # busy
                    flash = "that directory already has a claude session running"
                else:
                    return ("continue", filtered[sel]["path"])
            elif key in (curses.KEY_RIGHT, ord("\t")):
                if n > 0:
                    cur_group = filtered[sel]
                    view = "sessions"
                    sel = 0
                    top = 0
            elif key == 27:  # Esc
                if filt:
                    filt = ""
                    sel = 0
                    top = 0
                else:
                    return None
            elif key in (curses.KEY_BACKSPACE, 127, 8):
                if filt:
                    filt = filt[:-1]
                    sel = 0
                    top = 0
            elif 32 <= key < 127:
                ch = chr(key)
                if ch == "q" and not filt:
                    return None
                filt += ch
                sel = 0
                top = 0
        else:
            # sessions view
            if key in (curses.KEY_UP,) or key == ord("k"):
                if n > 0:
                    sel = max(0, sel - 1)
            elif key in (curses.KEY_DOWN,) or key == ord("j"):
                if n > 0:
                    sel = min(n - 1, sel + 1)
            elif key in (curses.KEY_ENTER, 10, 13):
                if n == 0:
                    pass
                elif rows[sel][1]:  # busy
                    flash = "that directory already has a claude session running"
                else:
                    s = sessions[sel]
                    return ("resume", cur_group["path"], s["session_id"])
            elif key in (curses.KEY_LEFT, 27):  # Left or Esc
                view = "dirs"
                sel = 0
                top = 0
                cur_group = None
            elif key == ord("q"):
                return None


def main():
    try:
        with open(HISTORY_PATH, encoding="utf-8") as f:
            entries = parse_history(f)
    except OSError as e:
        sys.exit(f"reclaude: cannot read {HISTORY_PATH}: {e}")
    groups = group_by_project(entries)
    if not groups:
        sys.exit("reclaude: no resumable sessions found in history")
    busy = find_busy_dirs()
    os.environ.setdefault("ESCDELAY", "25")
    result = curses.wrapper(run_picker, groups, busy)
    if result is None:
        return
    if result[0] == "continue":
        path, argv = result[1], ["claude", "--continue"]
    else:
        path, argv = result[1], ["claude", "--resume", result[2]]
    try:
        os.chdir(path)
    except OSError as e:
        sys.exit(f"reclaude: cannot chdir to {path}: {e}")
    try:
        os.execvp("claude", argv)
    except OSError as e:
        sys.exit(f"reclaude: cannot exec claude: {e}")


if __name__ == "__main__":
    main()
