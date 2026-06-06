import json
import os

import reclaude as cr


def test_parse_history_basic():
    lines = [
        '{"display":"fix bug","pastedContents":{},"timestamp":1000,"project":"/p/a","sessionId":"s1"}',
        '{"display":"add feat","timestamp":2000,"project":"/p/b","sessionId":"s2"}',
    ]
    entries = cr.parse_history(lines)
    assert entries == [
        {"project": "/p/a", "session_id": "s1", "ts": 1000, "display": "fix bug"},
        {"project": "/p/b", "session_id": "s2", "ts": 2000, "display": "add feat"},
    ]


def test_parse_history_skips_garbage():
    lines = [
        "not json at all",
        '{"display":"missing fields"}',
        '"a json string, not an object"',
        '{"display":null,"timestamp":3000,"project":"/p/c","sessionId":"s3"}',
        "",
    ]
    entries = cr.parse_history(lines)
    # Only the entry with all required fields survives; null display becomes ""
    assert entries == [{"project": "/p/c", "session_id": "s3", "ts": 3000, "display": ""}]


def _e(project, sid, ts, display=""):
    return {"project": project, "session_id": sid, "ts": ts, "display": display}


def test_group_by_home_attribution_and_order():
    entries = [
        _e("/p/a", "s1", 1000, "first"),
        _e("/p/b", "s1", 5000, "moved"),   # session moved dirs: home stays /p/a
        _e("/p/a", "s2", 3000, "second"),
        _e("/p/c", "s3", 9000, "newest"),
    ]
    groups = cr.group_by_home(entries, transcript_exists=lambda h, s: True)
    assert [g["path"] for g in groups] == ["/p/c", "/p/a"]
    a = groups[1]
    assert a["last_ts"] == 5000
    assert [s["session_id"] for s in a["sessions"]] == ["s1", "s2"]
    assert a["sessions"][0]["display"] == "moved"


def test_group_by_home_drops_sessions_without_transcript():
    entries = [_e("/p/a", "s1", 1000), _e("/p/a", "s2", 2000), _e("/p/b", "s3", 3000)]
    groups = cr.group_by_home(entries, transcript_exists=lambda h, sid: sid != "s2")
    assert [g["path"] for g in groups] == ["/p/b", "/p/a"]
    assert [s["session_id"] for s in groups[1]["sessions"]] == ["s1"]


def test_group_by_home_drops_empty_groups():
    entries = [_e("/p/a", "s1", 1000)]
    assert cr.group_by_home(entries, transcript_exists=lambda h, s: False) == []


def _fake_proc(tmp_path, pid, comm, cwd_target):
    p = tmp_path / str(pid)
    p.mkdir()
    (p / "comm").write_text(comm + "\n")
    (p / "cwd").symlink_to(cwd_target)


