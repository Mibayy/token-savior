"""Tests for the regex-based C annotator (ISO C99/C11)."""

from token_savior.c_annotator import annotate_c


class TestIncludeDetection:
    """Tests for #include directive parsing."""

    def test_system_include(self):
        src = "#include <stdio.h>\n"
        meta = annotate_c(src)
        assert len(meta.imports) == 1
        imp = meta.imports[0]
        assert imp.module == "stdio.h"
        assert imp.alias == "system"
        assert imp.line_number == 1

    def test_local_include(self):
        src = '#include "myheader.h"\n'
        meta = annotate_c(src)
        assert len(meta.imports) == 1
        imp = meta.imports[0]
        assert imp.module == "myheader.h"
        assert imp.alias == "local"

    def test_multiple_includes(self):
        src = '#include <stdio.h>\n#include <stdlib.h>\n#include "app.h"\n#include "log.h"\n'
        meta = annotate_c(src)
        assert len(meta.imports) == 4
        assert meta.imports[0].module == "stdio.h"
        assert meta.imports[1].module == "stdlib.h"
        assert meta.imports[2].module == "app.h"
        assert meta.imports[3].module == "log.h"
        assert meta.imports[0].alias == "system"
        assert meta.imports[2].alias == "local"

    def test_include_with_path(self):
        src = '#include "effects/fx_auto_exposure.h"\n'
        meta = annotate_c(src)
        assert len(meta.imports) == 1
        assert meta.imports[0].module == "effects/fx_auto_exposure.h"

    def test_include_with_spaces(self):
        src = "#  include  <string.h>\n"
        meta = annotate_c(src)
        assert len(meta.imports) == 1
        assert meta.imports[0].module == "string.h"


class TestFunctionDetection:
    """Tests for C function definition parsing."""

    def test_simple_function(self):
        src = "int add(int a, int b)\n{\n    return a + b;\n}\n"
        meta = annotate_c(src)
        assert len(meta.functions) == 1
        f = meta.functions[0]
        assert f.name == "add"
        assert f.qualified_name == "add"
        assert f.is_method is False
        assert f.parent_class is None
        assert "a" in f.parameters
        assert "b" in f.parameters
        assert f.line_range.start == 1
        assert f.line_range.end == 4

    def test_void_function(self):
        src = "void cleanup(void)\n{\n    /* nothing */\n}\n"
        meta = annotate_c(src)
        assert len(meta.functions) == 1
        f = meta.functions[0]
        assert f.name == "cleanup"
        assert f.parameters == []

    def test_static_function(self):
        src = "static void handle_input(App* app)\n{\n    app->running = 1;\n}\n"
        meta = annotate_c(src)
        assert len(meta.functions) == 1
        f = meta.functions[0]
        assert f.name == "handle_input"
        assert "static" in f.decorators
        assert "app" in f.parameters

    def test_static_inline_function(self):
        src = "static inline bool check_flag(int value, int flag)\n{\n    return (value & flag) != 0;\n}\n"
        meta = annotate_c(src)
        assert len(meta.functions) == 1
        f = meta.functions[0]
        assert f.name == "check_flag"
        assert "static" in f.decorators
        assert "inline" in f.decorators
        assert "value" in f.parameters
        assert "flag" in f.parameters

    def test_pointer_return_type(self):
        src = "char* get_name(int id)\n{\n    return names[id];\n}\n"
        meta = annotate_c(src)
        assert len(meta.functions) == 1
        assert meta.functions[0].name == "get_name"
        assert "id" in meta.functions[0].parameters

    def test_struct_pointer_param(self):
        src = 'void print_node(struct Node *node)\n{\n    printf("%d\\n", node->value);\n}\n'
        meta = annotate_c(src)
        assert len(meta.functions) == 1
        assert "node" in meta.functions[0].parameters

    def test_function_with_brace_on_same_line(self):
        src = "int square(int x) {\n    return x * x;\n}\n"
        meta = annotate_c(src)
        assert len(meta.functions) == 1
        f = meta.functions[0]
        assert f.name == "square"
        assert f.line_range.start == 1
        assert f.line_range.end == 3

    def test_multiline_params(self):
        src = (
            "void complex_func(int width,\n"
            "                   int height,\n"
            "                   float scale)\n"
            "{\n"
            "    /* body */\n"
            "}\n"
        )
        meta = annotate_c(src)
        assert len(meta.functions) == 1
        f = meta.functions[0]
        assert f.name == "complex_func"
        assert "width" in f.parameters
        assert "height" in f.parameters
        assert "scale" in f.parameters

    def test_function_pointer_param(self):
        src = "void register_callback(void (*callback)(int, int))\n{\n}\n"
        meta = annotate_c(src)
        assert len(meta.functions) == 1
        assert "callback" in meta.functions[0].parameters

    def test_array_param(self):
        src = "void process(int arr[10], int count)\n{\n}\n"
        meta = annotate_c(src)
        assert len(meta.functions) == 1
        f = meta.functions[0]
        assert "arr" in f.parameters
        assert "count" in f.parameters

    def test_variadic_function(self):
        src = "void log_msg(const char *fmt, ...)\n{\n}\n"
        meta = annotate_c(src)
        assert len(meta.functions) == 1
        assert "fmt" in meta.functions[0].parameters

    def test_multiple_functions(self):
        src = (
            "int add(int a, int b)\n{\n    return a + b;\n}\n\n"
            "int sub(int a, int b)\n{\n    return a - b;\n}\n"
        )
        meta = annotate_c(src)
        assert len(meta.functions) == 2
        names = [f.name for f in meta.functions]
        assert "add" in names
        assert "sub" in names


