"""Tests for the regex-based C# annotator."""

from token_savior.annotator import annotate
from token_savior.csharp_annotator import annotate_csharp


class TestCSharpUsingDirectives:
    """Tests for detecting using directives."""

    def test_simple_using(self):
        src = "using System;\n"
        meta = annotate_csharp(src)
        assert len(meta.imports) == 1
        imp = meta.imports[0]
        assert imp.module == "System"
        assert imp.is_from_import is False
        assert imp.alias is None

    def test_qualified_using(self):
        src = "using System.Collections.Generic;\n"
        meta = annotate_csharp(src)
        assert len(meta.imports) == 1
        assert meta.imports[0].module == "System.Collections.Generic"

    def test_static_using(self):
        src = "using static System.Math;\n"
        meta = annotate_csharp(src)
        assert len(meta.imports) == 1
        imp = meta.imports[0]
        assert imp.module == "System.Math"
        assert imp.is_from_import is True
        assert imp.names == ["*"]

    def test_alias_using(self):
        src = "using Alias = MyNamespace.MyType;\n"
        meta = annotate_csharp(src)
        assert len(meta.imports) == 1
        imp = meta.imports[0]
        assert imp.module == "MyNamespace.MyType"
        assert imp.alias == "Alias"

    def test_global_using(self):
        src = "global using System.Linq;\n"
        meta = annotate_csharp(src)
        assert len(meta.imports) == 1
        assert meta.imports[0].module == "System.Linq"

    def test_multiple_usings(self):
        src = "using System;\nusing System.Collections.Generic;\nusing static System.Console;\n"
        meta = annotate_csharp(src)
        assert len(meta.imports) == 3


class TestCSharpClassDetection:
    """Tests for detecting class declarations."""

    def test_simple_class(self):
        src = "public class MyClass {\n}\n"
        meta = annotate_csharp(src)
        assert len(meta.classes) == 1
        cls = meta.classes[0]
        assert cls.name == "MyClass"
        assert cls.base_classes == []
        assert cls.line_range.start == 1
        assert cls.line_range.end == 2

    def test_class_with_base(self):
        src = "public class Derived : BaseClass {\n}\n"
        meta = annotate_csharp(src)
        assert len(meta.classes) == 1
        assert "BaseClass" in meta.classes[0].base_classes

    def test_class_with_interfaces(self):
        src = "public class MyClass : IDisposable, IComparable {\n}\n"
        meta = annotate_csharp(src)
        assert len(meta.classes) == 1
        assert "IDisposable" in meta.classes[0].base_classes
        assert "IComparable" in meta.classes[0].base_classes

    def test_abstract_class(self):
        src = "public abstract class Shape {\n}\n"
        meta = annotate_csharp(src)
        assert len(meta.classes) == 1
        assert meta.classes[0].name == "Shape"

    def test_sealed_class(self):
        src = "public sealed class Singleton {\n}\n"
        meta = annotate_csharp(src)
        assert len(meta.classes) == 1
        assert meta.classes[0].name == "Singleton"

    def test_static_class(self):
        src = "public static class Helpers {\n}\n"
        meta = annotate_csharp(src)
        assert len(meta.classes) == 1
        assert meta.classes[0].name == "Helpers"

    def test_partial_class(self):
        src = "public partial class Widget {\n}\n"
        meta = annotate_csharp(src)
        assert len(meta.classes) == 1
        assert meta.classes[0].name == "Widget"

    def test_generic_class(self):
        src = "public class Container<T> {\n}\n"
        meta = annotate_csharp(src)
        assert len(meta.classes) == 1
        assert meta.classes[0].name == "Container"

    def test_internal_class(self):
        src = "internal class Secret {\n}\n"
        meta = annotate_csharp(src)
        assert len(meta.classes) == 1
        assert meta.classes[0].name == "Secret"


