"""Pure, curses-free logic: history parsing, grouping, classification, rows."""
import json
import os
import re

MAX_DIRS = 30
PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
SESSIONS_DIR = os.path.expanduser("~/.claude/sessions")
WORKTREE_RE = re.compile(r"^(?P<repo>.+)/\.claude/worktrees/(?P<name>[^/]+)$")


def _is_busy(path, *, busy):
    return os.path.realpath(path) in busy


def _row_spans(row, *, home, now_ms):
    """Render a flatten_rows row as [(text, colorkey)] spans.

    Color keys: "gone", "orphan", "path", "running", "text", "time" — mapped
    to curses attributes by tui.init_colors(); tui.COLOR_KEYS must cover
    every key emitted here.
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