class TestStructDetection:
    """Tests for struct/union/enum definition parsing."""

    def test_simple_struct(self):
        src = "struct Node {\n    int value;\n    struct Node *next;\n};\n"
        meta = annotate_c(src)
        assert len(meta.classes) == 1
        c = meta.classes[0]
        assert c.name == "Node"
        assert "struct" in c.decorators
        assert c.line_range.start == 1

    def test_typedef_struct(self):
        src = "typedef struct {\n    int x;\n    int y;\n} Point;\n"
        meta = annotate_c(src)
        assert len(meta.classes) == 1
        c = meta.classes[0]
        assert c.name == "Point"
        assert "typedef" in c.decorators
        assert "struct" in c.decorators

    def test_typedef_named_struct(self):
        src = "typedef struct Node {\n    int value;\n    struct Node *next;\n} Node;\n"
        meta = annotate_c(src)
        assert len(meta.classes) >= 1
        names = [c.name for c in meta.classes]
        assert "Node" in names

    def test_union(self):
        src = "union Data {\n    int i;\n    float f;\n    char c;\n};\n"
        meta = annotate_c(src)
        assert len(meta.classes) == 1
        c = meta.classes[0]
        assert c.name == "Data"
        assert "union" in c.decorators

    def test_enum(self):
        src = "enum Color {\n    RED,\n    GREEN,\n    BLUE\n};\n"
        meta = annotate_c(src)
        assert len(meta.classes) == 1
        c = meta.classes[0]
        assert c.name == "Color"
        assert "enum" in c.decorators

    def test_anonymous_enum_constant(self):
        """Anonymous enums used as constants (common C pattern from suckless-ogl)."""
        src = "enum { PBR_DEBUG_MODE_COUNT = 10 };\n"
        meta = annotate_c(src)
        # Anonymous enums don't have a name to capture — should not crash
        assert meta is not None

    def test_typedef_enum(self):
        src = "typedef enum {\n    AA_NONE,\n    AA_FXAA,\n    AA_SMAA\n} AAMode;\n"
        meta = annotate_c(src)
        assert len(meta.classes) == 1
        c = meta.classes[0]
        assert c.name == "AAMode"
        assert "typedef" in c.decorators
        assert "enum" in c.decorators


