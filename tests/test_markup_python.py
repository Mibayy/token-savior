"""Tests for the Python file annotator."""

from token_savior.python_annotator import annotate_python
from token_savior.models import (
    StructuralMetadata,
)


# ---------------------------------------------------------------------------
# Test source strings
# ---------------------------------------------------------------------------

SOURCE_CLASSES_DECORATORS_IMPORTS = '''\
import os
import sys as system
from collections import OrderedDict, defaultdict
from typing import Optional

def helper(x, y):
    """A helper function."""
    return x + y

class Animal:
    """Base animal class."""

    def __init__(self, name: str):
        self.name = name

    def speak(self) -> str:
        return ""

class Dog(Animal):
    """A dog that can speak."""

    @staticmethod
    def species():
        return "Canis lupus familiaris"

    def speak(self) -> str:
        return f"{self.name} says Woof!"

    def fetch(self, item):
        """Fetch an item."""
        result = helper(1, 2)
        return f"Fetching {item}"

def process(animals):
    """Process a list of animals."""
    for a in animals:
        a.speak()
    dog = Dog("Rex")
    dog.fetch("ball")
'''

SOURCE_NESTED_FUNCTIONS = '''\
def outer(x):
    """Outer function with nested functions."""
    def middle(y):
        def inner(z):
            return x + y + z
        return inner(1)
    return middle(2)

def another():
    pass
'''

SOURCE_ASYNC_FUNCTIONS = '''\
import asyncio
from aiohttp import ClientSession

async def fetch_url(url: str) -> str:
    """Fetch a URL asynchronously."""
    async with ClientSession() as session:
        async with session.get(url) as response:
            return await response.text()

async def fetch_all(urls: list[str]) -> list[str]:
    """Fetch multiple URLs concurrently."""
    tasks = [fetch_url(u) for u in urls]
    return await asyncio.gather(*tasks)

class AsyncService:
    """An async service."""

    async def start(self):
        await self.setup()

    async def setup(self):
        pass

    def sync_method(self):
        return "sync"
'''

SOURCE_SYNTAX_ERROR = """\
def valid_func():
    pass

class Broken
    pass

def another():
    pass
"""

SOURCE_DECORATORS_COMPLEX = '''\
from functools import wraps

def my_decorator(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        return func(*args, **kwargs)
    return wrapper

@my_decorator
def decorated_func(a, b, c=10):
    """A decorated function."""
    return a + b + c

class MyClass:
    @staticmethod
    def static_method():
        pass

    @classmethod
    def class_method(cls, x):
        pass

    @my_decorator
    def instance_method(self, y, z):
        """An instance method."""
        return decorated_func(y, z)
'''

SOURCE_STAR_IMPORT_AND_ALIAS = """\
from os.path import join, exists
import numpy as np
from . import utils
"""

SOURCE_EMPTY = ""

SOURCE_MINIMAL = """\
x = 1
y = 2
"""


# ---------------------------------------------------------------------------
# Tests: classes, decorators, imports
# ---------------------------------------------------------------------------


