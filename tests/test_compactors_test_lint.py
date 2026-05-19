"""Tests for F1a test/lint compactors (jest, vitest, eslint, biome)."""
from __future__ import annotations

from token_savior.compactors import compact, registry
from token_savior.compactors.biome import BiomeCompactor
from token_savior.compactors.eslint import EslintCompactor
from token_savior.compactors.jest import JestCompactor
from token_savior.compactors.vitest import VitestCompactor


# ---------------------------------------------------------------------------
# Registry membership
# ---------------------------------------------------------------------------


def test_f1a_compactors_are_registered():
    classes = {c.__class__ for c in registry}
    assert JestCompactor in classes
    assert VitestCompactor in classes
    assert EslintCompactor in classes
    assert BiomeCompactor in classes


# ---------------------------------------------------------------------------
# Jest
# ---------------------------------------------------------------------------


JEST_FAIL_OUTPUT = """PASS  src/utils/dates.test.ts
PASS  src/utils/strings.test.ts
PASS  src/components/Button.test.tsx
PASS  src/components/Card.test.tsx
PASS  src/services/api.test.ts
FAIL  src/services/payments.test.ts (3.214 s)
  Payments service
    × charges customer (45 ms)
    × refunds order (12 ms)

  ● Payments service › charges customer

    expect(received).toBe(expected) // Object.is equality

    Expected: 100
    Received: 99

      14 |   it('charges customer', () => {
      15 |     const total = computeTotal(items);
    > 16 |     expect(total).toBe(100);
         |                   ^
      17 |   });

      at Object.<anonymous> (src/services/payments.test.ts:16:19)

  ● Payments service › refunds order

    TypeError: Cannot read properties of undefined (reading 'amount')

      28 |   it('refunds order', () => {
      29 |     const order = lookupOrder(id);
    > 30 |     return refund(order.amount);
         |                         ^
      31 |   });

PASS  src/services/users.test.ts
PASS  src/services/orders.test.ts
PASS  src/services/inventory.test.ts

Test Suites: 1 failed, 8 passed, 9 total
Tests:       2 failed, 42 passed, 44 total
Snapshots:   0 total
Time:        4.213 s
Ran all test suites.
"""


def test_jest_matches_invocations():
    j = JestCompactor()
    assert j.matches("jest")
    assert j.matches("jest --coverage")
    assert j.matches("npx jest")
    assert j.matches("yarn jest --watch")
    assert j.matches("pnpm jest")
    assert j.matches("pnpm run jest")
    assert not j.matches("eslint")
    assert not j.matches("vitest")
    # Substring guard: arbitrary mention of "jest" inside another word must not match
    assert not j.matches("jest-mock-import-helper")


def test_jest_compaction_failure_heavy():
    result = compact("npx jest", JEST_FAIL_OUTPUT)
    assert result is not None
    # Pass lines gone
    assert "PASS  src/utils/dates" not in result.text
    assert "PASS  src/components/Button" not in result.text
    # Fail file kept
    assert "FAIL  src/services/payments.test.ts" in result.text
    # Summary kept
    assert "Tests:" in result.text and "2 failed" in result.text
    # Significant savings on failure-heavy output
    assert result.savings_pct >= 60.0


def test_jest_all_green_compresses_aggressively():
    green = (
        "PASS src/a.test.ts\nPASS src/b.test.ts\nPASS src/c.test.ts\n"
        "PASS src/d.test.ts\nPASS src/e.test.ts\nPASS src/f.test.ts\n"
        "PASS src/g.test.ts\nPASS src/h.test.ts\nPASS src/i.test.ts\n"
        "PASS src/j.test.ts\n"
        "Test Suites: 10 passed, 10 total\n"
        "Tests:       120 passed, 120 total\n"
        "Snapshots:   0 total\n"
        "Time:        2.118 s\n"
    )
    result = compact("jest", green)
    assert result is not None
    assert "PASS" not in result.text
    assert "120 passed" in result.text
    assert result.savings_pct >= 60.0


def test_jest_strips_ansi_codes():
    ansi = "\x1b[32mPASS\x1b[0m src/a.test.ts\n\x1b[31mFAIL\x1b[0m src/b.test.ts\nTests: 1 failed, 1 passed\n"
    result = compact("jest", ansi)
    assert result is not None
    assert "\x1b[" not in result.text


# ---------------------------------------------------------------------------
# Vitest
# ---------------------------------------------------------------------------


VITEST_FAIL_OUTPUT = """ RUN  v1.6.0 /home/me/proj

 ✓ src/utils/dates.test.ts (12)
 ✓ src/utils/strings.test.ts (8)
 ✓ src/components/Button.test.tsx (5)
 ✓ src/components/Card.test.tsx (4)
 ✓ src/services/api.test.ts (9)
 ❯ src/services/payments.test.ts (2)
   × Payments service > charges customer
   × Payments service > refunds order

⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯ Failed Tests 2 ⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯

 FAIL  src/services/payments.test.ts > Payments service > charges customer
AssertionError: expected 99 to be 100

 ❯ src/services/payments.test.ts:16:19
     14|   it('charges customer', () => {
     15|     const total = computeTotal(items);
     16|     expect(total).toBe(100);
       |                   ^
     17|   });

 FAIL  src/services/payments.test.ts > Payments service > refunds order
TypeError: Cannot read properties of undefined (reading 'amount')

 ❯ src/services/payments.test.ts:30:25

 ✓ src/services/users.test.ts (10)
 ✓ src/services/orders.test.ts (12)

 Test Files  1 failed | 7 passed (8)
      Tests  2 failed | 58 passed (60)
   Start at  10:14:22
   Duration  1.43s (transform 220ms, setup 0ms)
"""


