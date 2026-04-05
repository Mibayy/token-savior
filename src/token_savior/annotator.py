"""Dispatch layer that selects the appropriate annotator by file type."""

from token_savior.csharp_annotator import annotate_csharp
from token_savior.generic_annotator import annotate_generic
from token_savior.go_annotator import annotate_go
from token_savior.json_annotator import annotate_json
from token_savior.models import StructuralMetadata
from token_savior.yaml_annotator import annotate_yaml
from token_savior.python_annotator import annotate_python
from token_savior.rust_annotator import annotate_rust
from token_savior.text_annotator import annotate_text
from token_savior.typescript_annotator import annotate_typescript

_EXTENSION_MAP: dict[str, str] = {
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
    ".rs": "rust",
    ".cs": "csharp",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
}


def annotate(
    text: str,
    source_name: str = "<source>",
    file_type: str | None = None,
) -> StructuralMetadata:
    """Annotate text with structural metadata.

    Dispatch rules:
    - file_type overrides extension-based detection
    - .py -> python annotator
    - .md, .txt, .rst -> text annotator
    - .ts, .tsx -> typescript annotator
    - .js, .jsx -> typescript annotator (close enough for regex-based parsing)
    - .go -> go annotator
    - .rs -> rust annotator
    - Otherwise -> generic annotator (line-only)
    """
    if file_type is None:
        # Detect from source_name extension
        dot_idx = source_name.rfind(".")
        if dot_idx >= 0:
            ext = source_name[dot_idx:].lower()
            file_type = _EXTENSION_MAP.get(ext)

    if file_type == "python":
        return annotate_python(text, source_name)
    elif file_type == "text":
        return annotate_text(text, source_name)
    elif file_type in ("typescript", "javascript"):
        return annotate_typescript(text, source_name)
    elif file_type == "go":
        return annotate_go(text, source_name)
    elif file_type == "rust":
        return annotate_rust(text, source_name)
    elif file_type == "csharp":
        return annotate_csharp(text, source_name)
    elif file_type == "json":
        return annotate_json(text, source_name)
    elif file_type == "yaml":
        return annotate_yaml(text, source_name)
    else:
        return annotate_generic(text, source_name)