class TestClassesDecoratorsImports:
    def test_imports_extracted(self):
        meta = annotate_python(SOURCE_CLASSES_DECORATORS_IMPORTS, "test.py")
        assert len(meta.imports) == 4
        # import os
        imp_os = meta.imports[0]
        assert imp_os.module == "os"
        assert imp_os.is_from_import is False
        assert imp_os.alias is None
        # import sys as system
        imp_sys = meta.imports[1]
        assert imp_sys.module == "sys"
        assert imp_sys.alias == "system"
        # from collections import ...
        imp_coll = meta.imports[2]
        assert imp_coll.module == "collections"
        assert imp_coll.is_from_import is True
        assert "OrderedDict" in imp_coll.names
        assert "defaultdict" in imp_coll.names
        # from typing import Optional
        imp_typing = meta.imports[3]
        assert imp_typing.module == "typing"
        assert "Optional" in imp_typing.names

    def test_top_level_functions(self):
        meta = annotate_python(SOURCE_CLASSES_DECORATORS_IMPORTS, "test.py")
        top_level = [f for f in meta.functions if not f.is_method]
        names = [f.name for f in top_level]
        assert "helper" in names
        assert "process" in names

    def test_helper_function_details(self):
        meta = annotate_python(SOURCE_CLASSES_DECORATORS_IMPORTS, "test.py")
        helper = next(f for f in meta.functions if f.name == "helper")
        assert helper.qualified_name == "helper"
        assert helper.parameters == ["x", "y"]
        assert helper.docstring == "A helper function."
        assert helper.is_method is False
        assert helper.parent_class is None
        assert helper.line_range.start == 6
        assert helper.line_range.end == 8

    def test_classes_extracted(self):
        meta = annotate_python(SOURCE_CLASSES_DECORATORS_IMPORTS, "test.py")
        assert len(meta.classes) == 2
        class_names = [c.name for c in meta.classes]
        assert "Animal" in class_names
        assert "Dog" in class_names

    def test_class_base_classes(self):
        meta = annotate_python(SOURCE_CLASSES_DECORATORS_IMPORTS, "test.py")
        animal = next(c for c in meta.classes if c.name == "Animal")
        assert animal.base_classes == []
        dog = next(c for c in meta.classes if c.name == "Dog")
        assert dog.base_classes == ["Animal"]

    def test_class_methods(self):
        meta = annotate_python(SOURCE_CLASSES_DECORATORS_IMPORTS, "test.py")
        dog = next(c for c in meta.classes if c.name == "Dog")
        method_names = [m.name for m in dog.methods]
        assert "species" in method_names
        assert "speak" in method_names
        assert "fetch" in method_names

    def test_method_qualified_name(self):
        meta = annotate_python(SOURCE_CLASSES_DECORATORS_IMPORTS, "test.py")
        fetch = next(f for f in meta.functions if f.qualified_name == "Dog.fetch")
        assert fetch.is_method is True
        assert fetch.parent_class == "Dog"

    def test_method_skips_self(self):
        meta = annotate_python(SOURCE_CLASSES_DECORATORS_IMPORTS, "test.py")
        fetch = next(f for f in meta.functions if f.qualified_name == "Dog.fetch")
        assert "self" not in fetch.parameters
        assert "item" in fetch.parameters

    def test_class_docstring(self):
        meta = annotate_python(SOURCE_CLASSES_DECORATORS_IMPORTS, "test.py")
        dog = next(c for c in meta.classes if c.name == "Dog")
        assert dog.docstring == "A dog that can speak."

    def test_staticmethod_decorator(self):
        meta = annotate_python(SOURCE_CLASSES_DECORATORS_IMPORTS, "test.py")
        species = next(f for f in meta.functions if f.name == "species")
        assert "staticmethod" in species.decorators

    def test_line_ranges_are_1_indexed(self):
        meta = annotate_python(SOURCE_CLASSES_DECORATORS_IMPORTS, "test.py")
        assert meta.total_lines > 0
        # First import is on line 1
        assert meta.imports[0].line_number == 1
        # helper starts at line 6
        helper = next(f for f in meta.functions if f.name == "helper")
        assert helper.line_range.start == 6

    def test_dependency_graph(self):
        meta = annotate_python(SOURCE_CLASSES_DECORATORS_IMPORTS, "test.py")
        # 'process' references Dog
        assert "Dog" in meta.dependency_graph.get("process", [])
        # 'Dog' class references Animal (base class) and helper
        assert "Animal" in meta.dependency_graph.get("Dog", [])


# ---------------------------------------------------------------------------
# Tests: nested functions
# ---------------------------------------------------------------------------


class TestNestedFunctions:
    def test_top_level_functions_only_at_module_level(self):
        meta = annotate_python(SOURCE_NESTED_FUNCTIONS, "nested.py")
        top_level = [f for f in meta.functions if not f.is_method]
        names = [f.name for f in top_level]
        # Only top-level functions should appear at module level
        assert "outer" in names
        assert "another" in names
        # middle and inner are nested, not top-level module functions
        # (they won't be extracted by iter_child_nodes on the module)

    def test_outer_function_line_range(self):
        meta = annotate_python(SOURCE_NESTED_FUNCTIONS, "nested.py")
        outer = next(f for f in meta.functions if f.name == "outer")
        assert outer.line_range.start == 1
        assert outer.line_range.end == 7

    def test_outer_has_docstring(self):
        meta = annotate_python(SOURCE_NESTED_FUNCTIONS, "nested.py")
        outer = next(f for f in meta.functions if f.name == "outer")
        assert outer.docstring == "Outer function with nested functions."


# ---------------------------------------------------------------------------
# Tests: async functions
# ---------------------------------------------------------------------------


class TestAsyncFunctions:
    def test_async_functions_detected(self):
        meta = annotate_python(SOURCE_ASYNC_FUNCTIONS, "async_mod.py")
        func_names = [f.name for f in meta.functions]
        assert "fetch_url" in func_names
        assert "fetch_all" in func_names

    def test_async_method_detected(self):
        meta = annotate_python(SOURCE_ASYNC_FUNCTIONS, "async_mod.py")
        start = next(f for f in meta.functions if f.qualified_name == "AsyncService.start")
        assert start.is_method is True
        assert start.parent_class == "AsyncService"

    def test_async_class_methods(self):
        meta = annotate_python(SOURCE_ASYNC_FUNCTIONS, "async_mod.py")
        svc = next(c for c in meta.classes if c.name == "AsyncService")
        method_names = [m.name for m in svc.methods]
        assert "start" in method_names
        assert "setup" in method_names
        assert "sync_method" in method_names

    def test_async_function_params(self):
        meta = annotate_python(SOURCE_ASYNC_FUNCTIONS, "async_mod.py")
        fetch_url = next(f for f in meta.functions if f.name == "fetch_url")
        assert fetch_url.parameters == ["url"]

    def test_async_imports(self):
        meta = annotate_python(SOURCE_ASYNC_FUNCTIONS, "async_mod.py")
        modules = [i.module for i in meta.imports]
        assert "asyncio" in modules
        assert "aiohttp" in modules


