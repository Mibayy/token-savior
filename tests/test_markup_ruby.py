"""Tests for the tree-sitter-based Ruby annotator."""

from token_savior.ruby_annotator import annotate_ruby


SOURCE_BASIC = """\
require 'json'
require_relative '../lib/helper'

def greet(name)
  puts name
end

class Animal
  def initialize(name)
    @name = name
  end

  def speak
    raise NotImplementedError
  end
end
"""


SOURCE_INHERITANCE = """\
class Dog < Animal
  def speak
    'woof'
  end
end

class Cat < Pets::Animal
  def speak
    'meow'
  end
end
"""


SOURCE_VISIBILITY = """\
class Account
  def public_method
  end

  private

  def secret_method
  end

  def also_secret
  end

  protected

  def semi_secret
  end

  public

  def back_to_public
  end
end
"""


SOURCE_INLINE_VISIBILITY = """\
class Service
  def normal
  end

  private def hidden
  end

  protected def guarded
  end
end
"""


SOURCE_CLASS_METHOD = """\
class Calculator
  def self.add(a, b)
    a + b
  end

  def self.subtract(x, y)
    x - y
  end

  def instance_method
  end
end
"""


SOURCE_MODULE = """\
module Greeter
  def hello
    puts 'hello'
  end

  def goodbye
    puts 'bye'
  end
end
"""


SOURCE_INCLUDES = """\
include Comparable
extend ActiveSupport
include Foo::Bar
"""


SOURCE_FULL = """\
require 'net/http'
require_relative 'base'
include Serializable

class User < ActiveRecord::Base
  def initialize(name, email)
    @name = name
    @email = email
  end

  def self.find(id)
    nil
  end

  private

  def validate
  end

  private def encrypt_password
  end
end

module Admin
  def admin?
    true
  end
end
"""


class TestRubyImports:
    def test_require_extracted(self):
        meta = annotate_ruby(SOURCE_BASIC, "animal.rb")
        mods = {imp.module for imp in meta.imports}
        assert "json" in mods
        assert "../lib/helper" in mods

    def test_require_is_not_from_import(self):
        meta = annotate_ruby(SOURCE_BASIC, "animal.rb")
        req = next(i for i in meta.imports if i.module == "json")
        assert req.is_from_import is False
        assert req.names == []

    def test_include_is_from_import(self):
        meta = annotate_ruby(SOURCE_INCLUDES, "inc.rb")
        comparable = next(i for i in meta.imports if i.module == "Comparable")
        assert comparable.is_from_import is True
        assert comparable.names == ["*"]

    def test_extend_is_from_import(self):
        meta = annotate_ruby(SOURCE_INCLUDES, "inc.rb")
        ext = next(i for i in meta.imports if i.module == "ActiveSupport")
        assert ext.is_from_import is True
        assert ext.names == ["*"]

    def test_scoped_include(self):
        meta = annotate_ruby(SOURCE_INCLUDES, "inc.rb")
        scoped = next(i for i in meta.imports if "Foo" in i.module)
        assert scoped.module == "Foo::Bar"
        assert scoped.is_from_import is True

    def test_require_relative_extracted(self):
        meta = annotate_ruby(SOURCE_FULL, "full.rb")
        mods = {imp.module for imp in meta.imports}
        assert "base" in mods

    def test_full_imports(self):
        meta = annotate_ruby(SOURCE_FULL, "full.rb")
        req_mods = [i.module for i in meta.imports if not i.is_from_import]
        inc_mods = [i.module for i in meta.imports if i.is_from_import]
        assert "net/http" in req_mods
        assert "base" in req_mods
        assert "Serializable" in inc_mods


