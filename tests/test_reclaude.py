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


def test_group_by_project_orders_and_aggregates():
    entries = [
        _e("/p/a", "s1", 1000, "first"),
        _e("/p/a", "s1", 5000, "latest in s1"),
        _e("/p/a", "s2", 3000, "only in s2"),
        _e("/p/b", "s3", 9000, "newest dir"),
    ]
    groups = cr.group_by_project(entries, dir_exists=lambda p: True)
    assert [g["path"] for g in groups] == ["/p/b", "/p/a"]  # newest first
    a = groups[1]
    assert a["last_ts"] == 5000
    assert [s["session_id"] for s in a["sessions"]] == ["s1", "s2"]  # newest first
    assert a["sessions"][0] == {"session_id": "s1", "ts": 5000, "display": "latest in s1"}


def test_group_by_project_drops_missing_dirs():
    entries = [_e("/gone", "s1", 1000), _e("/here", "s2", 2000)]
    groups = cr.group_by_project(entries, dir_exists=lambda p: p == "/here")
    assert [g["path"] for g in groups] == ["/here"]


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
