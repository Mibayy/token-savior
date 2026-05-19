"""Tests for bash output compactors (F1)."""
from __future__ import annotations

from token_savior.compactors import compact, registry
from token_savior.compactors.base import CompactResult, Compactor


# ---------------------------------------------------------------------------
# Generic dispatch
# ---------------------------------------------------------------------------


def test_compact_returns_none_for_unknown_command():
    assert compact("some-random-tool --flag", "lots of output\n" * 50) is None


def test_compact_returns_none_for_empty_output():
    assert compact("git status", "") is None


def test_registry_is_non_empty_and_unique_ordered():
    assert len(registry) >= 7
    names = [c.__class__.__name__ for c in registry]
    assert len(names) == len(set(names)), "duplicate compactor class in registry"


def test_compact_result_dataclass_savings_match():
    r = CompactResult(text="short", original_bytes=1000, compact_bytes=50, savings_pct=95.0)
    assert r.savings_pct == 95.0
    assert r.compact_bytes < r.original_bytes


# ---------------------------------------------------------------------------
# git status
# ---------------------------------------------------------------------------


GIT_STATUS_OUTPUT = """On branch feature/big-refactor
Your branch is up to date with 'origin/feature/big-refactor'.

Changes to be committed:
  (use "git restore --staged <file>..." to unstage)
\tmodified:   src/token_savior/server.py
\tmodified:   src/token_savior/cli.py
\tnew file:   src/token_savior/compactors/base.py
\tnew file:   src/token_savior/compactors/git.py

Changes not staged for commit:
  (use "git add <file>..." to update what will be committed)
  (use "git restore <file>..." to discard changes in working directory)
\tmodified:   tests/test_compactors.py
\tmodified:   hooks/tool_capture_hook.py
\tdeleted:    docs/old.md

Untracked files:
  (use "git add <file>..." to include in what will be committed)
\tnotes.txt
\tscratch/

no changes added to commit (use "git add" and/or "git commit -a")
"""


def test_git_status_compaction():
    result = compact("git status", GIT_STATUS_OUTPUT)
    assert result is not None
    assert "feature/big-refactor" in result.text
    assert "src/token_savior/server.py" in result.text
    assert "notes.txt" in result.text
    # Verbose hint lines must be gone
    assert "use \"git restore --staged" not in result.text
    assert "no changes added to commit" not in result.text
    assert result.savings_pct >= 50.0


# ---------------------------------------------------------------------------
# git diff
# ---------------------------------------------------------------------------


GIT_DIFF_OUTPUT = """diff --git a/foo.py b/foo.py
index 1234..5678 100644
--- a/foo.py
+++ b/foo.py
@@ -1,12 +1,14 @@
 import os
 import sys

-def hello():
-    return "hi"
+def hello(name):
+    return f"hi {name}"

 def unused():
     pass

 def main():
     print(hello())
+    print("done")

 if __name__ == "__main__":
     main()
diff --git a/bar.py b/bar.py
index aaaa..bbbb 100644
--- a/bar.py
+++ b/bar.py
@@ -5,7 +5,7 @@ class Bar:
     def __init__(self):
         self.x = 0

-    def foo(self):
+    def foo(self, y):
         return self.x

     def baz(self):
"""


def test_git_diff_drops_unchanged_context():
    result = compact("git diff", GIT_DIFF_OUTPUT)
    assert result is not None
    # Keep the changed lines and file headers
    assert "+def hello(name):" in result.text
    assert "-def hello():" in result.text
    assert "foo.py" in result.text
    assert "bar.py" in result.text
    # Context lines like "import os" should be dropped
    assert "import os" not in result.text
    assert "if __name__" not in result.text
    assert result.savings_pct >= 30.0


# ---------------------------------------------------------------------------
# git log
# ---------------------------------------------------------------------------


GIT_LOG_OUTPUT = """commit 95213c6b9b8e6f55a4f55c0c1d2e3f4a5b6c7d8e
Author: Louis <louis@example.com>
Date:   Mon May 19 10:00:00 2026 +0200

    fix(cli): remove unused 'io' import + 'project_name' var (ruff)

commit 378cc939e8d7f6b5c4d3e2f1a0b9c8d7e6f5a4b3
Author: Louis <louis@example.com>
Date:   Sun May 18 18:00:00 2026 +0200

    fix(site): hero-sub final v4 copy

commit 766f1620192837465564738291092837465a1b2c
Author: Louis <louis@example.com>
Date:   Sun May 18 17:00:00 2026 +0200

    docs(site): update landing for v4.0 — one MCP, one profile pitch

commit 632cffd11223344556677889900aabbccddeeff0
Author: Louis <louis@example.com>
Date:   Sat May 17 12:00:00 2026 +0200

    release: v4.0.0 — single 'optimized' profile, 97.9% @ −80% tokens
"""


