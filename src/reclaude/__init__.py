"""reclaude: curses picker for recent Claude Code sessions.

Reads ~/.claude/history.jsonl, shows recent project directories as an
expandable tree (sessions inline under each directory), marks directories
with a running claude session as locked, resurrects sessions from deleted
git worktrees via `claude --worktree`, then chdirs and execs claude.
"""
import curses
import json
import os
import re
import sys
import time

# AGE_WINDOWS is in Ctrl-T cycle order (coarsest to finest), not sorted.
AGE_WINDOWS = [("all", None), ("1mo", 30 * 86400_000), ("1w", 7 * 86400_000),
               ("1d", 86400_000), ("1h", 3600_000)]
# COLOR_KEYS must cover every key that _row_spans emits.
COLOR_KEYS = ("flash", "gone", "orphan", "path", "running", "text", "time")
FLASH_BUSY = "that directory already has a claude session running"
FLASH_GONE = "directory no longer exists"
HELP = ("↑↓ move · ⏎ resume · →/⇥ expand · ← collapse · ^W missing · "
        "^T age · type to filter · q quit")
HISTORY_PATH = os.path.expanduser("~/.claude/history.jsonl")
MAX_DIRS = 30
PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
SESSIONS_DIR = os.path.expanduser("~/.claude/sessions")
WORKTREE_RE = re.compile(r"^(?P<repo>.+)/\.claude/worktrees/(?P<name>[^/]+)$")


# Pure functions (unit-tested), sorted lexicographically.

def _is_busy(path, *, busy):
    return os.path.realpath(path) in busy


def _row_spans(row, *, home, now_ms):
    """Render a flatten_rows row as [(text, colorkey)] spans.

    Color keys: "gone", "orphan", "path", "running", "text", "time" — mapped
    to curses attributes by init_colors().
    """
    if row["kind"] == "dir":
        g = row["group"]
        vis = row["vis_sessions"]
        ts = vis[0]["ts"] if vis else g["last_ts"]
        spans = [(f"{relative_time(ts, now_ms=now_ms):>4}  ", "time"),
                 (abbreviate_path(g["path"], home=home), "path")]
        if row["busy"]:
            spans.append((" [running]", "running"))
        if row["cls"] == "orphan-worktree":
            spans.append((" [worktree gone]", "orphan"))
        elif row["cls"] == "gone":
            spans.append((" [gone]", "gone"))
        last = row["vis_sessions"][0]["display"] if row["vis_sessions"] else ""
        if last:
            spans.append((f"  —  {last}", "text"))
        return spans
    s = row["session"]
    spans = [("    ", "text"),
             (f"{relative_time(s['ts'], now_ms=now_ms):>4}  ", "time"),
             (s["display"] or "(no prompt)", "text")]
    if row.get("running"):
        spans.append((" [running]", "running"))
    return spans


def abbreviate_path(path, *, home):
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


def classify_dir(path, *, isdir=os.path.isdir):
    """Classify a session home dir.

    Returns (kind, repo, name): kind is "live" (dir exists), "orphan-worktree"
    (dir gone but it was <repo>/.claude/worktrees/<name> and <repo> exists —
    resumable via `claude --worktree <name> --resume <id>` from <repo>), or
    "gone". repo/name are None unless kind == "orphan-worktree".
    """
    if isdir(path):
        return ("live", None, None)
    m = WORKTREE_RE.match(path)
    if m and isdir(m.group("repo")):
        return ("orphan-worktree", m.group("repo"), m.group("name"))
    return ("gone", None, None)


def find_busy_dirs(*, proc_root="/proc"):
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


