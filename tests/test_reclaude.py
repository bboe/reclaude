import json
import os

from reclaude import core


def _e(*, display="", project, session_id, ts):
    return {"display": display, "project": project, "session_id": session_id,
            "ts": ts}


def _fake_proc(tmp_path, *, comm, cwd_target, pid):
    p = tmp_path / str(pid)
    p.mkdir()
    (p / "comm").write_text(comm + "\n")
    (p / "cwd").symlink_to(cwd_target)


def _fake_session_file(dirpath, *, cwd, pid, session_id):
    (dirpath / f"{pid}.json").write_text(
        json.dumps({"cwd": str(cwd), "pid": pid, "sessionId": session_id}))


def _g(path, *, sessions):
    return {"last_ts": sessions[0]["ts"], "path": path, "sessions": sessions}


def _s(session_id, *, display="", ts):
    return {"display": display, "session_id": session_id, "ts": ts}


def test_abbreviate_path():
    assert core.abbreviate_path("/home/u/proj", home="/home/u") == "~/proj"
    assert core.abbreviate_path("/home/u", home="/home/u") == "~"
    assert core.abbreviate_path("/home/uother/x", home="/home/u") == "/home/uother/x"
    assert core.abbreviate_path("/etc/x", home="/home/u") == "/etc/x"


def test_classify_dir():
    assert core.classify_dir("/x", isdir=lambda p: True) == ("live", None, None)
    assert core.classify_dir("/r/.claude/worktrees/a1", isdir=lambda p: p == "/r") == \
        ("orphan-worktree", "/r", "a1")
    assert core.classify_dir("/gone/dir", isdir=lambda p: False) == ("gone", None, None)


def test_classify_dir_worktree_repo_also_gone():
    assert core.classify_dir("/r/.claude/worktrees/a1", isdir=lambda p: False) == \
        ("gone", None, None)


