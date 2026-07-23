"""Pure, curses-free logic: history parsing, grouping, classification, rows."""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import re
import shutil

# subprocess only ever runs ps/lsof with a fixed argv (never a shell), to read
# another process's identity and cwd on POSIX systems without /proc (macOS/BSD).
import subprocess  # noqa: S404
import sys
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

# Transcript title records, in precedence order: a /rename (custom-title)
# beats the auto-generated ai-title; within each kind the newest record wins.
_TITLE_MARKERS = (
    ("customTitle", b'{"type":"custom-title"'),
    ("aiTitle", b'{"type":"ai-title"'),
)
MAX_DIRS = 30
MS_PER_DAY = 86_400_000
MS_PER_HOUR = 3_600_000
# Path chars that collapse to '-' in a ~/.claude/projects dir name (see mung_path).
MUNG_RE = re.compile(r"[/._ ]")
# /proc is read directly on Linux; None routes liveness/cwd through ps + lsof
# (macOS and other POSIX without /proc). Selected once, here, by platform.
PROC_ROOT = "/proc" if sys.platform == "linux" else None
PROJECTS_DIR = Path("~/.claude/projects").expanduser()
SECONDS_PER_DAY = MS_PER_DAY // 1000
SECONDS_PER_HOUR = MS_PER_HOUR // 1000
SECONDS_PER_MINUTE = 60
SESSIONS_DIR = Path("~/.claude/sessions").expanduser()
WORKTREE_RE = re.compile(r"^(?P<repo>.+)/\.claude/worktrees/(?P<name>[^/]+)$")


class BaseRow(TypedDict):
    """Fields shared by every flatten_rows row."""

    busy: bool
    cls: Classification
    group: Group
    kind: str


@dataclasses.dataclass(frozen=True, kw_only=True)
class Classification:
    """classify_dir's judgement of a session home directory."""

    kind: str  # "live" | "orphan-worktree" | "gone"
    name: str | None = None  # worktree name; orphan-worktree only
    repo: str | None = None  # owning repo; orphan-worktree only


class DirRow(BaseRow):
    """A directory row produced by flatten_rows."""

    vis_sessions: list[Session]


class Entry(TypedDict):
    """One parsed history.jsonl line."""

    display: str
    project: str
    session_id: str
    ts: float


class Group(TypedDict):
    """All sessions homed in one directory, newest first."""

    last_ts: float
    path: str
    sessions: list[Session]


@dataclasses.dataclass(frozen=True, kw_only=True)
class Launch:
    """A picked session: where to chdir and what to exec."""

    path: str
    session_id: str
    worktree_name: str | None = None

    @property
    def argv(self, /) -> list[str]:
        """Build the claude argv that resumes this session.

        Returns:
            `claude --resume <id>`, preceded by `--worktree <name>` when the
            session's deleted worktree must be resurrected first.

        """
        if self.worktree_name is None:
            return ["claude", "--resume", self.session_id]
        return [
            "claude",
            "--worktree",
            self.worktree_name,
            "--resume",
            self.session_id,
        ]


@dataclasses.dataclass(frozen=True, kw_only=True)
class RowFilter:
    """Criteria controlling which rows flatten_rows emits."""

    busy: set[str]
    expanded: set[str]
    filter_text: str
    home: str
    isdir: Callable[[str], bool] = os.path.isdir
    min_ts: float | None = None
    running_ids: set[str]
    show_missing: bool = True


class Session(TypedDict):
    """One session's newest timestamp, prompt, and title ("" when untitled)."""

    display: str
    session_id: str
    title: str
    ts: float


class SessionRow(BaseRow):
    """A session row produced by flatten_rows."""

    running: bool
    session: Session


def _clean_text(text: str, /) -> str:
    """Flatten whitespace and strip control characters from user-visible text.

    Returns:
        The text with whitespace runs collapsed to single spaces and the
        remaining unprintable characters (e.g. escape sequences) removed.

    """
    return "".join(char for char in " ".join(text.split()) if char.isprintable())