def test_git_log_oneline_compaction():
    result = compact("git log", GIT_LOG_OUTPUT)
    assert result is not None
    # First-line subject of each commit must survive
    assert "fix(cli): remove unused" in result.text
    assert "release: v4.0.0" in result.text
    # Author and Date lines must be stripped
    assert "Author:" not in result.text
    assert "Date:" not in result.text
    assert result.savings_pct >= 60.0


# ---------------------------------------------------------------------------
# git push/pull/add/commit single-line
# ---------------------------------------------------------------------------


GIT_PUSH_OUTPUT = """Enumerating objects: 21, done.
Counting objects: 100% (21/21), done.
Delta compression using up to 8 threads
Compressing objects: 100% (12/12), done.
Writing objects: 100% (12/12), 1.42 KiB | 1.42 MiB/s, done.
Total 12 (delta 8), reused 0 (delta 0), pack-reused 0
remote: Resolving deltas: 100% (8/8), completed with 7 local objects.
To github.com:Mibayy/token-savior.git
   95213c6..a1b2c3d  main -> main
"""


def test_git_push_single_line():
    result = compact("git push origin main", GIT_PUSH_OUTPUT)
    assert result is not None
    assert "ok" in result.text.lower()
    assert "main -> main" in result.text or "a1b2c3d" in result.text
    assert result.savings_pct >= 60.0


GIT_COMMIT_OUTPUT = """[main a1b2c3d] F1: bash output compactors
 8 files changed, 412 insertions(+), 3 deletions(-)
 create mode 100644 src/token_savior/compactors/__init__.py
 create mode 100644 src/token_savior/compactors/base.py
 create mode 100644 src/token_savior/compactors/git.py
 create mode 100644 src/token_savior/compactors/pytest_.py
 create mode 100644 src/token_savior/compactors/cargo_.py
 create mode 100644 src/token_savior/compactors/tsc.py
 create mode 100644 src/token_savior/compactors/docker.py
 create mode 100644 src/token_savior/compactors/gh.py
"""


def test_git_commit_single_line():
    result = compact("git commit -m 'foo'", GIT_COMMIT_OUTPUT)
    assert result is not None
    assert "a1b2c3d" in result.text
    assert "8 files" in result.text or "8 file" in result.text
    assert result.savings_pct >= 50.0


# ---------------------------------------------------------------------------
# pytest
# ---------------------------------------------------------------------------


PYTEST_OUTPUT = """============================= test session starts ==============================
platform linux -- Python 3.12.3, pytest-8.3.2, pluggy-1.5.0
rootdir: /root/ts-f1
collected 120 items

tests/test_a.py ....................                                     [ 16%]
tests/test_b.py ....................                                     [ 33%]
tests/test_c.py ............F.......                                     [ 50%]
tests/test_d.py ....................                                     [ 66%]
tests/test_e.py .........F..........                                     [ 83%]
tests/test_f.py ....................                                     [100%]

=================================== FAILURES ===================================
______________________________ test_thing_works ________________________________

    def test_thing_works():
        x = compute()
>       assert x == 42
E       assert 41 == 42

tests/test_c.py:33: AssertionError
_______________________________ test_other_case ________________________________

    def test_other_case():
        data = load()
>       result = transform(data)

tests/test_e.py:51:
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _

self = <Transformer obj at 0x7f>
data = None

    def transform(self, data):
>       return data.upper()
E       AttributeError: 'NoneType' object has no attribute 'upper'

src/transformer.py:18: AttributeError
=========================== short test summary info ============================
FAILED tests/test_c.py::test_thing_works - assert 41 == 42
FAILED tests/test_e.py::test_other_case - AttributeError: 'NoneType' object has no attribute 'upper'
================== 2 failed, 118 passed in 4.27s ===============================
"""


def test_pytest_failures_only():
    result = compact("pytest -v", PYTEST_OUTPUT)
    assert result is not None
    # Failure detail must survive
    assert "test_thing_works" in result.text
    assert "test_other_case" in result.text
    assert "assert 41 == 42" in result.text
    assert "AttributeError" in result.text
    # Summary line must survive
    assert "2 failed" in result.text and "118 passed" in result.text
    # Dot progress and platform header should be gone
    assert "platform linux" not in result.text
    assert "...................." not in result.text
    assert result.savings_pct >= 60.0


