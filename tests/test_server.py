"""Tests for server-side query wrappers."""

from token_savior.server import _q_get_edit_context


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
    }

    result = _q_get_edit_context(qfns, {"name": "AssetKeyRegistry"})

    assert result["dependencies"] == [{"name": "com.acme.AssetRegistry.lookup()"}]
