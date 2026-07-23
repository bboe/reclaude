import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from reclaude import core


def _criteria(**overrides: object) -> core.RowFilter:
    defaults: dict[str, object] = {
        "busy": set(),
        "expanded": set(),
        "filter_text": "",
        "home": "/h",
        "running_ids": set(),
    }
    return core.RowFilter(**(defaults | overrides))


def _entry(
    *, display: str = "", project: str, session_id: str, ts: float
) -> core.Entry:
    return core.Entry(display=display, project=project, session_id=session_id, ts=ts)


def _fake_proc(*, comm: str, cwd_target: Path, pid: int, tmp_path: Path) -> None:
    proc_entry = tmp_path / str(pid)
    proc_entry.mkdir()
    (proc_entry / "comm").write_text(comm + "\n")
    (proc_entry / "cwd").symlink_to(cwd_target)


def _fake_run(*, processes: dict[str, dict[str, str]]) -> Callable[[list[str]], str]:
    # Stand in for the real ps/lsof runner, backed by a {pid: {comm, cwd}} table.
    def run(command: list[str], /) -> str:
        if command[:2] == ["ps", "-axo"]:
            return "".join(
                f"  {pid} {info['comm']}\n" for pid, info in processes.items()
            )
        if command[0] == "ps":  # ps -o comm= -p <pid>: empty once the pid is gone
            return processes.get(command[-1], {}).get("comm", "")
        cwd = processes.get(command[-1], {}).get("cwd", "")  # lsof -Fn -p <pid>
        return f"p{command[-1]}\nfcwd\nn{cwd}\n" if cwd else ""

    return run


def _fake_session_file(*, cwd: Path, dirpath: Path, pid: int, session_id: str) -> None:
    (dirpath / f"{pid}.json").write_text(
        json.dumps({"cwd": str(cwd), "pid": pid, "sessionId": session_id})
    )


def _group(*, path: str, sessions: list[core.Session]) -> core.Group:
    return core.Group(last_ts=sessions[0]["ts"], path=path, sessions=sessions)


def _session(
    *, display: str = "", session_id: str, title: str = "", ts: float
) -> core.Session:
    return core.Session(display=display, session_id=session_id, title=title, ts=ts)


def _transcript(*, lines: list[str], tmp_path: Path) -> None:
    munged_dir = tmp_path / "-home-u-x"
    munged_dir.mkdir(exist_ok=True)
    (munged_dir / "s1.jsonl").write_text("\n".join(lines) + "\n")


def test_abbreviate_path() -> None:
    assert core.abbreviate_path("/home/u/proj", home="/home/u") == "~/proj"
    assert core.abbreviate_path("/home/u", home="/home/u") == "~"
    assert core.abbreviate_path("/home/uother/x", home="/home/u") == "/home/uother/x"
    assert core.abbreviate_path("/etc/x", home="/home/u") == "/etc/x"


def test_clamp_scroll() -> None:
    assert core.clamp_scroll(body=5, scroll_top=0, selection=2) == 0  # visible: keep
    assert core.clamp_scroll(body=5, scroll_top=4, selection=2) == 2  # above: snap up
    assert core.clamp_scroll(body=5, scroll_top=0, selection=7) == 3  # below: snap down
    assert core.clamp_scroll(body=0, scroll_top=3, selection=9) == 3  # no body: keep


def test_classify_dir() -> None:
    assert core.classify_dir("/x", isdir=lambda _path: True) == core.Classification(
        kind="live"
    )
    assert core.classify_dir(
        "/r/.claude/worktrees/a1", isdir=lambda path: path == "/r"
    ) == core.Classification(kind="orphan-worktree", name="a1", repo="/r")
    assert core.classify_dir(
        "/gone/dir", isdir=lambda _path: False
    ) == core.Classification(kind="gone")


def test_classify_dir_worktree_repo_also_gone() -> None:
    assert core.classify_dir(
        "/r/.claude/worktrees/a1", isdir=lambda _path: False
    ) == core.Classification(kind="gone")