def _is_claude_pid(
    *, pid: int | str, proc_root: str | None, run: Callable[[list[str]], str]
) -> bool:
    """Check whether pid is a live `claude` process.

    Reads proc_root/<pid>/comm when proc_root is set (Linux); otherwise asks
    `ps` for the process's exec path (macOS and other POSIX without /proc),
    which also reports liveness (empty output once the pid is gone).

    Returns:
        Whether the pid belongs to a live process running claude.

    """
    if proc_root is None:
        return _looks_like_claude(run(["ps", "-o", "comm=", "-p", str(pid)]))
    comm = ""
    with contextlib.suppress(OSError):
        comm = (Path(proc_root) / str(pid) / "comm").read_text(encoding="utf-8")
    return _looks_like_claude(comm)


def _last_title(data: bytes, /, *, field: str, marker: bytes) -> str:
    """Extract the newest valid title of one kind from raw transcript bytes.

    Scans backwards for lines starting with marker (a line-start anchor, so a
    title record merely *quoted* inside message content never matches — JSON
    escaping mangles it mid-line anyway), parsing each candidate until one
    yields a non-empty string in field.

    Returns:
        The flattened title text, or "" when no candidate line parses.

    """
    pos = len(data)
    while (pos := data.rfind(marker, 0, pos)) != -1:
        if pos > 0 and data[pos - 1 : pos] != b"\n":
            continue
        newline = data.find(b"\n", pos)
        line = data[pos : newline if newline != -1 else len(data)]
        record = None
        with contextlib.suppress(ValueError):
            record = json.loads(line)
        if isinstance(record, dict):
            title = record.get(field)
            if isinstance(title, str) and (cleaned := _clean_text(title)):
                return cleaned
    return ""


def _live_record(
    *, proc_root: str | None, record_path: Path, run: Callable[[list[str]], str]
) -> tuple[str, str | None] | None:
    """Validate one ~/.claude/sessions record against the running process.

    The record carries the cwd, so only liveness needs checking — via /proc
    (Linux) or `ps` (elsewhere), per proc_root.

    Returns:
        (realpath cwd, session id or None) when the record describes a live
        claude process, else None.

    """
    record = None
    if record_path.suffix == ".json":
        with contextlib.suppress(OSError, ValueError):
            record = json.loads(record_path.read_text(encoding="utf-8"))
    if not isinstance(record, dict):
        return None
    pid = record.get("pid")
    cwd = record.get("cwd")
    session_id = record.get("sessionId")
    if not (isinstance(pid, int) and isinstance(cwd, str)):
        return None
    if not _is_claude_pid(pid=pid, proc_root=proc_root, run=run):
        return None
    return (os.path.realpath(cwd), session_id if isinstance(session_id, str) else None)


def _looks_like_claude(name: str, /) -> bool:
    """Return whether a process comm or exec path denotes a running claude.

    Linux /proc/<pid>/comm is the literal "claude"; macOS `ps -o comm=` is the
    versioned binary path (.../claude/versions/<version>). Match both without
    matching reclaude itself (whose name merely contains "claude").

    Returns:
        Whether the name denotes a claude process.

    """
    name = name.strip()
    return name == "claude" or name.endswith("/claude") or "/claude/versions/" in name


def _process_cwd(*, pid: str, run: Callable[[list[str]], str]) -> str:
    """Return a process's working directory via lsof (used where /proc is absent).

    Returns:
        The realpath cwd, or "" when lsof is unavailable or reports nothing.

    """
    for line in run(["lsof", "-a", "-d", "cwd", "-Fn", "-p", str(pid)]).splitlines():
        if line.startswith("n"):
            return os.path.realpath(line[1:])
    return ""