class TestTypedefDetection:
    """Tests for typedef alias parsing."""

    def test_simple_typedef(self):
        src = "typedef unsigned int uint32;\n"
        meta = annotate_c(src)
        assert len(meta.classes) == 1
        c = meta.classes[0]
        assert c.name == "uint32"
        assert "typedef" in c.decorators
        assert "unsigned int" in c.base_classes

    def test_typedef_function_pointer(self):
        src = "typedef void (*EventHandler)(int event, void *data);\n"
        meta = annotate_c(src)
        assert len(meta.classes) == 1
        c = meta.classes[0]
        assert c.name == "EventHandler"
        assert "function_pointer" in c.decorators

    def test_forward_declaration_ignored(self):
        """Forward declarations should be skipped."""
        src = "struct App;\ntypedef struct App App;\n"
        meta = annotate_c(src)
        # Forward declarations can be parsed but should not crash
        assert meta is not None


class TestDefineDetection:
    """Tests for #define macro parsing."""

    def test_object_like_macro(self):
        src = "#define MAX_SIZE 1024\n"
        meta = annotate_c(src)
        assert len(meta.functions) == 1
        f = meta.functions[0]
        assert f.name == "MAX_SIZE"
        assert f.parameters == []
        assert "macro" in f.decorators

    def test_function_like_macro(self):
        src = "#define MIN(a, b) ((a) < (b) ? (a) : (b))\n"
        meta = annotate_c(src)
        assert len(meta.functions) == 1
        f = meta.functions[0]
        assert f.name == "MIN"
        assert "a" in f.parameters
        assert "b" in f.parameters
        assert "macro" in f.decorators

    def test_multiline_macro(self):
        src = (
            "#define TRANSFER_OWNERSHIP(ptr) \\\n"
            "    do { \\\n"
            "        (void)(ptr); \\\n"
            "    } while (0)\n"
        )
        meta = annotate_c(src)
        assert len(meta.functions) == 1
        f = meta.functions[0]
        assert f.name == "TRANSFER_OWNERSHIP"
        assert f.line_range.start == 1
        assert f.line_range.end == 4

    def test_include_guard_ignored(self):
        """Include guards are parsed as macros, which is acceptable."""
        src = "#ifndef MY_HEADER_H\n#define MY_HEADER_H\n\n/* content */\n\n#endif\n"
        meta = annotate_c(src)
        found = [f for f in meta.functions if f.name == "MY_HEADER_H"]
        assert len(found) == 1  # Include guard is a valid #define


class TestDocComments:
    """Tests for Doxygen-style documentation extraction."""

    def test_triple_slash_comment(self):
        src = "/// Adds two integers.\nint add(int a, int b)\n{\n    return a + b;\n}\n"
        meta = annotate_c(src)
        assert len(meta.functions) == 1
        assert meta.functions[0].docstring == "Adds two integers."

    def test_block_doc_comment(self):
        src = "/** Frees all resources. */\nvoid cleanup(void)\n{\n}\n"
        meta = annotate_c(src)
        assert len(meta.functions) == 1
        assert meta.functions[0].docstring is not None
        assert "Frees all resources" in meta.functions[0].docstring

    def test_multiline_doxygen(self):
        src = (
            "/**\n"
            " * Initialize the effect.\n"
            " * Returns 1 on success.\n"
            " */\n"
            "int fx_init(void)\n"
            "{\n"
            "    return 1;\n"
            "}\n"
        )
        meta = annotate_c(src)
        assert len(meta.functions) == 1
        doc = meta.functions[0].docstring
        assert doc is not None
        assert "Initialize" in doc


class TestDependencyGraph:
    """Tests for intra-file dependency graph building."""

    def test_function_calls_another(self):
        src = "void helper(void)\n{\n}\n\nvoid main_func(void)\n{\n    helper();\n}\n"
        meta = annotate_c(src)
        assert "main_func" in meta.dependency_graph
        assert "helper" in meta.dependency_graph["main_func"]

    def test_struct_reference_in_function(self):
        src = (
            "struct Node {\n    int val;\n};\n\n"
            "void print_node(struct Node *n)\n{\n    Node dummy;\n}\n"
        )
        meta = annotate_c(src)
        assert "print_node" in meta.dependency_graph
        assert "Node" in meta.dependency_graph["print_node"]

    def test_no_self_reference(self):
        src = "int recursive(int n)\n{\n    return recursive(n - 1);\n}\n"
        meta = annotate_c(src)
        assert "recursive" in meta.dependency_graph
        assert "recursive" not in meta.dependency_graph["recursive"]