def test_find_busy_dirs(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    _fake_proc(tmp_path, comm="claude", cwd_target=work, pid=100)
    _fake_proc(tmp_path, comm="vim", cwd_target=other, pid=200)  # wrong comm: ignored
    (tmp_path / "self").mkdir()                     # non-numeric entry: ignored
    broken = tmp_path / "300"
    broken.mkdir()                                  # numeric but no comm file: ignored
    busy = core.find_busy_dirs(proc_root=str(tmp_path))
    assert busy == {os.path.realpath(str(work))}


def test_find_busy_dirs_missing_proc_root():
    assert core.find_busy_dirs(proc_root="/nonexistent-proc") == set()


def test_flatten_rows_age_filters_sessions_and_dir():
    g = _g("/p/a", sessions=[_s("new", display="recent", ts=5000),
                             _s("mid", display="middle", ts=3000),
                             _s("old", display="ancient", ts=1000)])
    rows = core.flatten_rows([g], busy=set(), expanded={"/p/a"}, filt="",
                             home="/h", isdir=lambda p: True, min_ts=3000,
                             running_ids=set())
    assert [r.get("session", {}).get("session_id") for r in rows] == [
        None, "new", "mid"]                       # "old" hidden; boundary kept
    assert rows[0]["vis_sessions"][0]["session_id"] == "new"
    assert core.flatten_rows([g], busy=set(), expanded=set(), filt="", home="/h",
                             isdir=lambda p: True, min_ts=6000,
                             running_ids=set()) == []  # no survivor -> dir hidden


def test_flatten_rows_busy_and_classification():
    g = _g("/r/.claude/worktrees/a1", sessions=[_s("s1", ts=100)])
    rows = core.flatten_rows([g], busy={"/r/.claude/worktrees/a1"},
                             expanded={"/r/.claude/worktrees/a1"}, filt="",
                             home="/h", isdir=lambda p: p == "/r",
                             running_ids=set())
    assert rows[0]["cls"] == "orphan-worktree"
    assert (rows[0]["repo"], rows[0]["name"]) == ("/r", "a1")
    assert rows[0]["busy"] is True and rows[1]["busy"] is True


def test_flatten_rows_dir_top_reflects_filter():
    g = _g("/p/a", sessions=[_s("s1", display="alpha", ts=2000),
                             _s("s2", display="beta", ts=1000)])
    rows = core.flatten_rows([g], busy=set(), expanded=set(), filt="beta",
                             home="/h", isdir=lambda p: True, running_ids=set())
    assert rows[0]["vis_sessions"][0]["session_id"] == "s2"


def test_flatten_rows_expansion_filter_running():
    g1 = _g("/p/a", sessions=[_s("s1", display="x", ts=2000),
                              _s("s2", display="y", ts=1000)])
    g2 = _g("/p/b", sessions=[_s("s3", display="z", ts=500)])
    rows = core.flatten_rows([g1, g2], busy=set(), expanded={"/p/a"}, filt="",
                             home="/h", isdir=lambda p: True,
                             running_ids={"s2"})
    assert [(r["kind"], r.get("session", {}).get("session_id")) for r in rows] == [
        ("dir", None), ("session", "s1"), ("session", "s2"), ("dir", None)]
    assert rows[2]["running"] is True and rows[1]["running"] is False
    rows = core.flatten_rows([g1, g2], busy=set(), expanded={"/p/a"}, filt="B",
                             home="/h", isdir=lambda p: True, running_ids=set())
    assert len(rows) == 1 and rows[0]["group"] is g2


def test_flatten_rows_filters_apply_before_cap():
    gone_groups = [_g(f"/gone/{i}", sessions=[_s(f"g{i}", ts=10_000 - i)])
                   for i in range(core.MAX_DIRS)]
    live_old = _g("/p/old", sessions=[_s("old", ts=1)])
    rows = core.flatten_rows(gone_groups + [live_old], busy=set(),
                             expanded=set(), filt="", home="/h",
                             isdir=lambda p: p == "/p/old", running_ids=set(),
                             show_missing=False)
    assert len(rows) == 1 and rows[0]["group"] is live_old


def test_flatten_rows_prompt_text_match():
    g1 = _g("/p/a", sessions=[_s("s1", display="fix the parser", ts=2000),
                              _s("s2", display="other", ts=1000)])
    g2 = _g("/p/b", sessions=[_s("s3", display="unrelated", ts=500)])
    rows = core.flatten_rows([g1, g2], busy=set(), expanded={"/p/a", "/p/b"},
                             filt="PARSER", home="/h", isdir=lambda p: True,
                             running_ids=set())
    # dir surfaces on prompt match though its path doesn't match the filter;
    # only the matching session renders; g2 is fully hidden
    assert [(r["kind"], r.get("session", {}).get("session_id")) for r in rows] == [
        ("dir", None), ("session", "s1")]
    assert rows[0]["vis_sessions"] == [g1["sessions"][0]]


def test_flatten_rows_show_missing_and_min_ts():
    live = _g("/p/live", sessions=[_s("s1", ts=5000)])
    orphan = _g("/r/.claude/worktrees/w1", sessions=[_s("s2", ts=4000)])
    gone = _g("/gone/x", sessions=[_s("s3", ts=3000)])
    isdir = lambda p: p in ("/p/live", "/r")
    rows = core.flatten_rows([live, orphan, gone], busy=set(), expanded=set(),
                             filt="", home="/h", isdir=isdir, running_ids=set(),
                             show_missing=False)
    assert [r["group"] for r in rows] == [live]
    rows = core.flatten_rows([live, orphan, gone], busy=set(), expanded=set(),
                             filt="", home="/h", isdir=isdir, min_ts=4000,
                             running_ids=set())
    assert [r["group"] for r in rows] == [live, orphan]  # boundary ts kept


def test_group_by_home_attribution_and_order():
    entries = [
        _e(display="first", project="/p/a", session_id="s1", ts=1000),
        # session moved dirs: home stays /p/a
        _e(display="moved", project="/p/b", session_id="s1", ts=5000),
        _e(display="second", project="/p/a", session_id="s2", ts=3000),
        _e(display="newest", project="/p/c", session_id="s3", ts=9000),
    ]
    groups = core.group_by_home(
        entries, transcript_exists=lambda home_dir, *, session_id: True)
    assert [g["path"] for g in groups] == ["/p/c", "/p/a"]
    a = groups[1]
    assert a["last_ts"] == 5000
    assert [s["session_id"] for s in a["sessions"]] == ["s1", "s2"]
    assert a["sessions"][0]["display"] == "moved"


def test_group_by_home_drops_empty_groups():
    entries = [_e(project="/p/a", session_id="s1", ts=1000)]
    assert core.group_by_home(
        entries, transcript_exists=lambda home_dir, *, session_id: False) == []


def test_group_by_home_drops_sessions_without_transcript():
    entries = [_e(project="/p/a", session_id="s1", ts=1000),
               _e(project="/p/a", session_id="s2", ts=2000),
               _e(project="/p/b", session_id="s3", ts=3000)]
    groups = core.group_by_home(
        entries,
        transcript_exists=lambda home_dir, *, session_id: session_id != "s2")
    assert [g["path"] for g in groups] == ["/p/b", "/p/a"]
    assert [s["session_id"] for s in groups[1]["sessions"]] == ["s1"]


def test_live_sessions(tmp_path):
    sdir = tmp_path / "sessions"
    sdir.mkdir()
    proc = tmp_path / "proc"
    proc.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    _fake_session_file(sdir, cwd=work, pid=100, session_id="s-live")  # live claude
    p = proc / "100"
    p.mkdir()
    (p / "comm").write_text("claude\n")
    _fake_session_file(sdir, cwd=work, pid=200, session_id="s-stale")  # pid not running
    _fake_session_file(sdir, cwd=work, pid=300, session_id="s-vim")  # pid alive, not claude
    p = proc / "300"
    p.mkdir()
    (p / "comm").write_text("vim\n")
    (sdir / "400.json").write_text("not json")             # malformed
    busy, running = core.live_sessions(proc_root=str(proc), sessions_dir=str(sdir))
    assert busy == {os.path.realpath(str(work))}
    assert running == {"s-live"}


def test_live_sessions_fallback_to_proc_scan(tmp_path):
    proc = tmp_path / "proc"
    proc.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    p = proc / "500"
    p.mkdir()
    (p / "comm").write_text("claude\n")
    (p / "cwd").symlink_to(work)
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
        {"display": "fix bug", "project": "/p/a", "session_id": "s1", "ts": 1000},
        {"display": "add feat", "project": "/p/b", "session_id": "s2", "ts": 2000},
    ]