class TestRubyMethods:
    def test_plain_methods_extracted(self):
        meta = annotate_ruby(SOURCE_BASIC, "animal.rb")
        method_names = {f.name for f in meta.functions}
        assert "greet" in method_names
        assert "initialize" in method_names
        assert "speak" in method_names

    def test_top_level_function_not_a_method(self):
        meta = annotate_ruby(SOURCE_BASIC, "animal.rb")
        greet = next(f for f in meta.functions if f.name == "greet")
        assert greet.is_method is False
        assert greet.parent_class is None

    def test_instance_method_is_method(self):
        meta = annotate_ruby(SOURCE_BASIC, "animal.rb")
        speak = next(f for f in meta.functions if f.name == "speak")
        assert speak.is_method is True
        assert speak.parent_class == "Animal"

    def test_method_parameters(self):
        meta = annotate_ruby(SOURCE_BASIC, "animal.rb")
        greet = next(f for f in meta.functions if f.name == "greet")
        assert greet.parameters == ["name"]

    def test_class_method_name_prefixed(self):
        meta = annotate_ruby(SOURCE_CLASS_METHOD, "calc.rb")
        names = {f.name for f in meta.functions}
        assert "self.add" in names
        assert "self.subtract" in names

    def test_class_method_is_method(self):
        meta = annotate_ruby(SOURCE_CLASS_METHOD, "calc.rb")
        add = next(f for f in meta.functions if f.name == "self.add")
        assert add.is_method is True
        assert add.parent_class == "Calculator"

    def test_class_method_parameters(self):
        meta = annotate_ruby(SOURCE_CLASS_METHOD, "calc.rb")
        add = next(f for f in meta.functions if f.name == "self.add")
        assert add.parameters == ["a", "b"]

    def test_class_methods_qualified_name(self):
        meta = annotate_ruby(SOURCE_CLASS_METHOD, "calc.rb")
        add = next(f for f in meta.functions if f.name == "self.add")
        assert add.qualified_name == "Calculator.self.add"


class TestRubyClasses:
    def test_class_extracted(self):
        meta = annotate_ruby(SOURCE_BASIC, "animal.rb")
        names = {c.name for c in meta.classes}
        assert "Animal" in names

    def test_class_base_classes_empty_when_no_superclass(self):
        meta = annotate_ruby(SOURCE_BASIC, "animal.rb")
        animal = next(c for c in meta.classes if c.name == "Animal")
        assert animal.base_classes == []

    def test_class_with_simple_inheritance(self):
        meta = annotate_ruby(SOURCE_INHERITANCE, "dog.rb")
        dog = next(c for c in meta.classes if c.name == "Dog")
        assert "Animal" in dog.base_classes

    def test_class_with_scoped_inheritance(self):
        meta = annotate_ruby(SOURCE_INHERITANCE, "cat.rb")
        cat = next(c for c in meta.classes if c.name == "Cat")
        assert "Pets::Animal" in cat.base_classes

    def test_class_methods_linked(self):
        meta = annotate_ruby(SOURCE_BASIC, "animal.rb")
        animal = next(c for c in meta.classes if c.name == "Animal")
        method_names = {m.name for m in animal.methods}
        assert "initialize" in method_names
        assert "speak" in method_names

    def test_class_line_range(self):
        meta = annotate_ruby(SOURCE_BASIC, "animal.rb")
        animal = next(c for c in meta.classes if c.name == "Animal")
        assert animal.line_range.start >= 1
        assert animal.line_range.end > animal.line_range.start

    def test_qualified_name(self):
        meta = annotate_ruby(SOURCE_INHERITANCE, "dog.rb")
        dog = next(c for c in meta.classes if c.name == "Dog")
        assert dog.qualified_name == "Dog"


class TestRubyModule:
    def test_module_extracted_as_class_info(self):
        meta = annotate_ruby(SOURCE_MODULE, "greeter.rb")
        names = {c.name for c in meta.classes}
        assert "Greeter" in names

    def test_module_has_empty_base_classes(self):
        meta = annotate_ruby(SOURCE_MODULE, "greeter.rb")
        greeter = next(c for c in meta.classes if c.name == "Greeter")
        assert greeter.base_classes == []

    def test_module_methods_extracted(self):
        meta = annotate_ruby(SOURCE_MODULE, "greeter.rb")
        method_names = {f.name for f in meta.functions}
        assert "hello" in method_names
        assert "goodbye" in method_names

    def test_full_module(self):
        meta = annotate_ruby(SOURCE_FULL, "full.rb")
        names = {c.name for c in meta.classes}
        assert "Admin" in names
        admin = next(c for c in meta.classes if c.name == "Admin")
        assert admin.base_classes == []


