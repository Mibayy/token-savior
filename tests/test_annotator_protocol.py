"""Tests for annotator protocol compliance."""
from token_savior.annotator import _ANNOTATOR_MAP, annotate_generic
from token_savior.models import StructuralMetadata


class TestAnnotatorProtocol:
    def test_all_annotators_are_callable(self):
        """Every registered annotator must be callable."""
        for name, func in _ANNOTATOR_MAP.items():
            assert callable(func), f"Annotator '{name}' is not callable: {func!r}"

    def test_all_annotators_return_structural_metadata(self):
        """Every annotator must return StructuralMetadata for empty input."""
        for name, func in _ANNOTATOR_MAP.items():
            result = func("", f"test.{name}")
            assert isinstance(result, StructuralMetadata), (
                f"Annotator '{name}' returned {type(result).__name__}, expected StructuralMetadata"
            )

    def test_generic_fallback_returns_structural_metadata(self):
        result = annotate_generic("hello\nworld", "test.unknown")
        assert isinstance(result, StructuralMetadata)
        assert result.total_lines == 2

    def test_annotator_map_covers_all_languages(self):
        """Sanity check that the map has a reasonable number of annotators."""
        assert len(_ANNOTATOR_MAP) >= 15, f"Expected >= 15 annotators, got {len(_ANNOTATOR_MAP)}"