def test_parse_history_flattens_control_chars():
    lines = ['{"display":"line one\\nline two\\tend","timestamp":1,"project":"/p","sessionId":"s"}']
    assert core.parse_history(lines)[0]["display"] == "line one line two end"


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
    assert core.relative_time(now + 5_000, now_ms=now) == "0s"  # clock skew clamps to 0


def test_row_spans_badges_and_session():
    g = _g("/r/.claude/worktrees/a1", sessions=[_s("s1", ts=0)])
    row = {"busy": False, "cls": "orphan-worktree", "group": g, "kind": "dir",
           "name": "a1", "repo": "/r", "vis_sessions": g["sessions"]}
    assert (" [worktree gone]", "orphan") in core._row_spans(row, home="/h",
                                                             now_ms=0)
    row["cls"] = "gone"
    assert (" [gone]", "gone") in core._row_spans(row, home="/h", now_ms=0)
    srow = {"busy": False, "cls": "live", "group": g, "kind": "session",
            "name": None, "repo": None, "running": True,
            "session": _s("s1", ts=0)}
    assert core._row_spans(srow, home="/h", now_ms=0) == [
        ("    ", "text"), ("  0s  ", "time"),
        ("(no prompt)", "text"), (" [running]", "running")]


def test_row_spans_dir():
    g = _g("/h/proj", sessions=[_s("s1", display="newest", ts=60_000),
                                _s("s0", display="older", ts=0)])
    row = {"busy": True, "cls": "live", "group": g, "kind": "dir",
           "name": None, "repo": None, "vis_sessions": g["sessions"][1:]}
    # time and prompt come from the newest VISIBLE session (s0), not s1
    assert core._row_spans(row, home="/h", now_ms=120_000) == [
        ("  2m  ", "time"), ("~/proj", "path"),
        (" [running]", "running"), ("  —  older", "text")]


def test_transcript_exists(tmp_path):
    d = tmp_path / "-home-u-x"
    d.mkdir()
    (d / "s1.jsonl").write_text("{}")
    assert core.transcript_exists("/home/u/x", projects_dir=str(tmp_path),
                                  session_id="s1")
    assert not core.transcript_exists("/home/u/x", projects_dir=str(tmp_path),
                                      session_id="s2")


def test_transcript_path():
    p = core.transcript_path("/home/u/scratch", projects_dir="/pp",
                             session_id="abc")
    assert p == "/pp/-home-u-scratch/abc.jsonl"


def test_truncate():
    assert core.truncate("hello", width=10) == "hello"
    assert core.truncate("hello world", width=8) == "hello w…"
    assert core.truncate("hi", width=0) == ""