class TestCSharpInterfaceDetection:
    """Tests for detecting interface declarations."""

    def test_simple_interface(self):
        src = "public interface IService {\n}\n"
        meta = annotate_csharp(src)
        assert len(meta.classes) == 1
        cls = meta.classes[0]
        assert cls.name == "IService"

    def test_interface_with_base(self):
        src = "public interface IAdvancedService : IService {\n}\n"
        meta = annotate_csharp(src)
        assert len(meta.classes) == 1
        assert "IService" in meta.classes[0].base_classes


class TestCSharpStructDetection:
    """Tests for detecting struct declarations."""

    def test_simple_struct(self):
        src = "public struct Point {\n    public int X;\n    public int Y;\n}\n"
        meta = annotate_csharp(src)
        assert len(meta.classes) == 1
        assert meta.classes[0].name == "Point"

    def test_readonly_struct(self):
        src = "public readonly struct Vector3 {\n}\n"
        meta = annotate_csharp(src)
        assert len(meta.classes) == 1
        assert meta.classes[0].name == "Vector3"

    def test_record_struct(self):
        src = "public record struct Coordinate(double Lat, double Lon);\n"
        meta = annotate_csharp(src)
        assert len(meta.classes) == 1
        assert meta.classes[0].name == "Coordinate"


class TestCSharpEnumDetection:
    """Tests for detecting enum declarations."""

    def test_simple_enum(self):
        src = "public enum Color {\n    Red,\n    Green,\n    Blue\n}\n"
        meta = annotate_csharp(src)
        assert len(meta.classes) == 1
        cls = meta.classes[0]
        assert cls.name == "Color"
        assert cls.methods == []  # enums have no methods extracted


class TestCSharpRecordDetection:
    """Tests for detecting record declarations."""

    def test_positional_record(self):
        src = "public record Person(string Name, int Age);\n"
        meta = annotate_csharp(src)
        assert len(meta.classes) == 1
        assert meta.classes[0].name == "Person"

    def test_record_class(self):
        src = "public record class Employee(string Name) {\n    public int Id { get; init; }\n}\n"
        meta = annotate_csharp(src)
        assert len(meta.classes) == 1
        assert meta.classes[0].name == "Employee"


