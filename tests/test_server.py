"""Tests for server-side query wrappers."""

from token_savior.server import _q_get_edit_context

_EXTRA_QFNS = {
    "get_functions": lambda file_path=None, max_results=0: [],
    "get_classes": lambda file_path=None, max_results=0: [],
    "find_impacted_test_files": lambda symbol_names=None, changed_files=None, max_tests=5: {
        "impacted_tests": [],
    },
}


def test_get_edit_context_prefers_full_class_source_and_filters_private_constructor():
    qfns = {
        "find_symbol": lambda name: {
            "name": "com.acme.AssetKeyRegistry",
            "type": "class",
            "file": "src/main/java/com/acme/AssetKeyRegistry.java",
            "line": 1,
        },
        "get_function_source": lambda name, max_lines=200: "constructor only",
        "get_class_source": lambda name, max_lines=200: "full class body",
        "get_dependencies": lambda name, max_results=10: [
            {
                "name": "com.acme.AssetKeyRegistry.AssetKeyRegistry()",
                "type": "method",
            },
            {
                "name": "com.acme.AssetRegistry.lookup()",
                "type": "method",
            },
        ],
        "get_dependents": lambda name, max_results=10: [],
        **_EXTRA_QFNS,
    }

    result = _q_get_edit_context(qfns, {"name": "AssetKeyRegistry"})

    assert result["source"] == "full class body"
    assert result["dependencies"] == [
        {
            "name": "com.acme.AssetRegistry.lookup()",
            "type": "method",
        }
    ]


def test_get_edit_context_filters_private_constructor_without_type_field():
    qfns = {
        "find_symbol": lambda name: {
            "name": "com.acme.AssetKeyRegistry",
            "type": "class",
            "file": "src/main/java/com/acme/AssetKeyRegistry.java",
            "line": 1,
        },
        "get_function_source": lambda name, max_lines=200: "constructor only",
        "get_class_source": lambda name, max_lines=200: "full class body",
        "get_dependencies": lambda name, max_results=10: [
            {"name": "com.acme.AssetKeyRegistry.AssetKeyRegistry()"},
            {"name": "com.acme.AssetRegistry.lookup()"},
        ],
        "get_dependents": lambda name, max_results=10: [],
        **_EXTRA_QFNS,
    }

    result = _q_get_edit_context(qfns, {"name": "AssetKeyRegistry"})

    assert result["dependencies"] == [{"name": "com.acme.AssetRegistry.lookup()"}]


def test_get_edit_context_includes_siblings_and_impacted_tests():
    qfns = {
        "find_symbol": lambda name: {
            "name": "slugify",
            "type": "function",
            "file": "apps/api/utils/strings.py",
            "line": 10,
        },
        "get_function_source": lambda name, max_lines=200: "def slugify(s): ...",
        "get_class_source": lambda name, max_lines=200: "class Foo: ...",
        "get_dependencies": lambda name, max_results=10: [],
        "get_dependents": lambda name, max_results=10: [
            {"name": "create_article", "file": "apps/api/views.py", "line": 5},
        ],
        "get_functions": lambda file_path=None, max_results=0: [
            {"name": "slugify", "line": 10},
            {"name": "truncate", "line": 25},
            {"name": "sanitize", "line": 40},
        ],
        "get_classes": lambda file_path=None, max_results=0: [
            {"name": "StringHelper", "line": 55},
        ],
        "find_impacted_test_files": lambda symbol_names=None, changed_files=None, max_tests=5: {
            "impacted_tests": ["tests/test_strings.py"],
        },
    }

    result = _q_get_edit_context(qfns, {"name": "slugify"})

    assert result["symbol"] == "slugify"
    assert result["source"] == "def slugify(s): ..."
    assert result["callers"] == [
        {"name": "create_article", "file": "apps/api/views.py", "line": 5},
    ]
    # siblings should exclude the symbol itself
    sibling_names = [s["name"] for s in result["siblings"]]
    assert "truncate" in sibling_names
    assert "sanitize" in sibling_names
    assert "StringHelper" in sibling_names
    assert "slugify" not in sibling_names
    # impacted tests
    assert result["impacted_tests"] == ["tests/test_strings.py"]