PYTEST_ALL_PASS = """============================= test session starts ==============================
platform linux -- Python 3.12.3, pytest-8.3.2, pluggy-1.5.0
rootdir: /root/ts-f1
collected 1478 items

tests/test_a.py ....................                                     [  1%]
tests/test_b.py ....................                                     [  2%]
""" + "tests/test_x.py ....................                                     [ 50%]\n" * 60 + """
============================ 1478 passed in 12.34s =============================
"""


def test_pytest_all_pass_reduces_to_summary():
    result = compact("pytest -q", PYTEST_ALL_PASS)
    assert result is not None
    assert "1478 passed" in result.text
    assert result.savings_pct >= 80.0


# ---------------------------------------------------------------------------
# cargo
# ---------------------------------------------------------------------------


CARGO_TEST_OUTPUT = """    Finished test [unoptimized + debuginfo] target(s) in 0.04s
     Running unittests src/lib.rs (target/debug/deps/myapp-abc123)

running 42 tests
test core::tests::test_basic ... ok
test core::tests::test_advanced ... ok
test core::tests::test_edge_case ... FAILED
test core::tests::test_normal ... ok
test util::tests::test_helper ... ok
test util::tests::test_format ... FAILED

failures:

---- core::tests::test_edge_case stdout ----
thread 'core::tests::test_edge_case' panicked at 'assertion failed: `(left == right)`
  left: `0`,
 right: `1`', src/core.rs:88:5

---- util::tests::test_format stdout ----
thread 'util::tests::test_format' panicked at 'expected formatting to succeed',
src/util.rs:45:9


failures:
    core::tests::test_edge_case
    util::tests::test_format

test result: FAILED. 40 passed; 2 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.12s
"""


def test_cargo_test_failures_only():
    result = compact("cargo test", CARGO_TEST_OUTPUT)
    assert result is not None
    assert "test_edge_case" in result.text
    assert "test_format" in result.text
    assert "panicked" in result.text
    assert "2 failed" in result.text or "FAILED" in result.text
    # Passing tests should be dropped
    assert "test_basic ... ok" not in result.text
    assert result.savings_pct >= 40.0


CARGO_BUILD_OUTPUT = """   Compiling proc-macro2 v1.0.86
   Compiling unicode-ident v1.0.13
   Compiling libc v0.2.158
   Compiling cfg-if v1.0.0
   Compiling syn v2.0.77
error[E0308]: mismatched types
  --> src/main.rs:12:18
   |
12 |     let x: i32 = "hello";
   |            ---   ^^^^^^^ expected `i32`, found `&str`
   |            |
   |            expected due to this

error[E0599]: no method named `foo` found for struct `Bar`
  --> src/lib.rs:33:7
   |
33 |     b.foo();
   |       ^^^ method not found

warning: unused variable: `y`
  --> src/lib.rs:40:9
   |
40 |     let y = 5;
   |         ^

error: aborting due to 2 previous errors; 1 warning emitted
"""


def test_cargo_build_errors_only():
    result = compact("cargo build", CARGO_BUILD_OUTPUT)
    assert result is not None
    assert "E0308" in result.text
    assert "E0599" in result.text
    # 'Compiling proc-macro2' noise should be gone
    assert "Compiling proc-macro2" not in result.text
    assert result.savings_pct >= 30.0


# ---------------------------------------------------------------------------
# tsc
# ---------------------------------------------------------------------------


TSC_OUTPUT = """src/app.ts(12,5): error TS2322: Type 'string' is not assignable to type 'number'.
src/app.ts(45,12): error TS2304: Cannot find name 'foo'.
src/lib/util.ts(8,3): error TS2345: Argument of type 'undefined' is not assignable.
src/lib/util.ts(22,9): error TS2322: Type 'null' is not assignable to type 'string'.
src/components/Button.tsx(3,10): error TS6133: 'unused' is declared but never read.
src/components/Button.tsx(15,5): error TS2741: Property 'onClick' is missing.

Found 6 errors in 3 files.

Errors  Files
     2  src/app.ts:12
     2  src/lib/util.ts:8
     2  src/components/Button.tsx:3
"""


def test_tsc_groups_errors_by_file():
    result = compact("tsc --noEmit", TSC_OUTPUT)
    assert result is not None
    assert "src/app.ts" in result.text
    assert "src/lib/util.ts" in result.text
    assert "src/components/Button.tsx" in result.text
    assert "TS2322" in result.text
    assert "TS2304" in result.text
    # Should be more compact than original — and the per-file summary
    # block ("Errors  Files") is redundant once we group.
    assert result.savings_pct >= 30.0