def _run_command(command: list[str], /) -> str:
    """Run command and return its stdout, swallowing every failure.

    The executable is resolved through PATH first so the argv stays a fixed
    list of literals (plus an int-derived pid) — never a shell string.

    Returns:
        Captured stdout, or "" if the command is missing, errors, or times out.

    """
    executable = shutil.which(command[0])
    if executable is None:
        return ""
    try:
        completed = subprocess.run(  # noqa: S603
            [executable, *command[1:]],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout


def _scan_busy_dirs(*, run: Callable[[list[str]], str]) -> set[str]:
    """Find busy dirs by scanning `ps` for claude processes (the /proc-less path).

    Returns:
        Realpath cwds of running claude processes, discovered via ps + lsof.

    """
    busy: set[str] = set()
    for line in run(["ps", "-axo", "pid=,comm="]).splitlines():
        try:
            pid, comm = line.split(maxsplit=1)
        except ValueError:
            continue
        if not (pid.isdigit() and _looks_like_claude(comm)):
            continue
        cwd = _process_cwd(pid=pid, run=run)
        if cwd:
            busy.add(cwd)
    return busy


def abbreviate_path(path: str, /, *, home: str) -> str:
    """Return path with the home directory abbreviated to ~.

    Returns:
        The path with a leading home directory shown as ~.

    """
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~" + path[len(home) :]
    return path


def clamp_scroll(*, body: int, scroll_top: int, selection: int) -> int:
    """Adjust the scroll offset so the selection stays on screen.

    Returns:
        The scroll offset, moved the minimum distance needed to keep the
        selection within the body rows.

    """
    if selection < scroll_top:
        return selection
    if body > 0 and selection >= scroll_top + body:
        return selection - body + 1
    return scroll_top


def classify_dir(
    path: str, /, *, isdir: Callable[[str], bool] = os.path.isdir
) -> Classification:
    """Classify a session home dir.

    Returns:
        kind "live" (dir exists); kind "orphan-worktree" with repo/name set
        (dir gone but it was <repo>/.claude/worktrees/<name> and <repo>
        exists — resumable via `claude --worktree <name> --resume <id>` from
        <repo>); or kind "gone".

    """
    if isdir(path):
        return Classification(kind="live")
    match = WORKTREE_RE.match(path)
    if match and isdir(match.group("repo")):
        return Classification(
            kind="orphan-worktree", name=match.group("name"), repo=match.group("repo")
        )
    return Classification(kind="gone")


def find_busy_dirs(
    *,
    proc_root: str | None = PROC_ROOT,
    run: Callable[[list[str]], str] = _run_command,
) -> set[str]:
    """Find dirs with a running `claude`, scanning /proc or falling back to ps.

    With proc_root set (Linux) the scan reads /proc directly; with proc_root
    None (macOS and other POSIX without /proc) it delegates to ps + lsof.

    Returns:
        The set of realpath cwds of running `claude` processes.

    """
    if proc_root is None:
        return _scan_busy_dirs(run=run)
    busy: set[str] = set()
    try:
        proc_entries = list(Path(proc_root).iterdir())
    except OSError:
        return busy
    for proc_entry in proc_entries:
        if not proc_entry.name.isdigit():
            continue
        if not _is_claude_pid(pid=proc_entry.name, proc_root=proc_root, run=run):
            continue
        with contextlib.suppress(OSError):  # process exited, or not ours to read
            busy.add(os.path.realpath(proc_entry / "cwd"))
    return busy


def flatten_rows(
    *, criteria: RowFilter, groups: list[Group]
) -> list[DirRow | SessionRow]:
    """Flatten groups + expansion state into the visible row list.

    A session is visible iff it passes the age window (criteria.min_ts) and
    the text filter — a dir-path match admits all its sessions, otherwise the
    prompt text must contain the filter (both case-insensitive). A dir is
    shown iff it has visible sessions and passes the missing-dir filter; dirs
    are capped at MAX_DIRS. Expanded dirs render only their visible sessions.

    Returns:
        The visible rows: each kept dir, immediately followed by its visible
        session rows when expanded.

    """
    filter_lower = criteria.filter_text.lower()
    kept: list[DirRow] = []
    for group in groups:
        abbreviated = abbreviate_path(group["path"], home=criteria.home)
        path_match = filter_lower in abbreviated.lower()
        visible = [
            session
            for session in group["sessions"]
            if (criteria.min_ts is None or session["ts"] >= criteria.min_ts)
            and (
                path_match
                or filter_lower in session["display"].lower()
                or filter_lower in session["title"].lower()
            )
        ]
        if not visible:
            continue
        classification = classify_dir(group["path"], isdir=criteria.isdir)
        if not criteria.show_missing and classification.kind != "live":
            continue
        kept.append(
            DirRow(
                busy=os.path.realpath(group["path"]) in criteria.busy,
                cls=classification,
                group=group,
                kind="dir",
                vis_sessions=visible,
            )
        )
    rows: list[DirRow | SessionRow] = []
    for dir_row in kept[:MAX_DIRS]:
        rows.append(dir_row)
        if dir_row["group"]["path"] in criteria.expanded:
            rows.extend(
                SessionRow(
                    busy=dir_row["busy"],
                    cls=dir_row["cls"],
                    group=dir_row["group"],
                    kind="session",
                    running=session["session_id"] in criteria.running_ids,
                    session=session,
                )
                for session in dir_row["vis_sessions"]
            )
    return rows


def group_by_home(
    *,
    entries: list[Entry],
    session_title: Callable[..., str],
    transcript_exists: Callable[..., bool],
) -> list[Group]:
    """Group sessions under their home dir (first project seen), newest first.

    A session's transcript lives where the session started, so that first
    directory is the only place `claude --resume` can find it. Sessions whose
    transcript no longer exists are dropped; the rest are titled via
    session_title ("" when untitled).

    Returns:
        Groups sorted newest-first, each with its sessions newest-first.

    """
    sessions = {}
    for entry in entries:
        session = sessions.setdefault(
            entry["session_id"],
            {"display": "", "home": entry["project"], "ts": 0},
        )
        if entry["ts"] >= session["ts"]:
            session["display"] = entry["display"]
            session["ts"] = entry["ts"]
    dirs = {}
    for session_id, session in sessions.items():
        if not transcript_exists(home_dir=session["home"], session_id=session_id):
            continue
        dirs.setdefault(session["home"], []).append(
            Session(
                display=session["display"],
                session_id=session_id,
                title=session_title(home_dir=session["home"], session_id=session_id),
                ts=session["ts"],
            )
        )
    groups = []
    for path, dir_sessions in dirs.items():
        dir_sessions.sort(key=lambda session: -session["ts"])
        groups.append(
            Group(last_ts=dir_sessions[0]["ts"], path=path, sessions=dir_sessions)
        )
    groups.sort(key=lambda group: -group["last_ts"])
    return groups


def live_sessions(
    *,
    proc_root: str | None = PROC_ROOT,
    run: Callable[[list[str]], str] = _run_command,
    sessions_dir: str | None = None,
) -> tuple[set[str], set[str]]:
    """Busy dirs and running session ids from ~/.claude/sessions records.

    Each <pid>.json record counts only if its pid is a live claude — validated
    against /proc/<pid>/comm on Linux, or `ps` where /proc is absent (stale
    files survive crashes). Falls back to scanning every process when the
    records yield nothing, so a claude started outside the session tracker
    still locks its directory (running ids unknown in that case).

    Returns:
        (busy, running): realpath cwds of live claude processes, and their
        session ids when known.

    """
    busy: set[str] = set()
    running: set[str] = set()
    records_dir = Path(sessions_dir) if sessions_dir else SESSIONS_DIR
    try:
        record_paths = list(records_dir.iterdir())
    except OSError:
        record_paths = []
    for record_path in record_paths:
        live = _live_record(proc_root=proc_root, record_path=record_path, run=run)
        if live is None:
            continue
        cwd, session_id = live
        busy.add(cwd)
        if session_id is not None:
            running.add(session_id)
    if not busy:
        busy = find_busy_dirs(proc_root=proc_root, run=run)
    return busy, running


def mung_path(path: str, /) -> str:
    """Return the munged ~/.claude/projects dir name: '/', '.', '_', ' ' become '-'.

    Returns:
        The munged directory name: '/', '.', '_', and ' ' become '-'.

    """
    return MUNG_RE.sub("-", path)


def parse_history(lines: Iterable[str], /) -> list[Entry]:
    """Parse history.jsonl lines into entries, skipping malformed lines.

    Returns:
        Entries in input order; display text is whitespace-flattened with
        control characters stripped.

    """
    entries = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        project = record.get("project")
        session_id = record.get("sessionId")
        timestamp = record.get("timestamp")
        display = record.get("display")
        if not (
            isinstance(project, str)
            and isinstance(session_id, str)
            and isinstance(timestamp, (int, float))
            and not isinstance(timestamp, bool)
        ):
            continue
        # Flatten whitespace and drop control characters (e.g. \x1b) so
        # prompts can't smuggle escape sequences.
        display = _clean_text(display) if isinstance(display, str) else ""
        entries.append(
            Entry(
                display=display,
                project=project,
                session_id=session_id,
                ts=timestamp,
            )
        )
    return entries


def relative_time(*, now_ms: float, ts_ms: float) -> str:
    """Return a compact age like '5s', '3m', '7h', '2d'.

    Returns:
        A compact age like '5s', '3m', '7h', '2d'.

    """
    seconds = max(0, int((now_ms - ts_ms) / 1000))
    if seconds < SECONDS_PER_MINUTE:
        return f"{seconds}s"
    if seconds < SECONDS_PER_HOUR:
        return f"{seconds // SECONDS_PER_MINUTE}m"
    if seconds < SECONDS_PER_DAY:
        return f"{seconds // SECONDS_PER_HOUR}h"
    return f"{seconds // SECONDS_PER_DAY}d"


def row_spans(
    row: DirRow | SessionRow, /, *, home: str, now_ms: float
) -> list[tuple[str, str]]:
    """Render a flatten_rows row as (text, colorkey) spans.

    Color keys: "gone", "orphan", "path", "running", "text", "time" — mapped
    to curses attributes by tui.init_colors(); tui.COLOR_KEYS must cover
    every key emitted here.

    Returns:
        The row as a list of (text, colorkey) spans.

    """
    if row["kind"] == "dir":
        group = row["group"]
        visible = row["vis_sessions"]
        newest_ts = visible[0]["ts"] if visible else group["last_ts"]
        spans = [
            (f"{relative_time(now_ms=now_ms, ts_ms=newest_ts):>4}  ", "time"),
            (abbreviate_path(group["path"], home=home), "path"),
        ]
        if row["busy"]:
            spans.append((" [running]", "running"))
        if row["cls"].kind == "orphan-worktree":
            spans.append((" [worktree gone]", "orphan"))
        elif row["cls"].kind == "gone":
            spans.append((" [gone]", "gone"))
        # The session title (when the transcript has one) labels the row
        # better than the last prompt; fall back to the prompt otherwise.
        last_display = (visible[0]["title"] or visible[0]["display"]) if visible else ""
        if last_display:
            spans.append((f"  —  {last_display}", "text"))
        return spans
    session = row["session"]
    spans = [
        ("    ", "text"),
        (f"{relative_time(now_ms=now_ms, ts_ms=session['ts']):>4}  ", "time"),
        (session["title"] or session["display"] or "(no prompt)", "text"),
    ]
    if row["running"]:
        spans.append((" [running]", "running"))
    return spans


def session_title(
    *, home_dir: str, projects_dir: str | None = None, session_id: str
) -> str:
    """Return the session's title from its transcript, or "" when untitled.

    Claude Code appends {"type":"custom-title","customTitle":...} records
    (/rename) and {"type":"ai-title","aiTitle":...} records (auto-generated)
    to the transcript; the newest custom title wins over the newest AI title.
    The scan is a raw byte search (no per-line JSON parsing), so multi-MB
    transcripts stay cheap.

    Returns:
        The flattened title text, or "" when the transcript has none.

    """
    path = transcript_path(
        home_dir=home_dir, projects_dir=projects_dir, session_id=session_id
    )
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    for field, marker in _TITLE_MARKERS:
        title = _last_title(data, field=field, marker=marker)
        if title:
            return title
    return ""


def transcript_exists(
    *, home_dir: str, projects_dir: str | None = None, session_id: str
) -> bool:
    """Return whether the session's transcript exists under its munged dir.

    Returns:
        Whether the transcript file exists.

    """
    return transcript_path(
        home_dir=home_dir, projects_dir=projects_dir, session_id=session_id
    ).is_file()


def transcript_path(
    *, home_dir: str, projects_dir: str | None = None, session_id: str
) -> Path:
    """Return where the session's transcript lives for a given home dir.

    Returns:
        The transcript path under the (munged) projects directory.

    """
    base = Path(projects_dir) if projects_dir else PROJECTS_DIR
    return base / mung_path(home_dir) / f"{session_id}.jsonl"


def truncate(text: str, /, *, width: int) -> str:
    """Return text clipped to width columns, with an ellipsis when clipped.

    Returns:
        The text, clipped to width columns with a trailing ellipsis.

    """
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


def version() -> str:
    """Return the installed reclaude version.

    Returns:
        The distribution version (sourced from pyproject.toml at build time),
        or a sentinel when running from a source tree with no installed dist.

    """
    try:
        return metadata.version("reclaude")
    except metadata.PackageNotFoundError:
        return "0+unknown"
