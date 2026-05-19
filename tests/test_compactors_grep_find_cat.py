"""Tests for v4.3.0 F3b compactors: grep / find / cat."""
from __future__ import annotations

from token_savior.compactors import compact, registry
from token_savior.compactors.cat_ import CatCompactor
from token_savior.compactors.find_ import FindCompactor
from token_savior.compactors.grep_ import GrepCompactor


# ---------------------------------------------------------------------------
# Registry membership
# ---------------------------------------------------------------------------


def test_f3b_compactors_registered():
    classes = {c.__class__ for c in registry}
    assert GrepCompactor in classes
    assert FindCompactor in classes
    assert CatCompactor in classes


# ---------------------------------------------------------------------------
# grep
# ---------------------------------------------------------------------------


GREP_RN_SMALL = """src/a.py:14:  found match here
src/a.py:22:  another match
src/b.py:8:   match
src/b.py:12:  match
src/c.py:30:  match
"""


GREP_RN_BIG = (
    "src/api/users.py:14:def get_user(id):\n"
    "src/api/users.py:22:    return User.query.get(id)\n"
    "src/api/users.py:88:    user = get_user(req.user_id)\n"
    "src/api/orders.py:9:from .users import get_user\n"
    "src/api/orders.py:33:    user = get_user(order.user_id)\n"
    "src/api/orders.py:71:        return get_user(order.author_id)\n"
    "src/api/payments.py:14:from .users import get_user\n"
    "src/api/payments.py:55:    payer = get_user(invoice.payer_id)\n"
    "src/services/notifier.py:7:from api.users import get_user\n"
    "src/services/notifier.py:120:    recipient = get_user(event.target)\n"
    "tests/test_users.py:11:    u = get_user(42)\n"
    "tests/test_users.py:34:    assert get_user(99) is None\n"
)


def _make_100_line_grep_fixture() -> str:
    # 100 hits across 8 files — realistic shape of a project-wide grep.
    lines: list[str] = []
    files = [
        "src/core/auth.py",
        "src/core/db.py",
        "src/api/v1.py",
        "src/api/v2.py",
        "src/services/email.py",
        "src/services/billing.py",
        "tests/test_auth.py",
        "tests/test_api.py",
    ]
    line_no = 10
    for i in range(100):
        f = files[i % len(files)]
        lines.append(f"{f}:{line_no}:    match for query #{i}")
        line_no += 3
    return "\n".join(lines) + "\n"


def test_grep_matches_invocations():
    g = GrepCompactor()
    assert g.matches("grep -rn pattern .")
    assert g.matches("grep pattern file")
    assert g.matches("grep -E foo src/")
    assert g.matches("grep -iE foo src/")
    assert g.matches("rg pattern")
    assert g.matches("ripgrep pattern")


def test_grep_does_not_match_lookalikes():
    g = GrepCompactor()
    # Not actually grep
    assert not g.matches("pgrep python")
    assert not g.matches("bzgrep pattern file.gz")
    # Shell composition — bail out
    assert not g.matches("grep foo bar | wc -l")
    assert not g.matches("grep foo bar; echo ok")
    assert not g.matches("grep foo bar && echo found")
    # Already counted output — pass through
    assert not g.matches("grep -c foo bar")
    assert not g.matches("grep --count foo bar")


def test_grep_groups_recursive_output_by_filename():
    g = GrepCompactor()
    out = g.compact(GREP_RN_SMALL)
    # All 3 files present, grouped
    assert "src/a.py (2x): L14, L22" in out
    assert "src/b.py (2x): L8, L12" in out
    assert "src/c.py (1x): L30" in out
    # Original chatty content should be gone
    assert "found match here" not in out
    assert "another match" not in out


def test_grep_passthrough_for_short_output():
    g = GrepCompactor()
    short = "src/a.py:14: hit\nsrc/a.py:22: hit\n"
    out = g.compact(short)
    # 2 meaningful lines, well under 5 — pass-through (or near-equivalent).
    assert "src/a.py" in out
    assert "(2x)" not in out  # no regrouping