class TestCSharpMethodDetection:
    """Tests for detecting method declarations."""

    def test_void_method(self):
        src = "public class MyClass {\n    public void DoWork() {\n    }\n}\n"
        meta = annotate_csharp(src)
        assert len(meta.functions) == 1
        f = meta.functions[0]
        assert f.name == "DoWork"
        assert f.is_method is True
        assert f.parent_class == "MyClass"
        assert f.parameters == []

    def test_return_type_method(self):
        src = (
            "public class Calc {\n"
            "    public int Add(int a, int b) {\n"
            "        return a + b;\n"
            "    }\n"
            "}\n"
        )
        meta = annotate_csharp(src)
        assert len(meta.functions) == 1
        f = meta.functions[0]
        assert f.name == "Add"
        assert "a" in f.parameters
        assert "b" in f.parameters

    def test_static_method(self):
        src = (
            "public class Utils {\n"
            "    public static string Format(string s) {\n"
            "        return s;\n"
            "    }\n"
            "}\n"
        )
        meta = annotate_csharp(src)
        assert len(meta.functions) == 1
        assert meta.functions[0].name == "Format"
        assert "s" in meta.functions[0].parameters

    def test_async_method(self):
        src = (
            "public class Service {\n"
            "    public async Task<string> FetchAsync(string url) {\n"
            "        return await http.GetStringAsync(url);\n"
            "    }\n"
            "}\n"
        )
        meta = annotate_csharp(src)
        assert len(meta.functions) == 1
        assert meta.functions[0].name == "FetchAsync"
        assert "url" in meta.functions[0].parameters

    def test_virtual_method(self):
        src = "public class Base {\n    public virtual void Run() {\n    }\n}\n"
        meta = annotate_csharp(src)
        assert len(meta.functions) == 1
        assert meta.functions[0].name == "Run"

    def test_override_method(self):
        src = "public class Derived : Base {\n    public override void Run() {\n    }\n}\n"
        meta = annotate_csharp(src)
        assert len(meta.functions) == 1
        assert meta.functions[0].name == "Run"

    def test_abstract_method_no_body(self):
        src = "public abstract class Shape {\n    public abstract double Area();\n}\n"
        meta = annotate_csharp(src)
        assert len(meta.functions) == 1
        assert meta.functions[0].name == "Area"

    def test_expression_bodied_method(self):
        src = "public class Circle {\n    public double Area() => Math.PI * r * r;\n}\n"
        meta = annotate_csharp(src)
        assert len(meta.functions) == 1
        assert meta.functions[0].name == "Area"

    def test_constructor(self):
        src = (
            "public class MyClass {\n"
            "    public MyClass(int value) {\n"
            "        Value = value;\n"
            "    }\n"
            "}\n"
        )
        meta = annotate_csharp(src)
        assert len(meta.functions) == 1
        f = meta.functions[0]
        assert f.name == "MyClass"
        assert f.qualified_name == "MyClass.MyClass"
        assert "value" in f.parameters

    def test_generic_method(self):
        src = (
            "public class Utils {\n"
            "    public T Parse<T>(string input) {\n"
            "        return default;\n"
            "    }\n"
            "}\n"
        )
        meta = annotate_csharp(src)
        assert len(meta.functions) == 1
        assert meta.functions[0].name == "Parse"
        assert "input" in meta.functions[0].parameters

    def test_qualified_name(self):
        src = "public class Foo {\n    public void Bar() {\n    }\n}\n"
        meta = annotate_csharp(src)
        assert meta.functions[0].qualified_name == "Foo.Bar"

    def test_parameter_extraction_modifiers(self):
        src = (
            "public class MyClass {\n"
            "    public void Process(ref int x, out string y, params object[] args) {\n"
            "    }\n"
            "}\n"
        )
        meta = annotate_csharp(src)
        assert len(meta.functions) == 1
        params = meta.functions[0].parameters
        assert "x" in params
        assert "y" in params
        assert "args" in params


class TestCSharpAttributes:
    """Tests for detecting [Attribute] decorators."""

    def test_single_attribute(self):
        src = (
            "public class MyClass {\n"
            "    [HttpGet]\n"
            "    public string GetValue() {\n"
            '        return "";\n'
            "    }\n"
            "}\n"
        )
        meta = annotate_csharp(src)
        assert len(meta.functions) == 1
        assert "HttpGet" in meta.functions[0].decorators

    def test_attribute_with_args(self):
        src = (
            "public class MyClass {\n"
            '    [Route("api/values")]\n'
            "    public string GetAll() {\n"
            '        return "";\n'
            "    }\n"
            "}\n"
        )
        meta = annotate_csharp(src)
        assert len(meta.functions) == 1
        assert "Route" in meta.functions[0].decorators

    def test_stacked_attributes(self):
        src = "[Serializable]\n[Obsolete]\npublic class Legacy {\n}\n"
        meta = annotate_csharp(src)
        assert len(meta.classes) == 1
        assert "Serializable" in meta.classes[0].decorators
        assert "Obsolete" in meta.classes[0].decorators


class TestCSharpDocComments:
    """Tests for detecting /// XML doc comments."""

    def test_single_line_doc(self):
        src = (
            "public class MyClass {\n"
            "    /// <summary>Does work.</summary>\n"
            "    public void DoWork() {\n"
            "    }\n"
            "}\n"
        )
        meta = annotate_csharp(src)
        assert meta.functions[0].docstring is not None
        assert "Does work" in meta.functions[0].docstring

    def test_multiline_doc(self):
        src = "/// <summary>\n/// Represents a person.\n/// </summary>\npublic class Person {\n}\n"
        meta = annotate_csharp(src)
        assert meta.classes[0].docstring is not None
        assert "Represents a person" in meta.classes[0].docstring


