import json
import os

from reclaude import core


def _entry(*, display="", project, session_id, ts):
    return {"display": display, "project": project, "session_id": session_id,
            "ts": ts}


def _fake_proc(tmp_path, *, comm, cwd_target, pid):
    proc_entry = tmp_path / str(pid)
    proc_entry.mkdir()
    (proc_entry / "comm").write_text(comm + "\n")
    (proc_entry / "cwd").symlink_to(cwd_target)


def _fake_session_file(dirpath, *, cwd, pid, session_id):
    (dirpath / f"{pid}.json").write_text(
        json.dumps({"cwd": str(cwd), "pid": pid, "sessionId": session_id}))


def _group(path, *, sessions):
    return {"last_ts": sessions[0]["ts"], "path": path, "sessions": sessions}


def _session(session_id, *, display="", ts):
    return {"display": display, "session_id": session_id, "ts": ts}


def test_abbreviate_path():
    assert core.abbreviate_path("/home/u/proj", home="/home/u") == "~/proj"
    assert core.abbreviate_path("/home/u", home="/home/u") == "~"
    assert core.abbreviate_path("/home/uother/x", home="/home/u") == \
        "/home/uother/x"
    assert core.abbreviate_path("/etc/x", home="/home/u") == "/etc/x"


def test_classify_dir():
    assert core.classify_dir("/x", isdir=lambda path: True) == \
        ("live", None, None)
    assert core.classify_dir("/r/.claude/worktrees/a1",
                             isdir=lambda path: path == "/r") == \
        ("orphan-worktree", "/r", "a1")
    assert core.classify_dir("/gone/dir", isdir=lambda path: False) == \
        ("gone", None, None)


def test_classify_dir_worktree_repo_also_gone():
    assert core.classify_dir("/r/.claude/worktrees/a1",
                             isdir=lambda path: False) == ("gone", None, None)


