from __future__ import annotations

from token_savior.discover.superseded import find_superseded
from token_savior.discover.transcript_scanner import Event


def _ev(tool, session="s1", **args):
    return Event(ts=None, tool_name=tool, args=args, session_id=session, project="p")


def test_reread_is_flagged():
    evs = [_ev("Read", file_path="a.py"), _ev("Bash", command="ls"), _ev("Read", file_path="a.py")]
    f = find_superseded(evs)
    assert any(x["reason"] == "reread" and x["target"] == "a.py" for x in f)


def test_read_then_edit_is_flagged():
    evs = [_ev("Read", file_path="a.py"), _ev("Edit", file_path="a.py")]
    assert any(x["reason"] == "read_then_edit" for x in find_superseded(evs))


def test_duplicate_bash_is_flagged():
    evs = [_ev("Bash", command="pytest -q"), _ev("Bash", command="pytest -q")]
    assert any(x["reason"] == "rerun" for x in find_superseded(evs))


def test_mcp_prefixed_edit_matches():
    evs = [_ev("Read", file_path="a.py"),
           _ev("mcp__token-savior__replace_symbol_source", file_path="a.py")]
    assert any(x["reason"] == "read_then_edit" for x in find_superseded(evs))


def test_distinct_files_not_flagged():
    evs = [_ev("Read", file_path="a.py"), _ev("Read", file_path="b.py")]
    assert find_superseded(evs) == []


def test_cross_session_not_a_reread():
    evs = [_ev("Read", file_path="a.py", session="s1"),
           _ev("Read", file_path="a.py", session="s2")]
    assert find_superseded(evs) == []


def test_deterministic():
    evs = [_ev("Read", file_path="a.py"), _ev("Read", file_path="a.py"),
           _ev("Bash", command="ls"), _ev("Bash", command="ls")]
    assert find_superseded(evs) == find_superseded(list(evs))