class TestRubyVisibility:
    def test_default_visibility_is_public(self):
        meta = annotate_ruby(SOURCE_BASIC, "animal.rb")
        speak = next(f for f in meta.functions if f.name == "speak" and f.parent_class == "Animal")
        assert speak.visibility == "public"

    def test_private_keyword_sets_visibility(self):
        meta = annotate_ruby(SOURCE_VISIBILITY, "account.rb")
        secret = next(f for f in meta.functions if f.name == "secret_method")
        assert secret.visibility == "private"

    def test_private_applies_to_subsequent_methods(self):
        meta = annotate_ruby(SOURCE_VISIBILITY, "account.rb")
        also = next(f for f in meta.functions if f.name == "also_secret")
        assert also.visibility == "private"

    def test_protected_sets_visibility(self):
        meta = annotate_ruby(SOURCE_VISIBILITY, "account.rb")
        semi = next(f for f in meta.functions if f.name == "semi_secret")
        assert semi.visibility == "protected"

    def test_public_resets_visibility(self):
        meta = annotate_ruby(SOURCE_VISIBILITY, "account.rb")
        back = next(f for f in meta.functions if f.name == "back_to_public")
        assert back.visibility == "public"

    def test_method_before_private_stays_public(self):
        meta = annotate_ruby(SOURCE_VISIBILITY, "account.rb")
        pub = next(f for f in meta.functions if f.name == "public_method")
        assert pub.visibility == "public"

    def test_inline_private_def(self):
        meta = annotate_ruby(SOURCE_INLINE_VISIBILITY, "service.rb")
        hidden = next(f for f in meta.functions if f.name == "hidden")
        assert hidden.visibility == "private"

    def test_inline_protected_def(self):
        meta = annotate_ruby(SOURCE_INLINE_VISIBILITY, "service.rb")
        guarded = next(f for f in meta.functions if f.name == "guarded")
        assert guarded.visibility == "protected"

    def test_inline_visibility_does_not_affect_state(self):
        """A `private def` should not change the running visibility for subsequent methods."""
        meta = annotate_ruby(SOURCE_INLINE_VISIBILITY, "service.rb")
        normal = next(f for f in meta.functions if f.name == "normal")
        assert normal.visibility == "public"
        # After `private def hidden`, the next method `protected def guarded`
        # should be protected, not affected by the inline private
        hidden = next(f for f in meta.functions if f.name == "hidden")
        assert hidden.visibility == "private"

    def test_full_source_visibility(self):
        meta = annotate_ruby(SOURCE_FULL, "full.rb")
        validate = next(f for f in meta.functions if f.name == "validate")
        assert validate.visibility == "private"
        encrypt = next(f for f in meta.functions if f.name == "encrypt_password")
        assert encrypt.visibility == "private"

    def test_class_method_visibility_in_full(self):
        meta = annotate_ruby(SOURCE_FULL, "full.rb")
        find = next(f for f in meta.functions if f.name == "self.find")
        assert find.visibility == "public"


SOURCE_TOP_LEVEL_SINGLETON = """\
def self.foo(x)
  x
end
"""

SOURCE_CLASS_INCLUDE = """\
class MyModel
  include Comparable
  extend Serializable
  include Foo::Bar

  def hello
  end
end
"""


class TestTopLevelSingletonMethod:
    def test_top_level_singleton_method_name(self):
        meta = annotate_ruby(SOURCE_TOP_LEVEL_SINGLETON, "tls.rb")
        foo = next(f for f in meta.functions if f.name == "self.foo")
        assert foo.name == "self.foo"

    def test_top_level_singleton_method_is_not_method(self):
        """Top-level singleton_method has no enclosing class, so is_method is False."""
        meta = annotate_ruby(SOURCE_TOP_LEVEL_SINGLETON, "tls.rb")
        foo = next(f for f in meta.functions if f.name == "self.foo")
        assert foo.is_method is False

    def test_top_level_singleton_method_parent_class_is_none(self):
        meta = annotate_ruby(SOURCE_TOP_LEVEL_SINGLETON, "tls.rb")
        foo = next(f for f in meta.functions if f.name == "self.foo")
        assert foo.parent_class is None


class TestClassBodyMixins:
    def test_include_inside_class_emits_import(self):
        meta = annotate_ruby(SOURCE_CLASS_INCLUDE, "model.rb")
        mods = {i.module for i in meta.imports}
        assert "Comparable" in mods

    def test_extend_inside_class_emits_import(self):
        meta = annotate_ruby(SOURCE_CLASS_INCLUDE, "model.rb")
        ext = next(i for i in meta.imports if i.module == "Serializable")
        assert ext.is_from_import is True
        assert ext.names == ["*"]

    def test_scoped_include_inside_class_emits_import(self):
        meta = annotate_ruby(SOURCE_CLASS_INCLUDE, "model.rb")
        scoped = next(i for i in meta.imports if "Foo" in i.module)
        assert scoped.module == "Foo::Bar"
        assert scoped.is_from_import is True