class TestCSharpNamespace:
    """Tests for namespace handling."""

    def test_file_scoped_namespace(self):
        src = "namespace MyApp.Models;\n\npublic class User {\n}\n"
        meta = annotate_csharp(src)
        assert len(meta.classes) == 1
        assert meta.classes[0].name == "User"

    def test_block_namespace(self):
        src = "namespace MyApp.Models {\n    public class User {\n    }\n}\n"
        meta = annotate_csharp(src)
        assert len(meta.classes) == 1
        assert meta.classes[0].name == "User"

    def test_namespace_not_in_classes(self):
        src = "namespace MyApp;\n\npublic class Foo {\n}\n"
        meta = annotate_csharp(src)
        # Namespace should NOT appear as a class
        assert all(cls.name != "MyApp" for cls in meta.classes)
        assert len(meta.classes) == 1
        assert meta.classes[0].name == "Foo"


class TestCSharpComplexFile:
    """Test a realistic multi-class file with usings, namespace, classes, methods, attributes."""

    def test_complex_file(self):
        src = (
            "using System;\n"
            "using System.Collections.Generic;\n"
            "using static System.Console;\n"
            "\n"
            "namespace MyApp.Services;\n"
            "\n"
            "/// <summary>\n"
            "/// Service interface.\n"
            "/// </summary>\n"
            "public interface IUserService {\n"
            "    Task<User> GetUserAsync(int id);\n"
            "}\n"
            "\n"
            "[Serializable]\n"
            "public class UserService : IUserService {\n"
            "    private readonly ILogger _logger;\n"
            "\n"
            "    public UserService(ILogger logger) {\n"
            "        _logger = logger;\n"
            "    }\n"
            "\n"
            "    /// <summary>Gets a user by ID.</summary>\n"
            "    [HttpGet]\n"
            "    public async Task<User> GetUserAsync(int id) {\n"
            "        return await _repo.FindAsync(id);\n"
            "    }\n"
            "\n"
            "    public void Delete(int id) {\n"
            "        _repo.Remove(id);\n"
            "    }\n"
            "}\n"
            "\n"
            "public enum Status {\n"
            "    Active,\n"
            "    Inactive\n"
            "}\n"
        )
        meta = annotate_csharp(src)

        # 3 usings
        assert len(meta.imports) == 3

        # 3 types: IUserService, UserService, Status
        assert len(meta.classes) == 3
        names = [c.name for c in meta.classes]
        assert "IUserService" in names
        assert "UserService" in names
        assert "Status" in names

        # UserService has Serializable attribute
        us = next(c for c in meta.classes if c.name == "UserService")
        assert "Serializable" in us.decorators
        assert "IUserService" in us.base_classes

        # IUserService has doc comment
        iface = next(c for c in meta.classes if c.name == "IUserService")
        assert iface.docstring is not None

        # Methods: GetUserAsync (in IUserService), UserService constructor,
        # GetUserAsync (in UserService), Delete
        method_names = [f.name for f in meta.functions]
        assert "GetUserAsync" in method_names
        assert "UserService" in method_names  # constructor
        assert "Delete" in method_names

        # GetUserAsync in UserService has HttpGet attribute
        get_user = [
            f
            for f in meta.functions
            if f.name == "GetUserAsync" and f.parent_class == "UserService"
        ]
        assert len(get_user) == 1
        assert "HttpGet" in get_user[0].decorators
        assert get_user[0].docstring is not None


class TestCSharpAnnotatorDispatch:
    """Verify .cs extension routes correctly through the dispatch layer."""

    def test_cs_dispatch(self):
        src = "public class Hello {\n}\n"
        meta = annotate(src, source_name="Hello.cs")
        assert len(meta.classes) == 1
        assert meta.classes[0].name == "Hello"

    def test_cs_dispatch_case_insensitive(self):
        src = "public class Hello {\n}\n"
        meta = annotate(src, source_name="Hello.CS")
        assert len(meta.classes) == 1
        assert meta.classes[0].name == "Hello"