def test_find_busy_dirs(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    _fake_proc(tmp_path, comm="claude", cwd_target=work, pid=100)
    _fake_proc(tmp_path, comm="vim", cwd_target=other, pid=200)  # not claude
    (tmp_path / "self").mkdir()                # non-numeric entry: ignored
    broken = tmp_path / "300"
    broken.mkdir()                             # numeric, no comm: ignored
    busy = core.find_busy_dirs(proc_root=str(tmp_path))
    assert busy == {os.path.realpath(str(work))}


def test_find_busy_dirs_missing_proc_root():
    assert core.find_busy_dirs(proc_root="/nonexistent-proc") == set()


def test_flatten_rows_age_filters_sessions_and_dir():
    group = _group("/p/a", sessions=[
        _session("new", display="recent", ts=5000),
        _session("mid", display="middle", ts=3000),
        _session("old", display="ancient", ts=1000)])
    rows = core.flatten_rows([group], busy=set(), expanded={"/p/a"},
                             filter_text="", home="/h",
                             isdir=lambda path: True, min_ts=3000,
                             running_ids=set())
    assert [row.get("session", {}).get("session_id") for row in rows] == [
        None, "new", "mid"]                       # "old" hidden; boundary kept
    assert rows[0]["vis_sessions"][0]["session_id"] == "new"
    assert core.flatten_rows([group], busy=set(), expanded=set(),
                             filter_text="", home="/h",
                             isdir=lambda path: True, min_ts=6000,
                             running_ids=set()) == []  # no survivor
    # (every session aged out, so the dir row is hidden too)


def test_flatten_rows_busy_and_classification():
    group = _group("/r/.claude/worktrees/a1",
                   sessions=[_session("s1", ts=100)])
    rows = core.flatten_rows([group], busy={"/r/.claude/worktrees/a1"},
                             expanded={"/r/.claude/worktrees/a1"},
                             filter_text="", home="/h",
                             isdir=lambda path: path == "/r",
                             running_ids=set())
    assert rows[0]["cls"] == "orphan-worktree"
    assert (rows[0]["repo"], rows[0]["name"]) == ("/r", "a1")
    assert rows[0]["busy"] is True and rows[1]["busy"] is True


def test_flatten_rows_dir_top_reflects_filter():
    group = _group("/p/a", sessions=[
        _session("s1", display="alpha", ts=2000),
        _session("s2", display="beta", ts=1000)])
    rows = core.flatten_rows([group], busy=set(), expanded=set(),
                             filter_text="beta", home="/h",
                             isdir=lambda path: True, running_ids=set())
    assert rows[0]["vis_sessions"][0]["session_id"] == "s2"


def test_flatten_rows_expansion_filter_running():
    group_a = _group("/p/a", sessions=[
        _session("s1", display="x", ts=2000),
        _session("s2", display="y", ts=1000)])
    group_b = _group("/p/b", sessions=[_session("s3", display="z", ts=500)])
    rows = core.flatten_rows([group_a, group_b], busy=set(),
                             expanded={"/p/a"}, filter_text="", home="/h",
                             isdir=lambda path: True, running_ids={"s2"})
    assert [(row["kind"], row.get("session", {}).get("session_id"))
            for row in rows] == [
        ("dir", None), ("session", "s1"), ("session", "s2"), ("dir", None)]
    assert rows[2]["running"] is True and rows[1]["running"] is False
    rows = core.flatten_rows([group_a, group_b], busy=set(),
                             expanded={"/p/a"}, filter_text="B", home="/h",
                             isdir=lambda path: True, running_ids=set())
    assert len(rows) == 1 and rows[0]["group"] is group_b


def test_flatten_rows_filters_apply_before_cap():
    gone_groups = [_group(f"/gone/{i}",
                          sessions=[_session(f"g{i}", ts=10_000 - i)])
                   for i in range(core.MAX_DIRS)]
    live_old = _group("/p/old", sessions=[_session("old", ts=1)])
    rows = core.flatten_rows(gone_groups + [live_old], busy=set(),
                             expanded=set(), filter_text="", home="/h",
                             isdir=lambda path: path == "/p/old",
                             running_ids=set(), show_missing=False)
    assert len(rows) == 1 and rows[0]["group"] is live_old


def test_flatten_rows_prompt_text_match():
    group_a = _group("/p/a", sessions=[
        _session("s1", display="fix the parser", ts=2000),
        _session("s2", display="other", ts=1000)])
    group_b = _group("/p/b", sessions=[
        _session("s3", display="unrelated", ts=500)])
    rows = core.flatten_rows([group_a, group_b], busy=set(),
                             expanded={"/p/a", "/p/b"}, filter_text="PARSER",
                             home="/h", isdir=lambda path: True,
                             running_ids=set())
    # dir surfaces on prompt match though its path doesn't match the filter;
    # only the matching session renders; group_b is fully hidden
    assert [(row["kind"], row.get("session", {}).get("session_id"))
            for row in rows] == [("dir", None), ("session", "s1")]
    assert rows[0]["vis_sessions"] == [group_a["sessions"][0]]


def test_flatten_rows_show_missing_and_min_ts():
    live = _group("/p/live", sessions=[_session("s1", ts=5000)])
    orphan = _group("/r/.claude/worktrees/w1",
                    sessions=[_session("s2", ts=4000)])
    gone = _group("/gone/x", sessions=[_session("s3", ts=3000)])

    def isdir(path):
        return path in ("/p/live", "/r")

    rows = core.flatten_rows([live, orphan, gone], busy=set(), expanded=set(),
                             filter_text="", home="/h", isdir=isdir,
                             running_ids=set(), show_missing=False)
    assert [row["group"] for row in rows] == [live]
    rows = core.flatten_rows([live, orphan, gone], busy=set(), expanded=set(),
                             filter_text="", home="/h", isdir=isdir,
                             min_ts=4000, running_ids=set())
    assert [row["group"] for row in rows] == [live, orphan]  # boundary ts kept


def test_group_by_home_attribution_and_order():
    entries = [
        _entry(display="first", project="/p/a", session_id="s1", ts=1000),
        # session moved dirs: home stays /p/a
        _entry(display="moved", project="/p/b", session_id="s1", ts=5000),
        _entry(display="second", project="/p/a", session_id="s2", ts=3000),
        _entry(display="newest", project="/p/c", session_id="s3", ts=9000),
    ]
    groups = core.group_by_home(
        entries, transcript_exists=lambda home_dir, *, session_id: True)
    assert [group["path"] for group in groups] == ["/p/c", "/p/a"]
    group_a = groups[1]
    assert group_a["last_ts"] == 5000
    assert [session["session_id"]
            for session in group_a["sessions"]] == ["s1", "s2"]
    assert group_a["sessions"][0]["display"] == "moved"


def test_group_by_home_drops_empty_groups():
    entries = [_entry(project="/p/a", session_id="s1", ts=1000)]
    assert core.group_by_home(
        entries, transcript_exists=lambda home_dir, *, session_id: False) == []


def test_group_by_home_drops_sessions_without_transcript():
    entries = [_entry(project="/p/a", session_id="s1", ts=1000),
               _entry(project="/p/a", session_id="s2", ts=2000),
               _entry(project="/p/b", session_id="s3", ts=3000)]
    groups = core.group_by_home(
        entries,
        transcript_exists=lambda home_dir, *, session_id: session_id != "s2")
    assert [group["path"] for group in groups] == ["/p/b", "/p/a"]
    assert [session["session_id"]
            for session in groups[1]["sessions"]] == ["s1"]


def test_live_sessions(tmp_path):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    proc = tmp_path / "proc"
    proc.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    _fake_session_file(sessions_dir, cwd=work, pid=100, session_id="s-live")
    proc_entry = proc / "100"
    proc_entry.mkdir()
    (proc_entry / "comm").write_text("claude\n")
    # pid 200: not running; pid 300: alive but not claude
    _fake_session_file(sessions_dir, cwd=work, pid=200,
                       session_id="s-stale")
    _fake_session_file(sessions_dir, cwd=work, pid=300, session_id="s-vim")
    proc_entry = proc / "300"
    proc_entry.mkdir()
    (proc_entry / "comm").write_text("vim\n")
    (sessions_dir / "400.json").write_text("not json")     # malformed
    busy, running = core.live_sessions(proc_root=str(proc),
                                       sessions_dir=str(sessions_dir))
    assert busy == {os.path.realpath(str(work))}
    assert running == {"s-live"}


def test_live_sessions_fallback_to_proc_scan(tmp_path):
    proc = tmp_path / "proc"
    proc.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    proc_entry = proc / "500"
    proc_entry.mkdir()
    (proc_entry / "comm").write_text("claude\n")
    (proc_entry / "cwd").symlink_to(work)
    busy, running = core.live_sessions(proc_root=str(proc),
                                       sessions_dir=str(tmp_path / "missing"))
    assert busy == {os.path.realpath(str(work))}
    assert running == set()


def test_mung_path():
    assert core.mung_path("/home/u/scratch") == "-home-u-scratch"
    assert core.mung_path("/home/u/repo/.claude/worktrees/a1") == \
        "-home-u-repo--claude-worktrees-a1"


def test_parse_history_basic():
    lines = [
        '{"display":"fix bug","pastedContents":{},"timestamp":1000,"project":"/p/a","sessionId":"s1"}',
        '{"display":"add feat","timestamp":2000,"project":"/p/b","sessionId":"s2"}',
    ]
    entries = core.parse_history(lines)
    assert entries == [
        {"display": "fix bug", "project": "/p/a", "session_id": "s1",
         "ts": 1000},
        {"display": "add feat", "project": "/p/b", "session_id": "s2",
         "ts": 2000},
    ]


def test_parse_history_flattens_control_chars():
    lines = ['{"display":"line one\\nline two\\tend","timestamp":1,"project":"/p","sessionId":"s"}']
    assert core.parse_history(lines)[0]["display"] == "line one line two end"
    # Non-whitespace control characters (escape sequences) are stripped too.
    lines = ['{"display":"a\\u001b[31mred\\u0007","timestamp":1,"project":"/p","sessionId":"s"}']
    assert core.parse_history(lines)[0]["display"] == "a[31mred"


def test_parse_history_skips_garbage():
    lines = [
        "not json at all",
        '{"display":"missing fields"}',
        '"a json string, not an object"',
        '{"display":null,"timestamp":3000,"project":"/p/c","sessionId":"s3"}',
        "",
    ]
    entries = core.parse_history(lines)
    # Only the entry with all required fields survives; null display becomes ""
    assert entries == [{"display": "", "project": "/p/c", "session_id": "s3",
                        "ts": 3000}]


def test_relative_time():
    now = 10_000_000_000_000
    assert core.relative_time(now - 5_000, now_ms=now) == "5s"
    assert core.relative_time(now - 90_000, now_ms=now) == "1m"
    assert core.relative_time(now - 3 * 3600_000, now_ms=now) == "3h"
    assert core.relative_time(now - 49 * 3600_000, now_ms=now) == "2d"
    assert core.relative_time(now + 5_000, now_ms=now) == "0s"  # clock skew


def test_row_spans_badges_and_session():
    group = _group("/r/.claude/worktrees/a1", sessions=[_session("s1", ts=0)])
    row = {"busy": False, "cls": "orphan-worktree", "group": group,
           "kind": "dir", "name": "a1", "repo": "/r",
           "vis_sessions": group["sessions"]}
    assert (" [worktree gone]", "orphan") in core._row_spans(row, home="/h",
                                                             now_ms=0)
    row["cls"] = "gone"
    assert (" [gone]", "gone") in core._row_spans(row, home="/h", now_ms=0)
    session_row = {"busy": False, "cls": "live", "group": group,
                   "kind": "session", "name": None, "repo": None,
                   "running": True, "session": _session("s1", ts=0)}
    assert core._row_spans(session_row, home="/h", now_ms=0) == [
        ("    ", "text"), ("  0s  ", "time"),
        ("(no prompt)", "text"), (" [running]", "running")]


def test_row_spans_dir():
    group = _group("/h/proj", sessions=[
        _session("s1", display="newest", ts=60_000),
        _session("s0", display="older", ts=0)])
    row = {"busy": True, "cls": "live", "group": group, "kind": "dir",
           "name": None, "repo": None, "vis_sessions": group["sessions"][1:]}
    # time and prompt come from the newest VISIBLE session (s0), not s1
    assert core._row_spans(row, home="/h", now_ms=120_000) == [
        ("  2m  ", "time"), ("~/proj", "path"),
        (" [running]", "running"), ("  —  older", "text")]


def test_transcript_exists(tmp_path):
    munged_dir = tmp_path / "-home-u-x"
    munged_dir.mkdir()
    (munged_dir / "s1.jsonl").write_text("{}")
    assert core.transcript_exists("/home/u/x", projects_dir=str(tmp_path),
                                  session_id="s1")
    assert not core.transcript_exists("/home/u/x", projects_dir=str(tmp_path),
                                      session_id="s2")


def test_transcript_path():
    path = core.transcript_path("/home/u/scratch", projects_dir="/pp",
                                session_id="abc")
    assert path == "/pp/-home-u-scratch/abc.jsonl"


def test_truncate():
    assert core.truncate("hello", width=10) == "hello"
    assert core.truncate("hello world", width=8) == "hello w…"
    assert core.truncate("hi", width=0) == ""