def test_find_busy_dirs(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    _fake_proc(tmp_path, 100, "claude", work)
    _fake_proc(tmp_path, 200, "vim", other)        # wrong comm: ignored
    (tmp_path / "self").mkdir()                     # non-numeric entry: ignored
    broken = tmp_path / "300"
    broken.mkdir()                                  # numeric but no comm file: ignored
    busy = cr.find_busy_dirs(proc_root=str(tmp_path))
    assert busy == {os.path.realpath(str(work))}


def test_find_busy_dirs_missing_proc_root():
    assert cr.find_busy_dirs(proc_root="/nonexistent-proc") == set()


def test_relative_time():
    now = 10_000_000_000_000
    assert cr.relative_time(now - 5_000, now) == "5s"
    assert cr.relative_time(now - 90_000, now) == "1m"
    assert cr.relative_time(now - 3 * 3600_000, now) == "3h"
    assert cr.relative_time(now - 49 * 3600_000, now) == "2d"
    assert cr.relative_time(now + 5_000, now) == "0s"  # clock skew clamps to 0


def test_abbreviate_path():
    assert cr.abbreviate_path("/home/u/proj", "/home/u") == "~/proj"
    assert cr.abbreviate_path("/home/u", "/home/u") == "~"
    assert cr.abbreviate_path("/home/uother/x", "/home/u") == "/home/uother/x"
    assert cr.abbreviate_path("/etc/x", "/home/u") == "/etc/x"


def test_parse_history_flattens_control_chars():
    lines = ['{"display":"line one\\nline two\\tend","timestamp":1,"project":"/p","sessionId":"s"}']
    assert cr.parse_history(lines)[0]["display"] == "line one line two end"


def test_truncate():
    assert cr.truncate("hello", 10) == "hello"
    assert cr.truncate("hello world", 8) == "hello w…"
    assert cr.truncate("hi", 0) == ""


def test_mung_path():
    assert cr.mung_path("/home/u/scratch") == "-home-u-scratch"
    assert cr.mung_path("/home/u/repo/.claude/worktrees/a1") == \
        "-home-u-repo--claude-worktrees-a1"


def test_transcript_path():
    p = cr.transcript_path("/home/u/scratch", "abc", projects_dir="/pp")
    assert p == "/pp/-home-u-scratch/abc.jsonl"


def test_transcript_exists(tmp_path):
    d = tmp_path / "-home-u-x"
    d.mkdir()
    (d / "s1.jsonl").write_text("{}")
    assert cr.transcript_exists("/home/u/x", "s1", projects_dir=str(tmp_path))
    assert not cr.transcript_exists("/home/u/x", "s2", projects_dir=str(tmp_path))


def test_classify_dir():
    assert cr.classify_dir("/x", isdir=lambda p: True) == ("live", None, None)
    assert cr.classify_dir("/r/.claude/worktrees/a1", isdir=lambda p: p == "/r") == \
        ("orphan-worktree", "/r", "a1")
    assert cr.classify_dir("/gone/dir", isdir=lambda p: False) == ("gone", None, None)


def test_classify_dir_worktree_repo_also_gone():
    assert cr.classify_dir("/r/.claude/worktrees/a1", isdir=lambda p: False) == \
        ("gone", None, None)


def _fake_session_file(dirpath, pid, sid, cwd):
    (dirpath / f"{pid}.json").write_text(
        json.dumps({"pid": pid, "sessionId": sid, "cwd": str(cwd)}))


def test_live_sessions(tmp_path):
    sdir = tmp_path / "sessions"
    sdir.mkdir()
    proc = tmp_path / "proc"
    proc.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    _fake_session_file(sdir, 100, "s-live", work)          # live claude
    p = proc / "100"
    p.mkdir()
    (p / "comm").write_text("claude\n")
    _fake_session_file(sdir, 200, "s-stale", work)         # pid not running
    _fake_session_file(sdir, 300, "s-vim", work)           # pid alive, not claude
    p = proc / "300"
    p.mkdir()
    (p / "comm").write_text("vim\n")
    (sdir / "400.json").write_text("not json")             # malformed
    busy, running = cr.live_sessions(sessions_dir=str(sdir), proc_root=str(proc))
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
    busy, running = cr.live_sessions(sessions_dir=str(tmp_path / "missing"),
                                     proc_root=str(proc))
    assert busy == {os.path.realpath(str(work))}
    assert running == set()


def _g(path, sessions):
    return {"path": path, "last_ts": sessions[0]["ts"], "sessions": sessions}


def _s(sid, ts, display=""):
    return {"session_id": sid, "ts": ts, "display": display}


def test_flatten_rows_expansion_filter_running():
    g1 = _g("/p/a", [_s("s1", 2000, "x"), _s("s2", 1000, "y")])
    g2 = _g("/p/b", [_s("s3", 500, "z")])
    rows = cr.flatten_rows([g1, g2], expanded={"/p/a"}, filt="", home="/h",
                           busy=set(), running_ids={"s2"}, isdir=lambda p: True)
    assert [(r["kind"], r.get("session", {}).get("session_id")) for r in rows] == [
        ("dir", None), ("session", "s1"), ("session", "s2"), ("dir", None)]
    assert rows[2]["running"] is True and rows[1]["running"] is False
    rows = cr.flatten_rows([g1, g2], expanded={"/p/a"}, filt="B", home="/h",
                           busy=set(), running_ids=set(), isdir=lambda p: True)
    assert len(rows) == 1 and rows[0]["group"] is g2


def test_flatten_rows_busy_and_classification():
    g = _g("/r/.claude/worktrees/a1", [_s("s1", 100)])
    rows = cr.flatten_rows([g], expanded={"/r/.claude/worktrees/a1"}, filt="",
                           home="/h", busy={"/r/.claude/worktrees/a1"},
                           running_ids=set(), isdir=lambda p: p == "/r")
    assert rows[0]["cls"] == "orphan-worktree"
    assert (rows[0]["repo"], rows[0]["name"]) == ("/r", "a1")
    assert rows[0]["busy"] is True and rows[1]["busy"] is True


def test_row_spans_dir():
    g = _g("/h/proj", [_s("s1", 60_000, "newest"), _s("s0", 0, "older")])
    row = {"kind": "dir", "group": g, "vis_sessions": g["sessions"][1:],
           "cls": "live", "repo": None, "name": None, "busy": True}
    # time and prompt come from the newest VISIBLE session (s0), not s1
    assert cr._row_spans(row, now_ms=120_000, home="/h") == [
        ("  2m  ", "time"), ("~/proj", "path"),
        (" [running]", "running"), ("  —  older", "text")]


def test_row_spans_badges_and_session():
    g = _g("/r/.claude/worktrees/a1", [_s("s1", 0, "")])
    row = {"kind": "dir", "group": g, "vis_sessions": g["sessions"], "cls": "orphan-worktree",
           "repo": "/r", "name": "a1", "busy": False}
    assert (" [worktree gone]", "orphan") in cr._row_spans(row, 0, "/h")
    row["cls"] = "gone"
    assert (" [gone]", "gone") in cr._row_spans(row, 0, "/h")
    srow = {"kind": "session", "group": g, "session": _s("s1", 0, ""),
            "cls": "live", "repo": None, "name": None, "busy": False,
            "running": True}
    assert cr._row_spans(srow, 0, "/h") == [
        ("    ", "text"), ("  0s  ", "time"),
        ("(no prompt)", "text"), (" [running]", "running")]


def test_flatten_rows_show_missing_and_min_ts():
    live = _g("/p/live", [_s("s1", 5000)])
    orphan = _g("/r/.claude/worktrees/w1", [_s("s2", 4000)])
    gone = _g("/gone/x", [_s("s3", 3000)])
    isdir = lambda p: p in ("/p/live", "/r")
    rows = cr.flatten_rows([live, orphan, gone], expanded=set(), filt="",
                           home="/h", busy=set(), running_ids=set(),
                           isdir=isdir, show_missing=False)
    assert [r["group"] for r in rows] == [live]
    rows = cr.flatten_rows([live, orphan, gone], expanded=set(), filt="",
                           home="/h", busy=set(), running_ids=set(),
                           isdir=isdir, min_ts=4000)
    assert [r["group"] for r in rows] == [live, orphan]  # boundary ts kept


def test_flatten_rows_filters_apply_before_cap():
    gone_groups = [_g(f"/gone/{i}", [_s(f"g{i}", 10_000 - i)])
                   for i in range(cr.MAX_DIRS)]
    live_old = _g("/p/old", [_s("old", 1)])
    rows = cr.flatten_rows(gone_groups + [live_old], expanded=set(), filt="",
                           home="/h", busy=set(), running_ids=set(),
                           isdir=lambda p: p == "/p/old", show_missing=False)
    assert len(rows) == 1 and rows[0]["group"] is live_old


def test_flatten_rows_prompt_text_match():
    g1 = _g("/p/a", [_s("s1", 2000, "fix the parser"), _s("s2", 1000, "other")])
    g2 = _g("/p/b", [_s("s3", 500, "unrelated")])
    rows = cr.flatten_rows([g1, g2], expanded={"/p/a", "/p/b"}, filt="PARSER",
                           home="/h", busy=set(), running_ids=set(),
                           isdir=lambda p: True)
    # dir surfaces on prompt match though its path doesn't match the filter;
    # only the matching session renders; g2 is fully hidden
    assert [(r["kind"], r.get("session", {}).get("session_id")) for r in rows] == [
        ("dir", None), ("session", "s1")]
    assert rows[0]["vis_sessions"] == [g1["sessions"][0]]


def test_flatten_rows_age_filters_sessions_and_dir():
    g = _g("/p/a", [_s("new", 5000, "recent"), _s("mid", 3000, "middle"),
                    _s("old", 1000, "ancient")])
    rows = cr.flatten_rows([g], expanded={"/p/a"}, filt="", home="/h",
                           busy=set(), running_ids=set(), isdir=lambda p: True,
                           min_ts=3000)
    assert [r.get("session", {}).get("session_id") for r in rows] == [
        None, "new", "mid"]                       # "old" hidden; boundary kept
    assert rows[0]["vis_sessions"][0]["session_id"] == "new"
    assert cr.flatten_rows([g], expanded=set(), filt="", home="/h",
                           busy=set(), running_ids=set(), isdir=lambda p: True,
                           min_ts=6000) == []     # no survivor -> dir hidden


def test_flatten_rows_dir_top_reflects_filter():
    g = _g("/p/a", [_s("s1", 2000, "alpha"), _s("s2", 1000, "beta")])
    rows = cr.flatten_rows([g], expanded=set(), filt="beta", home="/h",
                           busy=set(), running_ids=set(), isdir=lambda p: True)
    assert rows[0]["vis_sessions"][0]["session_id"] == "s2"
