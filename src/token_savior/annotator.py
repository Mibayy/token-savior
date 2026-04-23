"""Dispatch layer that selects the appropriate annotator by file type."""

from token_savior.c_annotator import annotate_c
from token_savior.conf_annotator import annotate_conf
from token_savior.dockerfile_annotator import annotate_dockerfile
from token_savior.csharp_annotator import annotate_csharp
from token_savior.env_annotator import annotate_env
from token_savior.generic_annotator import annotate_generic
from token_savior.go_annotator import annotate_go
from token_savior.gradle_annotator import annotate_gradle
from token_savior.hcl_annotator import annotate_hcl
from token_savior.ini_annotator import annotate_ini
from token_savior.java_annotator import annotate_java
from token_savior.json_annotator import annotate_json
from token_savior.prisma_annotator import annotate_prisma
from token_savior.models import AnnotatorProtocol, StructuralMetadata
from token_savior.python_annotator import annotate_python
from token_savior.ruby_annotator import annotate_ruby
from token_savior.rust_annotator import annotate_rust
from token_savior.text_annotator import annotate_text
from token_savior.toml_annotator import annotate_toml
from token_savior.typescript_annotator import annotate_typescript
from token_savior.xml_annotator import annotate_xml
from token_savior.yaml_annotator import annotate_yaml

_EXTENSION_MAP: dict[str, str] = {
    ".c": "c",
    ".h": "c",
    ".glsl": "c",
    ".vert": "c",
    ".frag": "c",
    ".comp": "c",
    ".py": "python",
    ".pyw": "python",
    ".md": "text",
    ".txt": "text",
    ".rst": "text",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".go": "go",
    ".rb": "ruby",
    ".rs": "rust",
    ".cs": "csharp",
    ".java": "java",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".ini": "ini",
    ".cfg": "ini",
    ".properties": "ini",
    ".gradle": "gradle",
    ".env": "env",
    ".xml": "xml",
    ".plist": "xml",
    ".svg": "xml",
    ".xhtml": "xml",
    ".hcl": "hcl",
    ".tf": "hcl",
    ".conf": "conf",
    ".prisma": "prisma",
}


_ANNOTATOR_MAP: dict[str, AnnotatorProtocol] = {
    "c": annotate_c,
    "python": annotate_python,
    "text": annotate_text,
    "typescript": annotate_typescript,
    "javascript": annotate_typescript,
    "go": annotate_go,
    "gradle": annotate_gradle,
    "ruby": annotate_ruby,
    "rust": annotate_rust,
    "csharp": annotate_csharp,
    "java": annotate_java,
    "json": annotate_json,
    "yaml": annotate_yaml,
    "ini": annotate_ini,
    "xml": annotate_xml,
    "hcl": annotate_hcl,
    "toml": annotate_toml,
    "env": annotate_env,
    "conf": annotate_conf,
    "dockerfile": annotate_dockerfile,
    "prisma": annotate_prisma,
}


def annotate(
    text: str,
    source_name: str = "<source>",
    file_type: str | None = None,
) -> StructuralMetadata:
    """Annotate text with structural metadata.

    Dispatch rules:
    - file_type overrides extension-based detection
    - Extension -> _EXTENSION_MAP -> language name -> _ANNOTATOR_MAP -> annotator
    - Dockerfile detected by filename pattern
    - Otherwise -> generic annotator (line-only)
    """
    if file_type is None:
        import os as _os

        _basename = _os.path.basename(source_name)
        if (
            _basename == "Dockerfile"
            or _basename.lower() == "dockerfile"
            or _basename.startswith("Dockerfile.")
            or _basename.lower().endswith(".dockerfile")
        ):
            file_type = "dockerfile"
        elif _basename.lower().endswith(".gradle.kts"):
            file_type = "gradle"
        elif _basename.startswith(".env") or _basename.endswith(".env"):
            file_type = "env"
        else:
            dot_idx = source_name.rfind(".")
            if dot_idx >= 0:
                ext = source_name[dot_idx:].lower()
                file_type = _EXTENSION_MAP.get(ext)

    annotator = _ANNOTATOR_MAP.get(file_type, annotate_generic)
    if not callable(annotator):
        raise TypeError(f"Annotator for '{file_type}' is not callable: {annotator!r}")
    return annotator(text, source_name)
