"""Tests for compact checkpoints and restore."""

from __future__ import annotations

from token_savior.checkpoint_ops import (
    compare_checkpoint_by_symbol,
    create_checkpoint,
    delete_checkpoint,
    list_checkpoints,
    prune_checkpoints,
    restore_checkpoint,
)
from token_savior.models import ProjectIndex


class TestCheckpointOps:
    def test_create_and_restore_checkpoint(self, tmp_path):
        (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")
        index = ProjectIndex(root_path=str(tmp_path))

        checkpoint = create_checkpoint(index, ["main.py"])
        assert checkpoint["ok"] is True
        assert checkpoint["saved_files"] == ["main.py"]

        (tmp_path / "main.py").write_text("x = 2\n", encoding="utf-8")
        restored = restore_checkpoint(index, checkpoint["checkpoint_id"])

        assert restored["ok"] is True
        assert restored["restored_files"] == ["main.py"]
        assert (tmp_path / "main.py").read_text(encoding="utf-8") == "x = 1\n"

    def test_compare_checkpoint_by_symbol(self, tmp_path):
        (tmp_path / "main.py").write_text("def hello():\n    return 'a'\n", encoding="utf-8")
        index = ProjectIndex(root_path=str(tmp_path))

        checkpoint = create_checkpoint(index, ["main.py"])
        (tmp_path / "main.py").write_text(
            "def hello():\n    return 'b'\n\n\ndef world():\n    return 'c'\n",
            encoding="utf-8",
        )

        result = compare_checkpoint_by_symbol(index, checkpoint["checkpoint_id"])

        assert result["ok"] is True
        assert result["files"][0]["symbols"]["changed"] == ["hello"]
        assert result["files"][0]["symbols"]["added"] == ["world"]

    def test_list_delete_and_prune_checkpoints(self, tmp_path):
        (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")
        index = ProjectIndex(root_path=str(tmp_path))

        create_checkpoint(index, ["main.py"])
        (tmp_path / "main.py").write_text("x = 2\n", encoding="utf-8")
        create_checkpoint(index, ["main.py"])

        listed = list_checkpoints(index)
        assert listed["ok"] is True
        assert len(listed["checkpoints"]) == 2

        pruned = prune_checkpoints(index, keep_last=1)
        assert pruned["ok"] is True
        assert len(pruned["deleted"]) == 1

        remaining = list_checkpoints(index)
        remaining_id = remaining["checkpoints"][0]["checkpoint_id"]
        deleted = delete_checkpoint(index, remaining_id)
        assert deleted["ok"] is True