# ---------------------------------------------------------------------------
# docker
# ---------------------------------------------------------------------------


DOCKER_PS_OUTPUT = """CONTAINER ID   IMAGE                          COMMAND                  CREATED         STATUS          PORTS                                       NAMES
abc123def456   postgres:15-alpine             "docker-entrypoint.s..."  3 hours ago     Up 3 hours      0.0.0.0:5432->5432/tcp, :::5432->5432/tcp   ts-postgres
def456abc789   redis:7-alpine                 "docker-entrypoint.s..."  3 hours ago     Up 3 hours      0.0.0.0:6379->6379/tcp                       ts-redis
789abcdef012   nginx:latest                   "/docker-entrypoint...."  1 day ago       Up 1 day        0.0.0.0:80->80/tcp, 443/tcp                  ts-nginx
012def345abc   intel-api:latest               "uvicorn intel.main..."   2 days ago      Up 2 days       0.0.0.0:8001->8001/tcp                       intel-api
"""


def test_docker_ps_keeps_name_image_status():
    result = compact("docker ps", DOCKER_PS_OUTPUT)
    assert result is not None
    assert "ts-postgres" in result.text
    assert "postgres:15-alpine" in result.text
    assert "intel-api" in result.text
    # COMMAND/PORTS columns must be gone or shortened
    assert "docker-entrypoint.s..." not in result.text
    assert result.savings_pct >= 40.0


DOCKER_LOGS_OUTPUT = (
    "2026-05-19 10:00:00 INFO healthcheck ok\n" * 50
    + "2026-05-19 10:01:00 WARNING slow query 1.2s\n"
    + "2026-05-19 10:01:00 INFO healthcheck ok\n" * 30
    + "2026-05-19 10:02:00 ERROR db connection lost\n"
    + "2026-05-19 10:02:00 INFO healthcheck ok\n" * 20
)


def test_docker_logs_dedups_repeated_lines():
    result = compact("docker logs ts-postgres", DOCKER_LOGS_OUTPUT)
    assert result is not None
    # Dedup marker
    assert "healthcheck ok" in result.text
    # Distinctive lines preserved
    assert "slow query" in result.text
    assert "db connection lost" in result.text
    # Should mention counts (xN) somewhere
    assert "x" in result.text  # x50, x30, x20
    assert result.savings_pct >= 70.0


# ---------------------------------------------------------------------------
# gh
# ---------------------------------------------------------------------------


GH_RUN_LIST_OUTPUT = """STATUS  TITLE                                           WORKFLOW  BRANCH  EVENT  ID            ELAPSED  AGE
✓       fix(cli): remove unused 'io' import             CI        main    push   18923472389   2m13s    5m
✓       fix(site): hero-sub final v4 copy               CI        main    push   18923470012   2m05s    1h
X       docs(site): update landing for v4.0             CI        main    push   18923459001   1m45s    2h
✓       release: v4.0.0 — single 'optimized' profile    CI        main    push   18923400000   3m21s    1d
*       F1: bash output compactors                       CI        f1-bash push   18923499999   12s      now
"""


def test_gh_run_list_compaction():
    result = compact("gh run list", GH_RUN_LIST_OUTPUT)
    assert result is not None
    assert "18923472389" in result.text or "fix(cli)" in result.text
    # Failed run should be flagged
    assert "X" in result.text or "fail" in result.text.lower() or "docs(site)" in result.text
    assert result.savings_pct >= 20.0


GH_RUN_VIEW_OUTPUT = """
X main CI · 18923459001
Triggered via push about 2 hours ago

JOBS
✓ lint in 32s (ID 12345)
X test in 1m24s (ID 12346)
  ✓ Set up job
  ✓ Checkout
  ✓ Install Python
  ✓ Install deps
  X Run pytest
    pytest tests/ -q
    ============================= test session starts =====================
    collected 1478 items
    tests/test_x.py F
    FAILED tests/test_x.py::test_thing
  - Upload coverage
✓ build in 45s (ID 12347)

For more information about a job, try: gh run view --job=<job-id>
View this run on GitHub: https://github.com/Mibayy/token-savior/actions/runs/18923459001
"""


def test_gh_run_view_keeps_failures():
    result = compact("gh run view 18923459001", GH_RUN_VIEW_OUTPUT)
    assert result is not None
    assert "test_thing" in result.text or "Run pytest" in result.text
    # Successful sub-steps should be dropped
    assert "Set up job" not in result.text
    assert "Checkout" not in result.text
    assert result.savings_pct >= 30.0