# ---------------------------------------------------------------------------
# Tests: syntax errors (graceful fallback)
# ---------------------------------------------------------------------------


class TestSyntaxErrorFallback:
    def test_syntax_error_returns_metadata(self):
        meta = annotate_python(SOURCE_SYNTAX_ERROR, "broken.py")
        assert isinstance(meta, StructuralMetadata)
        assert meta.source_name == "broken.py"
        assert meta.total_lines > 0

    def test_syntax_error_has_empty_structures(self):
        meta = annotate_python(SOURCE_SYNTAX_ERROR, "broken.py")
        assert meta.functions == []
        assert meta.classes == []
        assert meta.imports == []
        assert meta.dependency_graph == {}

    def test_syntax_error_has_lines(self):
        meta = annotate_python(SOURCE_SYNTAX_ERROR, "broken.py")
        assert len(meta.lines) == meta.total_lines
        assert "def valid_func():" in meta.lines[0]


# ---------------------------------------------------------------------------
# Tests: complex decorators and classmethod/staticmethod
# ---------------------------------------------------------------------------


class TestComplexDecorators:
    def test_classmethod_skips_cls(self):
        meta = annotate_python(SOURCE_DECORATORS_COMPLEX, "decorators.py")
        cm = next(f for f in meta.functions if f.qualified_name == "MyClass.class_method")
        assert "cls" not in cm.parameters
        assert "x" in cm.parameters
        assert "classmethod" in cm.decorators

    def test_decorated_function_params(self):
        meta = annotate_python(SOURCE_DECORATORS_COMPLEX, "decorators.py")
        df = next(f for f in meta.functions if f.name == "decorated_func")
        assert df.parameters == ["a", "b", "c"]
        assert "my_decorator" in df.decorators

    def test_decorator_line_range_includes_decorator(self):
        meta = annotate_python(SOURCE_DECORATORS_COMPLEX, "decorators.py")
        df = next(f for f in meta.functions if f.name == "decorated_func")
        # @my_decorator is on line 9, def is on line 10
        assert df.line_range.start == 9

    def test_dependency_graph_cross_reference(self):
        meta = annotate_python(SOURCE_DECORATORS_COMPLEX, "decorators.py")
        # MyClass.instance_method calls decorated_func
        assert "decorated_func" in meta.dependency_graph.get("MyClass", [])


# ---------------------------------------------------------------------------
# Tests: edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_source(self):
        meta = annotate_python(SOURCE_EMPTY, "empty.py")
        # source.split("\n", "") returns [""] -- one empty line. This matches
        # the convention used by every other annotator (see go/java/rust/...).
        assert meta.total_lines == 1
        assert meta.total_chars == 0
        assert meta.functions == []
        assert meta.classes == []

    def test_no_functions_or_classes(self):
        meta = annotate_python(SOURCE_MINIMAL, "minimal.py")
        assert meta.functions == []
        assert meta.classes == []
        # "x = 1\ny = 2\n".split("\n") -> ["x = 1", "y = 2", ""] -- the
        # trailing empty is the convention shared with all other annotators.
        assert meta.total_lines == 3

    def test_import_aliases(self):
        meta = annotate_python(SOURCE_STAR_IMPORT_AND_ALIAS, "imports.py")
        np_import = next(i for i in meta.imports if i.module == "numpy")
        assert np_import.alias == "np"
        assert np_import.is_from_import is False

    def test_from_import_multiple_names(self):
        meta = annotate_python(SOURCE_STAR_IMPORT_AND_ALIAS, "imports.py")
        ospath = next(i for i in meta.imports if i.module == "os.path")
        assert "join" in ospath.names
        assert "exists" in ospath.names
        assert ospath.is_from_import is True

    def test_relative_import(self):
        meta = annotate_python(SOURCE_STAR_IMPORT_AND_ALIAS, "imports.py")
        rel = next(i for i in meta.imports if "utils" in i.names)
        assert rel.is_from_import is True

    def test_line_char_offsets(self):
        source = "line1\nline2\nline3\n"
        meta = annotate_python(source, "offsets.py")
        assert meta.line_char_offsets[0] == 0
        assert meta.line_char_offsets[1] == 6  # "line1\n" = 6 chars
        assert meta.line_char_offsets[2] == 12

    def test_source_name_preserved(self):
        meta = annotate_python("x = 1", "my_module.py")
        assert meta.source_name == "my_module.py"

    def test_total_chars(self):
        source = "hello\nworld"
        meta = annotate_python(source, "chars.py")
        assert meta.total_chars == len(source)
