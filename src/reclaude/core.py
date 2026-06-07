"""Pure, curses-free logic: history parsing, grouping, classification, rows."""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

MAX_DIRS = 30
MS_PER_DAY = 86_400_000
MS_PER_HOUR = 3_600_000
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
    """One session's newest timestamp and prompt."""

    display: str
    session_id: str
    ts: float


class SessionRow(BaseRow):
    """A session row produced by flatten_rows."""

    running: bool
    session: Session


def _is_claude_pid(*, pid: int | str, proc_root: str) -> bool:
    """Check proc_root/<pid>/comm for a running `claude` process.

    Returns:
        Whether the pid belongs to a live process named "claude".

    """
    comm = ""
    with contextlib.suppress(OSError):
        comm = (Path(proc_root) / str(pid) / "comm").read_text(encoding="utf-8")
    return comm.strip() == "claude"


def _live_record(*, proc_root: str, record_path: Path) -> tuple[str, str | None] | None:
    """Validate one ~/.claude/sessions record against /proc.

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
    if not _is_claude_pid(pid=pid, proc_root=proc_root):
        return None
    return (os.path.realpath(cwd), session_id if isinstance(session_id, str) else None)


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


def find_busy_dirs(*, proc_root: str = "/proc") -> set[str]:
    """Scan proc_root for running `claude` processes.

    Returns:
        The set of realpath cwds of running `claude` processes.

    """
    busy: set[str] = set()
    try:
        proc_entries = list(Path(proc_root).iterdir())
    except OSError:
        return busy
    for proc_entry in proc_entries:
        if not proc_entry.name.isdigit():
            continue
        if not _is_claude_pid(pid=proc_entry.name, proc_root=proc_root):
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
            and (path_match or filter_lower in session["display"].lower())
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
    *, entries: list[Entry], transcript_exists: Callable[..., bool]
) -> list[Group]:
    """Group sessions under their home dir (first project seen), newest first.

    A session's transcript lives where the session started, so that first
    directory is the only place `claude --resume` can find it. Sessions whose
    transcript no longer exists are dropped.

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
    *, proc_root: str = "/proc", sessions_dir: str | None = None
) -> tuple[set[str], set[str]]:
    """Busy dirs and running session ids from ~/.claude/sessions records.

    Each <pid>.json record counts only if /proc/<pid>/comm is "claude" (stale
    files survive crashes). Falls back to scanning /proc when the records
    yield nothing, so a claude started outside the session tracker still
    locks its directory (running ids unknown in that case).

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
        live = _live_record(proc_root=proc_root, record_path=record_path)
        if live is None:
            continue
        cwd, session_id = live
        busy.add(cwd)
        if session_id is not None:
            running.add(session_id)
    if not busy:
        busy = find_busy_dirs(proc_root=proc_root)
    return busy, running


def mung_path(path: str, /) -> str:
    """Return the munged ~/.claude/projects dir name: '/' and '.' become '-'.

    Returns:
        The munged directory name: '/' and '.' become '-'.

    """
    return path.replace("/", "-").replace(".", "-")


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
        if isinstance(display, str):
            # Flatten whitespace, then drop remaining control characters
            # (e.g. \x1b) so prompts can't smuggle escape sequences.
            display = "".join(
                char for char in " ".join(display.split()) if char.isprintable()
            )
        else:
            display = ""
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
        last_display = visible[0]["display"] if visible else ""
        if last_display:
            spans.append((f"  —  {last_display}", "text"))
        return spans
    session = row["session"]
    spans = [
        ("    ", "text"),
        (f"{relative_time(now_ms=now_ms, ts_ms=session['ts']):>4}  ", "time"),
        (session["display"] or "(no prompt)", "text"),
    ]
    if row["running"]:
        spans.append((" [running]", "running"))
    return spans


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