def flatten_rows(groups, *, busy, expanded, filt, home, isdir=os.path.isdir,
                 min_ts=None, running_ids, show_missing=True):
    """Flatten groups + expansion state into the visible row list.

    Dir row:     {"busy", "cls", "group", "kind": "dir", "name", "repo",
                  "vis_sessions"}
    Session row: {"busy", "cls", "group", "kind": "session", "name", "repo",
                  "running", "session"}.
    A session is visible iff it passes the age window (min_ts) and the text
    filter — a dir-path match admits all its sessions, otherwise the prompt
    text must contain the filter (both case-insensitive). A dir is shown iff
    it has visible sessions and passes the missing-dir filter; dirs are
    capped at MAX_DIRS. Expanded dirs render only their visible sessions.
    """
    f = filt.lower()
    kept = []
    for g in groups:
        path_match = f in abbreviate_path(g["path"], home=home).lower()
        vis = [s for s in g["sessions"]
               if (min_ts is None or s["ts"] >= min_ts)
               and (path_match or f in s["display"].lower())]
        if not vis:
            continue
        cls, repo, name = classify_dir(g["path"], isdir=isdir)
        if not show_missing and cls != "live":
            continue
        kept.append((g, vis, cls, repo, name))
    rows = []
    for g, vis, cls, repo, name in kept[:MAX_DIRS]:
        b = _is_busy(g["path"], busy=busy)
        rows.append({"busy": b, "cls": cls, "group": g, "kind": "dir",
                     "name": name, "repo": repo, "vis_sessions": vis})
        if g["path"] in expanded:
            for s in vis:
                rows.append({"busy": b, "cls": cls, "group": g,
                             "kind": "session", "name": name, "repo": repo,
                             "running": s["session_id"] in running_ids,
                             "session": s})
    return rows


def group_by_home(entries, *, transcript_exists):
    """Group sessions under their home dir (first project seen), newest first.

    A session's transcript lives where the session started, so that first
    directory is the only place `claude --resume` can find it. Sessions whose
    transcript no longer exists are dropped. Each group:
    {"last_ts", "path", "sessions": [{"display", "session_id", "ts"}, ...]}
    with sessions newest-first.
    """
    sessions = {}
    for e in entries:
        s = sessions.setdefault(e["session_id"],
                                {"display": "", "home": e["project"], "ts": 0})
        if e["ts"] >= s["ts"]:
            s["display"] = e["display"]
            s["ts"] = e["ts"]
    dirs = {}
    for sid, s in sessions.items():
        if not transcript_exists(s["home"], session_id=sid):
            continue
        dirs.setdefault(s["home"], []).append(
            {"display": s["display"], "session_id": sid, "ts": s["ts"]})
    groups = []
    for path, sess in dirs.items():
        sess.sort(key=lambda s: -s["ts"])
        groups.append({"last_ts": sess[0]["ts"], "path": path,
                       "sessions": sess})
    groups.sort(key=lambda g: -g["last_ts"])
    return groups


def live_sessions(*, proc_root="/proc", sessions_dir=None):
    """Busy dirs and running session ids from ~/.claude/sessions records.

    Each <pid>.json record counts only if /proc/<pid>/comm is "claude" (stale
    files survive crashes). Falls back to scanning /proc when the records
    yield nothing, so a claude started outside the session tracker still
    locks its directory (running ids unknown in that case).
    """
    busy, running = set(), set()
    try:
        names = os.listdir(sessions_dir or SESSIONS_DIR)
    except OSError:
        names = []
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(sessions_dir or SESSIONS_DIR, name)) as f:
                rec = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict):
            continue
        pid, cwd, sid = rec.get("pid"), rec.get("cwd"), rec.get("sessionId")
        if not (isinstance(pid, int) and isinstance(cwd, str)):
            continue
        try:
            with open(os.path.join(proc_root, str(pid), "comm")) as f:
                if f.read().strip() != "claude":
                    continue
        except OSError:
            continue
        busy.add(os.path.realpath(cwd))
        if isinstance(sid, str):
            running.add(sid)
    if not busy:
        busy = find_busy_dirs(proc_root=proc_root)
    return busy, running


def mung_path(path):
    """Munged ~/.claude/projects dir name for a path: '/' and '.' become '-'."""
    return path.replace("/", "-").replace(".", "-")


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
            "display": display,
            "project": project,
            "session_id": session_id,
            "ts": ts,
        })
    return entries


def relative_time(ts_ms, *, now_ms):
    """Compact age like '5s', '3m', '7h', '2d'."""
    secs = max(0, int((now_ms - ts_ms) / 1000))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def transcript_exists(home_dir, *, projects_dir=None, session_id):
    return os.path.isfile(transcript_path(home_dir, projects_dir=projects_dir,
                                          session_id=session_id))


def transcript_path(home_dir, *, projects_dir=None, session_id):
    return os.path.join(projects_dir or PROJECTS_DIR,
                        mung_path(home_dir), session_id + ".jsonl")


def truncate(s, *, width):
    if width <= 0:
        return ""
    if len(s) <= width:
        return s
    return s[: width - 1] + "…"


# Curses layer (no unit tests), sorted lexicographically.

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