def test_grep_100_line_fixture_savings():
    big = _make_100_line_grep_fixture()
    result = compact("grep -rn match src/", big)
    assert result is not None
    assert result.savings_pct >= 50.0
    # Sanity: 8 distinct files each grouped
    assert result.text.count("(") >= 8


def test_grep_realistic_savings():
    result = compact("grep -rn get_user .", GREP_RN_BIG)
    assert result is not None
    # Big realistic case should compress hard
    assert result.savings_pct >= 50.0
    assert "src/api/users.py (3x)" in result.text


def test_grep_single_file_drops_blanks():
    g = GrepCompactor()
    # Non-recursive grep: no path prefix, just matches.
    out = g.compact(
        "first match\n\nsecond match\n\n\nthird match\nfourth match\nfifth match\n"
    )
    # Blank lines collapsed
    assert "\n\n" not in out
    assert "first match" in out


# ---------------------------------------------------------------------------
# find
# ---------------------------------------------------------------------------


def _find_fixture(n: int, root: str = "/root/proj") -> str:
    return "\n".join(f"{root}/dir{i // 10}/file{i}.py" for i in range(n)) + "\n"


def test_find_matches_invocations():
    f = FindCompactor()
    assert f.matches("find .")
    assert f.matches("find /var/log")
    assert f.matches("find . -name '*.py'")
    assert f.matches("  find . -type f")


def test_find_does_not_match_lookalikes_or_pipes():
    f = FindCompactor()
    assert not f.matches("find . -name x | xargs grep foo")
    assert not f.matches("findutils something")
    assert not f.matches("grep foo .")


def test_find_passthrough_small():
    f = FindCompactor()
    src = _find_fixture(20)
    out = f.compact(src)
    # 20 ≤ 30 — pass-through
    assert out.strip().splitlines() == src.strip().splitlines()


def test_find_medium_strips_prefix_and_truncates():
    src = _find_fixture(80)
    result = compact("find /root/proj -type f", src)
    assert result is not None
    assert "(60 more)" in result.text  # 80 - 15 - 5
    # Common prefix /root/proj/ should be stripped from the visible head/tail.
    assert "/root/proj/dir0/file0.py" not in result.text
    assert result.text.splitlines()[0].startswith("dir0/")
    # Savings clearly above 50%
    assert result.savings_pct >= 50.0


def test_find_large_head_tail():
    src = _find_fixture(300)
    result = compact("find /root/proj -type f", src)
    assert result is not None
    assert "items total" in result.text
    # 300 - 10 - 5 = 285 more
    assert "285 more" in result.text
    assert result.savings_pct >= 50.0


# ---------------------------------------------------------------------------
# cat
# ---------------------------------------------------------------------------


def _cat_fixture(n: int) -> str:
    return "\n".join(f"line {i}: content of line {i} in the file" for i in range(n)) + "\n"


def test_cat_matches_invocations():
    c = CatCompactor()
    assert c.matches("cat file.txt")
    assert c.matches("cat -n file.py")
    assert c.matches("cat /etc/hosts")


def test_cat_does_not_match_pipes_or_lookalikes():
    c = CatCompactor()
    assert not c.matches("cat foo | grep bar")
    assert not c.matches("cat foo && echo done")
    assert not c.matches("catfish")
    assert not c.matches("category")


def test_cat_passthrough_small():
    c = CatCompactor()
    src = _cat_fixture(40)
    out = c.compact(src)
    # Should not truncate
    assert "elided" not in out
    assert out.count("\n") >= 39


def test_cat_medium_truncation():
    src = _cat_fixture(120)
    result = compact("cat big.txt", src)
    assert result is not None
    # 120 - 25 - 10 = 85 elided
    assert "85 lines elided" in result.text
    assert result.savings_pct >= 50.0
    # Head kept
    assert "line 0:" in result.text
    # Tail kept
    assert "line 119:" in result.text


def test_cat_large_truncation():
    src = _cat_fixture(500)
    result = compact("cat huge.log", src)
    assert result is not None
    # 500 - 30 - 10 = 460 elided
    assert "460 lines elided" in result.text
    assert result.savings_pct >= 80.0
