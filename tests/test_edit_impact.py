"""Edit-impact notice: fold callers + impacted tests into the edit result.

Covers _edit_impact_notice / _edit_succeeded (server.py). The point of the
feature is that the safety value of get_edit_context is delivered by default
on every symbol edit, so the audit's 0/219 adoption gap stops mattering.
"""
from types import SimpleNamespace

from token_savior import server


def _slot(callers=None, tests=None):
    def get_dependents(symbol, max_results=8):
        return callers or []

    def find_impacted_test_files(symbol_names=None, max_tests=5):
        return {"impacted_tests": tests or []}

    return SimpleNamespace(
        query_fns={
            "get_dependents": get_dependents,
            "find_impacted_test_files": find_impacted_test_files,
        }
    )


def test_notice_lists_callers_and_tests(monkeypatch):
    monkeypatch.setattr(server, "_EDIT_IMPACT_DISABLED", False)
    slot = _slot(
        callers=[{"name": "router.handle"}, {"name": "worker.run"}],
        tests=["tests/test_router.py"],
    )
    notice = server._edit_impact_notice(slot, "replace_symbol_source", "billing.charge")
    assert notice is not None
    assert "[EDIT IMPACT]" in notice
    assert "billing.charge" in notice
    assert "router.handle" in notice and "worker.run" in notice
    assert "callers (2)" in notice
    assert "tests/test_router.py" in notice


def test_notice_none_when_disabled(monkeypatch):
    monkeypatch.setattr(server, "_EDIT_IMPACT_DISABLED", True)
    slot = _slot(callers=[{"name": "a.b"}])
    assert server._edit_impact_notice(slot, "replace_symbol_source", "foo") is None


def test_notice_none_for_non_edit_tool(monkeypatch):
    monkeypatch.setattr(server, "_EDIT_IMPACT_DISABLED", False)
    slot = _slot(callers=[{"name": "a.b"}])
    assert server._edit_impact_notice(slot, "get_function_source", "foo") is None


def test_notice_none_without_symbol(monkeypatch):
    monkeypatch.setattr(server, "_EDIT_IMPACT_DISABLED", False)
    assert server._edit_impact_notice(_slot(), "replace_symbol_source", "") is None


def test_notice_none_when_no_callers_no_tests(monkeypatch):
    monkeypatch.setattr(server, "_EDIT_IMPACT_DISABLED", False)
    assert server._edit_impact_notice(_slot(), "replace_symbol_source", "foo") is None


def test_notice_survives_query_fn_errors(monkeypatch):
    monkeypatch.setattr(server, "_EDIT_IMPACT_DISABLED", False)

    def boom(*a, **k):
        raise RuntimeError("index cold")

    slot = SimpleNamespace(
        query_fns={"get_dependents": boom, "find_impacted_test_files": boom}
    )
    # Both query fns raise -> no parts -> None, never propagates.
    assert server._edit_impact_notice(slot, "insert_near_symbol", "foo") is None


def test_edit_succeeded_detects_error_payload():
    ok = [SimpleNamespace(text="Replaced billing.charge (12 lines)")]
    err = [SimpleNamespace(text="Error: symbol not found")]
    assert server._edit_succeeded(ok) is True
    assert server._edit_succeeded(err) is False