def test_vitest_matches_invocations():
    v = VitestCompactor()
    assert v.matches("vitest")
    assert v.matches("vitest run")
    assert v.matches("npx vitest")
    assert v.matches("yarn vitest")
    assert v.matches("pnpm vitest")
    assert v.matches("pnpm run vitest --coverage")
    assert v.matches("pnpm exec vitest")
    assert not v.matches("jest")
    assert not v.matches("vitest-config-helper")


def test_vitest_compaction_failure_heavy():
    result = compact("vitest", VITEST_FAIL_OUTPUT)
    assert result is not None
    # All passing ✓ lines removed
    assert "✓ src/utils/dates" not in result.text
    assert "✓ src/utils/strings" not in result.text
    # Failures preserved
    assert "payments" in result.text.lower()
    # Summary preserved
    assert "Test Files" in result.text
    assert result.savings_pct >= 60.0


def test_vitest_all_green_aggressive():
    green = (
        " RUN  v1.6.0\n"
        + "\n".join(f" ✓ src/test_{i}.test.ts (5)" for i in range(20))
        + "\n\n Test Files  20 passed (20)\n      Tests  100 passed (100)\n   Duration  0.8s\n"
    )
    result = compact("vitest", green)
    assert result is not None
    assert "✓ src/test_0" not in result.text
    assert "Test Files" in result.text
    assert result.savings_pct >= 60.0


# ---------------------------------------------------------------------------
# ESLint
# ---------------------------------------------------------------------------


ESLINT_OUTPUT = """
/home/me/proj/src/foo.ts
  14:5   error    'x' is defined but never used                 no-unused-vars
  19:12  error    'y' is defined but never used                 no-unused-vars
  22:1   error    'unused' is defined but never used            no-unused-vars
  27:3   warning  Missing semicolon                              semi

/home/me/proj/src/bar.ts
  33:7   error    'z' is defined but never used                 no-unused-vars
  44:1   error    'foo' is defined but never used               no-unused-vars
  55:3   error    'bar' is defined but never used               no-unused-vars

/home/me/proj/src/baz.tsx
  9:5    error    Missing 'children' prop                       react/no-children-prop
  18:5   error    Missing 'children' prop                       react/no-children-prop
  27:5   error    Missing 'children' prop                       react/no-children-prop
  88:1   error    'unused' is defined but never used            no-unused-vars
  99:1   error    'also' is defined but never used              no-unused-vars

✖ 12 problems (11 errors, 1 warning)
"""


def test_eslint_matches_invocations():
    e = EslintCompactor()
    assert e.matches("eslint")
    assert e.matches("eslint .")
    assert e.matches("npx eslint src/")
    assert e.matches("yarn eslint --fix")
    assert e.matches("pnpm eslint")
    assert e.matches("pnpm run eslint")
    assert e.matches("pnpm exec eslint")
    assert not e.matches("jest")
    # Not a command, just text mentioning eslint
    assert not e.matches("eslint-config-airbnb")


def test_eslint_groups_by_rule():
    result = compact("eslint .", ESLINT_OUTPUT)
    assert result is not None
    assert "no-unused-vars" in result.text
    assert "react/no-children-prop" in result.text
    # Counts present (foo.ts: 3, bar.ts: 3, baz.tsx: 2 = 8 unused-vars total)
    assert "(8x)" in result.text  # no-unused-vars total
    assert "(3x)" in result.text  # react/no-children-prop
    # Summary preserved
    assert "12 problems" in result.text or "11 errors" in result.text
    assert result.savings_pct >= 50.0


def test_eslint_clean_returns_ok_or_summary():
    # Truly empty output -> dispatcher returns None
    assert compact("eslint .", "") is None
    # Some whitespace -> compactor returns a stub "ok"
    result = compact("eslint .", "   \n")
    assert result is not None
    assert result.text.strip() in {"ok", ""}


# ---------------------------------------------------------------------------
# Biome
# ---------------------------------------------------------------------------


BIOME_OUTPUT = """./src/foo.ts:14:5 lint/suspicious/noExplicitAny ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  × Unexpected any. Specify a different type.

    12 │ function take(value) {
    13 │   return value;
  > 14 │ const v: any = 1;
       │     ^^^^^^^^^
    15 │ }

  i This rule is fixable.

./src/foo.ts:22:1 lint/suspicious/noExplicitAny ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  × Unexpected any.

./src/bar.ts:9:3 lint/correctness/noUnusedVariables ━━━━━━━━━━━━━━━━━━━━━━━━

  × This variable is unused.

./src/bar.ts:33:5 lint/correctness/noUnusedVariables ━━━━━━━━━━━━━━━━━━━━━━━━

  × This variable is unused.

./src/baz.tsx:5:1 lint/style/useConst ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  × Use const instead.

Checked 24 files in 130ms. Found 5 errors.
"""


def test_biome_matches_invocations():
    b = BiomeCompactor()
    assert b.matches("biome check")
    assert b.matches("biome lint")
    assert b.matches("biome check ./src")
    assert b.matches("npx biome check")
    assert b.matches("pnpm biome lint")
    assert b.matches("yarn biome check")
    assert b.matches("pnpm dlx biome check")
    assert not b.matches("jest")
    assert not b.matches("biome-config-foo")


def test_biome_groups_by_rule():
    result = compact("biome check", BIOME_OUTPUT)
    assert result is not None
    assert "lint/suspicious/noExplicitAny" in result.text
    assert "lint/correctness/noUnusedVariables" in result.text
    assert "lint/style/useConst" in result.text
    assert "(2x)" in result.text
    # Summary verdict preserved
    assert "5 errors" in result.text or "Checked 24 files" in result.text
    assert result.savings_pct >= 60.0