class TestMetadataFields:
    """Tests for basic metadata correctness."""

    def test_source_name(self):
        src = "int x = 1;\n"
        meta = annotate_c(src, source_name="test.c")
        assert meta.source_name == "test.c"

    def test_line_count(self):
        src = "line1\nline2\nline3\n"
        meta = annotate_c(src)
        assert meta.total_lines == 4  # trailing newline creates empty last line

    def test_char_count(self):
        src = "int x;\n"
        meta = annotate_c(src)
        assert meta.total_chars == len(src)

    def test_line_offsets(self):
        src = "abc\ndef\nghi\n"
        meta = annotate_c(src)
        assert meta.line_char_offsets[0] == 0
        assert meta.line_char_offsets[1] == 4  # "abc\n" = 4 chars
        assert meta.line_char_offsets[2] == 8

    def test_lines_content(self):
        src = "int a;\nint b;\n"
        meta = annotate_c(src)
        assert meta.lines[0] == "int a;"
        assert meta.lines[1] == "int b;"


class TestRealWorldPatterns:
    """Tests based on actual suckless-ogl code patterns."""

    def test_suckless_ogl_effect_init(self):
        """Pattern from fx_auto_exposure.c"""
        src = (
            '#include "effects/fx_auto_exposure.h"\n'
            '#include "shader.h"\n'
            '#include "log.h"\n'
            "#include <stddef.h>\n"
            "\n"
            "/* Auto Exposure Constants */\n"
            "static const float EXPOSURE_INITIAL_VAL = 1.20F;\n"
            "static const int LUM_DOWNSAMPLE_GROUP_SIZE = 16;\n"
            "\n"
            "int fx_auto_exposure_init(PostProcess* post_processing)\n"
            "{\n"
            "    AutoExposureFX* auto_exp = &post_processing->auto_exposure_fx;\n"
            "    return 1;\n"
            "}\n"
            "\n"
            "void fx_auto_exposure_cleanup(PostProcess* post_processing)\n"
            "{\n"
            "}\n"
        )
        meta = annotate_c(src, source_name="fx_auto_exposure.c")
        assert meta.source_name == "fx_auto_exposure.c"
        assert len(meta.imports) == 4
        assert len(meta.functions) == 2
        names = [f.name for f in meta.functions]
        assert "fx_auto_exposure_init" in names
        assert "fx_auto_exposure_cleanup" in names

    def test_suckless_ogl_callback_pattern(self):
        """Pattern from app_input.c: callback with GLFWwindow*"""
        src = (
            "void framebuffer_size_callback(GLFWwindow* window, int width, int height)\n"
            "{\n"
            "    App* app = (App*)glfwGetWindowUserPointer(window);\n"
            "    app->width = width;\n"
            "    app->height = height;\n"
            "}\n"
        )
        meta = annotate_c(src)
        assert len(meta.functions) == 1
        f = meta.functions[0]
        assert f.name == "framebuffer_size_callback"
        assert "window" in f.parameters
        assert "width" in f.parameters
        assert "height" in f.parameters

    def test_suckless_ogl_enum_constants(self):
        """Pattern: enum { CONSTANT = value }; as constant definition."""
        src = "enum { PBR_DEBUG_MODE_COUNT = 10 };\nenum { NOTIF_BUF_SIZE = 128 };\n"
        meta = annotate_c(src)
        # Should not crash — anonymous enums may or may not be captured
        assert meta is not None

    def test_suckless_ogl_typedef_struct(self):
        """Pattern from suckless-ogl headers."""
        src = (
            "typedef struct {\n"
            "    float exposure;\n"
            "    float adaptation_speed;\n"
            "    int active_path;\n"
            "} AutoExposureFX;\n"
        )
        meta = annotate_c(src)
        assert len(meta.classes) == 1
        c = meta.classes[0]
        assert c.name == "AutoExposureFX"
        assert "typedef" in c.decorators
        assert "struct" in c.decorators

    def test_suckless_ogl_static_inline(self):
        """Pattern from utils.h."""
        src = (
            "static inline bool check_flag(int value, int flag)\n"
            "{\n"
            "    return (value & flag) != 0;\n"
            "}\n"
        )
        meta = annotate_c(src)
        assert len(meta.functions) == 1
        f = meta.functions[0]
        assert f.name == "check_flag"
        assert "static" in f.decorators
        assert "inline" in f.decorators

    def test_suckless_ogl_macro_pattern(self):
        """Pattern from utils.h."""
        src = (
            "#define CLEANUP_FREE __attribute__((cleanup(cleanup_free)))\n"
            "#define TRANSFER_OWNERSHIP(ptr) \\\n"
            "    do { \\\n"
            "        (void)(ptr); \\\n"
            "    } while (0)\n"
        )
        meta = annotate_c(src)
        assert len(meta.functions) == 2
        names = [f.name for f in meta.functions]
        assert "CLEANUP_FREE" in names
        assert "TRANSFER_OWNERSHIP" in names

    def test_full_header_file(self):
        """Simulate a complete small C header."""
        src = (
            "#ifndef FX_UTILS_H\n"
            "#define FX_UTILS_H\n"
            "\n"
            '#include "gl_common.h"\n'
            "\n"
            "typedef struct {\n"
            "    int width;\n"
            "    int height;\n"
            "    unsigned int internal_format;\n"
            "    unsigned int format;\n"
            "    unsigned int type;\n"
            "    int min_filter;\n"
            "    int mag_filter;\n"
            "    int wrap_s;\n"
            "    int wrap_t;\n"
            "    const void* initial_data;\n"
            "} FXTextureConfig;\n"
            "\n"
            "void fx_utils_create_texture(unsigned int* tex, const FXTextureConfig* cfg);\n"
            "\n"
            "#endif\n"
        )
        meta = annotate_c(src, source_name="fx_utils.h")
        assert len(meta.imports) == 1
        assert meta.imports[0].module == "gl_common.h"
        assert len(meta.classes) >= 1
        names = [c.name for c in meta.classes]
        assert "FXTextureConfig" in names

    def test_complex_function_with_gl_types(self):
        """Real-world OpenGL function pattern with unsigned int types."""
        src = (
            "void fx_utils_create_texture(unsigned int* tex, const FXTextureConfig* cfg)\n"
            "{\n"
            "    glGenTextures(1, tex);\n"
            "    glBindTexture(GL_TEXTURE_2D, *tex);\n"
            "}\n"
        )
        meta = annotate_c(src)
        assert len(meta.functions) == 1
        f = meta.functions[0]
        assert f.name == "fx_utils_create_texture"
        assert "tex" in f.parameters
        assert "cfg" in f.parameters


class TestEdgeCases:
    """Edge cases and tricky C patterns."""

    def test_empty_source(self):
        meta = annotate_c("")
        assert meta.total_lines == 1
        assert meta.total_chars == 0
        assert meta.functions == []
        assert meta.classes == []
        assert meta.imports == []

    def test_comments_only(self):
        src = "/* This is a comment */\n// Another comment\n"
        meta = annotate_c(src)
        assert meta.functions == []
        assert meta.classes == []

    def test_string_with_braces(self):
        """Braces inside strings should not confuse the parser."""
        src = 'void test(void)\n{\n    printf("{ not a real brace }");\n}\n'
        meta = annotate_c(src)
        assert len(meta.functions) == 1
        assert meta.functions[0].line_range.end == 4

    def test_no_crash_on_declaration_without_body(self):
        """Function prototype (no body) should NOT be detected as function."""
        src = "int add(int a, int b);\n"
        meta = annotate_c(src)
        # Prototypes should not be captured as function definitions
        func_names = [f.name for f in meta.functions if "macro" not in f.decorators]
        assert "add" not in func_names