def test_find_busy_dirs(*, tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    _fake_proc(comm="claude", cwd_target=work, pid=100, tmp_path=tmp_path)
    _fake_proc(comm="vim", cwd_target=other, pid=200, tmp_path=tmp_path)  # not claude
    (tmp_path / "self").mkdir()  # non-numeric entry: ignored
    broken = tmp_path / "300"
    broken.mkdir()  # numeric, no comm: ignored
    busy = core.find_busy_dirs(proc_root=str(tmp_path))
    assert busy == {os.path.realpath(str(work))}


def test_find_busy_dirs_missing_proc_root() -> None:
    assert core.find_busy_dirs(proc_root="/nonexistent-proc") == set()


def test_find_busy_dirs_ps_backend(*, tmp_path: Path) -> None:
    # proc_root=None routes the scan through ps + lsof (macOS / no /proc).
    live = tmp_path / "live"
    live.mkdir()
    run = _fake_run(
        processes={
            "100": {"comm": "/opt/claude/versions/2.1.0", "cwd": str(live)},
            "200": {"comm": "vim", "cwd": str(tmp_path)},  # not claude
            "300": {"comm": "/u/.local/bin/reclaude", "cwd": str(tmp_path)},  # not it
        }
    )
    assert core.find_busy_dirs(proc_root=None, run=run) == {os.path.realpath(str(live))}


def test_flatten_rows_age_filters_sessions_and_dir() -> None:
    group = _group(
        path="/p/a",
        sessions=[
            _session(display="recent", session_id="new", ts=5000),
            _session(display="middle", session_id="mid", ts=3000),
            _session(display="ancient", session_id="old", ts=1000),
        ],
    )
    rows = core.flatten_rows(
        criteria=_criteria(expanded={"/p/a"}, isdir=lambda _path: True, min_ts=3000),
        groups=[group],
    )
    assert [row.get("session", {}).get("session_id") for row in rows] == [
        None,
        "new",
        "mid",
    ]  # "old" hidden; boundary kept
    assert rows[0]["vis_sessions"][0]["session_id"] == "new"
    # No survivor: every session aged out, so the dir row is hidden too.
    assert (
        core.flatten_rows(
            criteria=_criteria(isdir=lambda _path: True, min_ts=6000), groups=[group]
        )
        == []
    )


def test_flatten_rows_busy_and_classification() -> None:
    group = _group(
        path="/r/.claude/worktrees/a1", sessions=[_session(session_id="s1", ts=100)]
    )
    rows = core.flatten_rows(
        criteria=_criteria(
            busy={"/r/.claude/worktrees/a1"},
            expanded={"/r/.claude/worktrees/a1"},
            isdir=lambda path: path == "/r",
        ),
        groups=[group],
    )
    assert rows[0]["cls"] == core.Classification(
        kind="orphan-worktree", name="a1", repo="/r"
    )
    assert rows[0]["busy"] is True
    assert rows[1]["busy"] is True


def test_flatten_rows_dir_top_reflects_filter() -> None:
    group = _group(
        path="/p/a",
        sessions=[
            _session(display="alpha", session_id="s1", ts=2000),
            _session(display="beta", session_id="s2", ts=1000),
        ],
    )
    rows = core.flatten_rows(
        criteria=_criteria(filter_text="beta", isdir=lambda _path: True), groups=[group]
    )
    assert rows[0]["vis_sessions"][0]["session_id"] == "s2"


def test_flatten_rows_expansion_filter_running() -> None:
    group_a = _group(
        path="/p/a",
        sessions=[
            _session(display="x", session_id="s1", ts=2000),
            _session(display="y", session_id="s2", ts=1000),
        ],
    )
    group_b = _group(
        path="/p/b", sessions=[_session(display="z", session_id="s3", ts=500)]
    )
    rows = core.flatten_rows(
        criteria=_criteria(
            expanded={"/p/a"}, isdir=lambda _path: True, running_ids={"s2"}
        ),
        groups=[group_a, group_b],
    )
    assert [
        (row["kind"], row.get("session", {}).get("session_id")) for row in rows
    ] == [("dir", None), ("session", "s1"), ("session", "s2"), ("dir", None)]
    assert rows[2]["running"] is True
    assert rows[1]["running"] is False
    rows = core.flatten_rows(
        criteria=_criteria(
            expanded={"/p/a"}, filter_text="B", isdir=lambda _path: True
        ),
        groups=[group_a, group_b],
    )
    assert len(rows) == 1
    assert rows[0]["group"] is group_b


def test_flatten_rows_filters_apply_before_cap() -> None:
    gone_groups = [
        _group(
            path=f"/gone/{index}",
            sessions=[_session(session_id=f"g{index}", ts=10_000 - index)],
        )
        for index in range(core.MAX_DIRS)
    ]
    live_old = _group(path="/p/old", sessions=[_session(session_id="old", ts=1)])
    rows = core.flatten_rows(
        criteria=_criteria(isdir=lambda path: path == "/p/old", show_missing=False),
        groups=[*gone_groups, live_old],
    )
    assert len(rows) == 1
    assert rows[0]["group"] is live_old


def test_flatten_rows_prompt_text_match() -> None:
    group_a = _group(
        path="/p/a",
        sessions=[
            _session(display="fix the parser", session_id="s1", ts=2000),
            _session(display="other", session_id="s2", ts=1000),
        ],
    )
    group_b = _group(
        path="/p/b", sessions=[_session(display="unrelated", session_id="s3", ts=500)]
    )
    rows = core.flatten_rows(
        criteria=_criteria(
            expanded={"/p/a", "/p/b"},
            filter_text="PARSER",
            isdir=lambda _path: True,
        ),
        groups=[group_a, group_b],
    )
    # dir surfaces on prompt match though its path doesn't match the filter;
    # only the matching session renders; group_b is fully hidden
    assert [
        (row["kind"], row.get("session", {}).get("session_id")) for row in rows
    ] == [("dir", None), ("session", "s1")]
    assert rows[0]["vis_sessions"] == [group_a["sessions"][0]]


def test_flatten_rows_show_missing_and_min_ts() -> None:
    live = _group(path="/p/live", sessions=[_session(session_id="s1", ts=5000)])
    orphan = _group(
        path="/r/.claude/worktrees/w1", sessions=[_session(session_id="s2", ts=4000)]
    )
    gone = _group(path="/gone/x", sessions=[_session(session_id="s3", ts=3000)])

    def isdir(path: str, /) -> bool:
        return path in {"/p/live", "/r"}

    rows = core.flatten_rows(
        criteria=_criteria(isdir=isdir, show_missing=False), groups=[live, orphan, gone]
    )
    assert [row["group"] for row in rows] == [live]
    rows = core.flatten_rows(
        criteria=_criteria(isdir=isdir, min_ts=4000), groups=[live, orphan, gone]
    )
    assert [row["group"] for row in rows] == [live, orphan]  # boundary ts kept


def test_flatten_rows_title_text_match() -> None:
    group = _group(
        path="/p/a",
        sessions=[
            _session(
                display="prompt", session_id="s1", title="Fix the parser", ts=2000
            ),
            _session(display="other", session_id="s2", ts=1000),
        ],
    )
    rows = core.flatten_rows(
        criteria=_criteria(
            expanded={"/p/a"}, filter_text="PARSER", isdir=lambda _path: True
        ),
        groups=[group],
    )
    # the filter matches the session title even though the prompt doesn't
    assert [
        (row["kind"], row.get("session", {}).get("session_id")) for row in rows
    ] == [("dir", None), ("session", "s1")]


def test_group_by_home_attribution_and_order() -> None:
    entries = [
        _entry(display="first", project="/p/a", session_id="s1", ts=1000),
        # session moved dirs: home stays /p/a
        _entry(display="moved", project="/p/b", session_id="s1", ts=5000),
        _entry(display="second", project="/p/a", session_id="s2", ts=3000),
        _entry(display="newest", project="/p/c", session_id="s3", ts=9000),
    ]
    groups = core.group_by_home(
        entries=entries,
        session_title=lambda **_kwargs: "",
        transcript_exists=lambda *_args, **_kwargs: True,
    )
    assert [group["path"] for group in groups] == ["/p/c", "/p/a"]
    group_a = groups[1]
    assert group_a["last_ts"] == 5000
    assert [session["session_id"] for session in group_a["sessions"]] == ["s1", "s2"]
    assert group_a["sessions"][0]["display"] == "moved"


def test_group_by_home_drops_empty_groups() -> None:
    entries = [_entry(project="/p/a", session_id="s1", ts=1000)]
    assert (
        core.group_by_home(
            entries=entries,
            session_title=lambda **_kwargs: "",
            transcript_exists=lambda *_args, **_kwargs: False,
        )
        == []
    )


def test_group_by_home_drops_sessions_without_transcript() -> None:
    entries = [
        _entry(project="/p/a", session_id="s1", ts=1000),
        _entry(project="/p/a", session_id="s2", ts=2000),
        _entry(project="/p/b", session_id="s3", ts=3000),
    ]
    groups = core.group_by_home(
        entries=entries,
        session_title=lambda **_kwargs: "",
        transcript_exists=lambda **kwargs: kwargs["session_id"] != "s2",
    )
    assert [group["path"] for group in groups] == ["/p/b", "/p/a"]
    assert [session["session_id"] for session in groups[1]["sessions"]] == ["s1"]


def test_group_by_home_titles() -> None:
    entries = [
        _entry(project="/p/a", session_id="s1", ts=1000),
        _entry(project="/p/a", session_id="s2", ts=2000),
    ]
    groups = core.group_by_home(
        entries=entries,
        session_title=lambda **kwargs: "named" if kwargs["session_id"] == "s2" else "",
        transcript_exists=lambda *_args, **_kwargs: True,
    )
    assert [session["title"] for session in groups[0]["sessions"]] == ["named", ""]


def test_launch_argv() -> None:
    launch = core.Launch(path="/p", session_id="s1")
    assert launch.argv == ["claude", "--resume", "s1"]
    launch = core.Launch(path="/r", session_id="s2", worktree_name="w1")
    assert launch.argv == ["claude", "--worktree", "w1", "--resume", "s2"]


def test_live_sessions(*, tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    proc = tmp_path / "proc"
    proc.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    _fake_session_file(cwd=work, dirpath=sessions_dir, pid=100, session_id="s-live")
    proc_entry = proc / "100"
    proc_entry.mkdir()
    (proc_entry / "comm").write_text("claude\n")
    # pid 200: not running; pid 300: alive but not claude
    _fake_session_file(cwd=work, dirpath=sessions_dir, pid=200, session_id="s-stale")
    _fake_session_file(cwd=work, dirpath=sessions_dir, pid=300, session_id="s-vim")
    proc_entry = proc / "300"
    proc_entry.mkdir()
    (proc_entry / "comm").write_text("vim\n")
    (sessions_dir / "400.json").write_text("not json")  # malformed
    busy, running = core.live_sessions(
        proc_root=str(proc), sessions_dir=str(sessions_dir)
    )
    assert busy == {os.path.realpath(str(work))}
    assert running == {"s-live"}


def test_live_sessions_fallback_to_proc_scan(*, tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    proc.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    proc_entry = proc / "500"
    proc_entry.mkdir()
    (proc_entry / "comm").write_text("claude\n")
    (proc_entry / "cwd").symlink_to(work)
    busy, running = core.live_sessions(
        proc_root=str(proc), sessions_dir=str(tmp_path / "missing")
    )
    assert busy == {os.path.realpath(str(work))}
    assert running == set()


def test_live_sessions_ps_backend(*, tmp_path: Path) -> None:
    # proc_root=None validates each record's pid via ps; cwd stays the record's.
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    _fake_session_file(cwd=work, dirpath=sessions_dir, pid=100, session_id="s-live")
    _fake_session_file(cwd=work, dirpath=sessions_dir, pid=200, session_id="s-stale")
    _fake_session_file(cwd=work, dirpath=sessions_dir, pid=300, session_id="s-vim")
    run = _fake_run(
        processes={
            "100": {"comm": "/opt/claude/versions/2.1.0", "cwd": str(work)},
            "300": {"comm": "vim", "cwd": str(work)},  # alive, but not claude
        }  # pid 200 absent: stale record, process gone
    )
    busy, running = core.live_sessions(
        proc_root=None, run=run, sessions_dir=str(sessions_dir)
    )
    assert busy == {os.path.realpath(str(work))}
    assert running == {"s-live"}


def test_mung_path() -> None:
    assert core.mung_path("/home/u/scratch") == "-home-u-scratch"
    assert (
        core.mung_path("/home/u/repo/.claude/worktrees/a1")
        == "-home-u-repo--claude-worktrees-a1"
    )
    # Underscores mung to '-' too, so dirs like claude_throwaway_session resolve.
    assert (
        core.mung_path("/Users/b/claude_throwaway_session")
        == "-Users-b-claude-throwaway-session"
    )
    # Spaces mung to '-' too, so paths like ~/Library/Application Support resolve.
    assert (
        core.mung_path("/Users/b/Library/Application Support/x")
        == "-Users-b-Library-Application-Support-x"
    )


def test_parse_history_basic() -> None:
    lines = [
        (
            '{"display":"fix bug","pastedContents":{},"timestamp":1000,'
            '"project":"/p/a","sessionId":"s1"}'
        ),
        '{"display":"add feat","timestamp":2000,"project":"/p/b","sessionId":"s2"}',
    ]
    entries = core.parse_history(lines)
    assert entries == [
        {"display": "fix bug", "project": "/p/a", "session_id": "s1", "ts": 1000},
        {"display": "add feat", "project": "/p/b", "session_id": "s2", "ts": 2000},
    ]


def test_parse_history_flattens_control_chars() -> None:
    lines = [
        (
            '{"display":"line one\\nline two\\tend","timestamp":1,'
            '"project":"/p","sessionId":"s"}'
        )
    ]
    assert core.parse_history(lines)[0]["display"] == "line one line two end"
    # Non-whitespace control characters (escape sequences) are stripped too.
    lines = [
        '{"display":"a\\u001b[31mred\\u0007","timestamp":1,"project":"/p","sessionId":"s"}'
    ]
    assert core.parse_history(lines)[0]["display"] == "a[31mred"


def test_parse_history_skips_garbage() -> None:
    lines = [
        "not json at all",
        '{"display":"missing fields"}',
        '"a json string, not an object"',
        '{"display":null,"timestamp":3000,"project":"/p/c","sessionId":"s3"}',
        "",
    ]
    entries = core.parse_history(lines)
    # Only the entry with all required fields survives; null display becomes ""
    assert entries == [
        {"display": "", "project": "/p/c", "session_id": "s3", "ts": 3000}
    ]


def test_relative_time() -> None:
    now = 10_000_000_000_000
    assert core.relative_time(now_ms=now, ts_ms=now - 5_000) == "5s"
    assert core.relative_time(now_ms=now, ts_ms=now - 90_000) == "1m"
    assert core.relative_time(now_ms=now, ts_ms=now - 3 * 3600_000) == "3h"
    assert core.relative_time(now_ms=now, ts_ms=now - 49 * 3600_000) == "2d"
    assert core.relative_time(now_ms=now, ts_ms=now + 5_000) == "0s"  # clock skew


def test_row_spans_badges_and_session() -> None:
    group = _group(
        path="/r/.claude/worktrees/a1", sessions=[_session(session_id="s1", ts=0)]
    )
    row = core.DirRow(
        busy=False,
        cls=core.Classification(kind="orphan-worktree", name="a1", repo="/r"),
        group=group,
        kind="dir",
        vis_sessions=group["sessions"],
    )
    spans = core.row_spans(row, home="/h", now_ms=0)
    assert (" [worktree gone]", "orphan") in spans
    row["cls"] = core.Classification(kind="gone")
    spans = core.row_spans(row, home="/h", now_ms=0)
    assert (" [gone]", "gone") in spans
    session_row = core.SessionRow(
        busy=False,
        cls=core.Classification(kind="live"),
        group=group,
        kind="session",
        running=True,
        session=_session(session_id="s1", ts=0),
    )
    assert core.row_spans(session_row, home="/h", now_ms=0) == [
        ("    ", "text"),
        ("  0s  ", "time"),
        ("(no prompt)", "text"),
        (" [running]", "running"),
    ]


def test_row_spans_dir() -> None:
    group = _group(
        path="/h/proj",
        sessions=[
            _session(display="newest", session_id="s1", ts=60_000),
            _session(display="older", session_id="s0", ts=0),
        ],
    )
    row = core.DirRow(
        busy=True,
        cls=core.Classification(kind="live"),
        group=group,
        kind="dir",
        vis_sessions=group["sessions"][1:],
    )
    # time and prompt come from the newest VISIBLE session (s0), not s1
    assert core.row_spans(row, home="/h", now_ms=120_000) == [
        ("  2m  ", "time"),
        ("~/proj", "path"),
        (" [running]", "running"),
        ("  —  older", "text"),
    ]


def test_row_spans_prefers_title() -> None:
    group = _group(
        path="/h/proj",
        sessions=[_session(display="prompt", session_id="s1", title="Named", ts=0)],
    )
    row = core.DirRow(
        busy=False,
        cls=core.Classification(kind="live"),
        group=group,
        kind="dir",
        vis_sessions=group["sessions"],
    )
    assert ("  —  Named", "text") in core.row_spans(row, home="/h", now_ms=0)
    session_row = core.SessionRow(
        busy=False,
        cls=core.Classification(kind="live"),
        group=group,
        kind="session",
        running=False,
        session=group["sessions"][0],
    )
    assert ("Named", "text") in core.row_spans(session_row, home="/h", now_ms=0)


def test_session_title(*, tmp_path: Path) -> None:
    _transcript(
        lines=[
            '{"type":"ai-title","aiTitle":"First title","sessionId":"s1"}',
            '{"type":"user","message":"mentions {\\"type\\":\\"ai-title\\" inline"}',
            '{"type":"ai-title","aiTitle":"Second title","sessionId":"s1"}',
        ],
        tmp_path=tmp_path,
    )
    title = core.session_title(
        home_dir="/home/u/x", projects_dir=str(tmp_path), session_id="s1"
    )
    assert title == "Second title"  # newest ai-title wins; quoted mention ignored


def test_session_title_custom_wins_and_sanitizes(*, tmp_path: Path) -> None:
    _transcript(
        lines=[
            '{"type":"custom-title","customTitle":"my\\nrename\\u001b","sessionId":"s1"}',
            '{"type":"ai-title","aiTitle":"Newer AI title","sessionId":"s1"}',
        ],
        tmp_path=tmp_path,
    )
    title = core.session_title(
        home_dir="/home/u/x", projects_dir=str(tmp_path), session_id="s1"
    )
    # a /rename beats a newer ai-title; whitespace/controls are flattened
    assert title == "my rename"


def test_session_title_missing_and_malformed(*, tmp_path: Path) -> None:
    assert not core.session_title(
        home_dir="/home/u/x", projects_dir=str(tmp_path), session_id="s1"
    )
    _transcript(
        lines=[
            '{"type":"ai-title","aiTitle":"Good title","sessionId":"s1"}',
            '{"type":"ai-title","aiTitle":123,"sessionId":"s1"}',  # non-string
            '{"type":"ai-title","aiTitle":"truncated',  # invalid json
        ],
        tmp_path=tmp_path,
    )
    title = core.session_title(
        home_dir="/home/u/x", projects_dir=str(tmp_path), session_id="s1"
    )
    assert title == "Good title"  # malformed newer records fall through


def test_transcript_exists(*, tmp_path: Path) -> None:
    munged_dir = tmp_path / "-home-u-x"
    munged_dir.mkdir()
    (munged_dir / "s1.jsonl").write_text("{}")
    assert core.transcript_exists(
        home_dir="/home/u/x", projects_dir=str(tmp_path), session_id="s1"
    )
    assert not core.transcript_exists(
        home_dir="/home/u/x", projects_dir=str(tmp_path), session_id="s2"
    )


def test_transcript_path() -> None:
    path = core.transcript_path(
        home_dir="/home/u/scratch", projects_dir="/pp", session_id="abc"
    )
    assert path == Path("/pp/-home-u-scratch/abc.jsonl")


def test_truncate() -> None:
    assert core.truncate("hello", width=10) == "hello"
    assert core.truncate("hello world", width=8) == "hello w…"
    assert not core.truncate("hi", width=0)


def test_version(*, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core.metadata, "version", lambda _name: "1.2.3")
    assert core.version() == "1.2.3"

    def _missing(_name: str) -> str:
        raise core.metadata.PackageNotFoundError

    monkeypatch.setattr(core.metadata, "version", _missing)
    assert core.version() == "0+unknown"
