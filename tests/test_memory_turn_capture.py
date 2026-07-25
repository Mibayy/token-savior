"""Turn-level conversational capture.

The existing memory pipeline only observes tool calls (PostToolUse), so a
turn where the user states a preference, settles a decision, or validates a
piece of content leaves no trace. This module closes that hole at the ``Stop``
hook boundary.

No real LLM is ever called here. Tests either assert the cheap local gate
short-circuits before any subprocess, or monkey-patch the model boundary
(``_call_claude``) and assert downstream parsing + ``observation_save``.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from token_savior import memory_db
from token_savior.memory import turn_capture

PROJECT = "/tmp/test-project-turn-capture"


@pytest.fixture
def _memory_tmpdb(tmp_path: Path):
    db_path = tmp_path / "memory.db"
    with patch.object(memory_db, "MEMORY_DB_PATH", db_path):
        yield db_path


def _write_transcript(tmp_path: Path, entries: list[dict]) -> str:
    path = tmp_path / "transcript.jsonl"
    path.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in entries),
        encoding="utf-8",
    )
    return str(path)


def _turn(user_text: str, assistant_text: str) -> list[dict]:
    return [
        {"type": "user", "message": {"role": "user", "content": user_text}},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": assistant_text}],
            },
        },
    ]


# ── local gate: must never cost a subprocess ─────────────────────────────


class TestShouldCapture:
    def test_rejects_pure_command_prompt(self):
        assert turn_capture.should_capture("restart le service intel-api") is False

    def test_rejects_prompt_too_short_to_carry_intent(self):
        assert turn_capture.should_capture("ok") is False

    def test_detects_french_preference_signal(self):
        assert turn_capture.should_capture(
            "à l'avenir ne mets jamais de tiret long dans mes posts"
        ) is True

    def test_detects_french_decision_signal(self):
        assert turn_capture.should_capture(
            "on part sur la version pédagogique pour LinkedIn"
        ) is True

    def test_detects_english_preference_signal(self):
        assert turn_capture.should_capture(
            "from now on always use the noreply email for commits"
        ) is True

    def test_detects_correction_signal(self):
        assert turn_capture.should_capture(
            "attention à parler de facturer, c'est très api"
        ) is True


# ── transcript reader ────────────────────────────────────────────────────


class TestReadLastTurn:
    def test_returns_last_user_and_assistant_text(self, tmp_path: Path):
        path = _write_transcript(
            tmp_path,
            _turn("premier message", "première réponse")
            + _turn("on part sur la version B", "noté, version B"),
        )
        turn = turn_capture.read_last_turn(path)
        assert turn == {
            "user": "on part sur la version B",
            "assistant": "noté, version B",
        }

    def test_ignores_tool_result_blocks(self, tmp_path: Path):
        path = _write_transcript(
            tmp_path,
            [
                {
                    "type": "user",
                    "message": {"role": "user", "content": "on part sur la version B"},
                },
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {"type": "tool_result", "content": "exit code 0"}
                        ],
                    },
                },
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "noté"}],
                    },
                },
            ],
        )
        turn = turn_capture.read_last_turn(path)
        assert turn["user"] == "on part sur la version B"

    def test_returns_none_when_transcript_missing(self, tmp_path: Path):
        assert turn_capture.read_last_turn(str(tmp_path / "nope.jsonl")) is None

    def test_returns_none_on_corrupt_lines(self, tmp_path: Path):
        path = tmp_path / "bad.jsonl"
        path.write_text("not-json\n{also-not-json\n", encoding="utf-8")
        assert turn_capture.read_last_turn(str(path)) is None


# ── parser ───────────────────────────────────────────────────────────────


class TestParseItems:
    def test_accepts_the_new_conversational_types(self):
        raw = json.dumps([
            {"type": "preference", "title": "t1", "content": "c1", "why": "w"},
            {"type": "decision", "title": "t2", "content": "c2"},
            {"type": "content", "title": "t3", "content": "c3"},
        ])
        items = turn_capture._parse_items(raw)
        assert [i["type"] for i in items] == ["preference", "decision", "content"]

    def test_drops_item_with_unknown_type(self):
        raw = json.dumps([{"type": "chmod", "title": "t", "content": "c"}])
        assert turn_capture._parse_items(raw) == []

    def test_drops_item_missing_required_field(self):
        raw = json.dumps([{"type": "preference", "title": "t"}])
        assert turn_capture._parse_items(raw) == []

    def test_returns_empty_on_non_json(self):
        assert turn_capture._parse_items("désolé, je ne peux pas") == []

    def test_strips_markdown_code_fences(self):
        """The CLI wraps its answer in ```json fences; strict json.loads dies."""
        raw = (
            '```json\n'
            '[{"type": "preference", "title": "t", "content": "c"}]\n'
            '```'
        )
        items = turn_capture._parse_items(raw)
        assert [i["title"] for i in items] == ["t"]

    def test_recovers_array_wrapped_in_prose(self):
        raw = 'Voici :\n[{"type": "decision", "title": "t", "content": "c"}]\nVoilà.'
        items = turn_capture._parse_items(raw)
        assert [i["type"] for i in items] == ["decision"]

    def test_caps_at_max_obs_per_turn(self):
        raw = json.dumps([
            {"type": "preference", "title": f"t{i}", "content": "c"}
            for i in range(10)
        ])
        items = turn_capture._parse_items(raw)
        assert len(items) == turn_capture.MAX_OBS_PER_TURN


# ── orchestration ────────────────────────────────────────────────────────


class TestCaptureTurn:
    def test_no_model_call_when_gate_rejects(self, tmp_path: Path, monkeypatch):
        path = _write_transcript(
            tmp_path, _turn("restart intel-api", "fait")
        )
        calls = {"n": 0}

        def should_not_run(*a, **kw):
            calls["n"] += 1
            return "[]"

        monkeypatch.setattr(turn_capture, "_call_claude", should_not_run)
        assert turn_capture.capture_turn(path, PROJECT) == []
        assert calls["n"] == 0

    def test_saves_observations_when_signal_present(
        self, tmp_path: Path, monkeypatch, _memory_tmpdb
    ):
        path = _write_transcript(
            tmp_path,
            _turn(
                "à l'avenir ne mets jamais de tiret long dans mes posts",
                "compris, noté",
            ),
        )
        monkeypatch.setattr(
            turn_capture,
            "_call_claude",
            lambda prompt, model: json.dumps([{
                "type": "preference",
                "title": "Pas de tiret long",
                "content": "Jamais de tiret long dans les contenus publiés",
                "why": "Demande explicite de Louis",
            }]),
        )
        saved = turn_capture.capture_turn(path, PROJECT)
        assert len(saved) == 1

        db = memory_db.get_db()
        row = db.execute(
            "SELECT type, title, tags FROM observations WHERE project_root=?",
            [PROJECT],
        ).fetchone()
        db.close()
        assert row[0] == "preference"
        assert row[1] == "Pas de tiret long"
        assert "turn-capture" in row[2]

    def test_uses_the_cheap_model(self, tmp_path: Path, monkeypatch, _memory_tmpdb):
        path = _write_transcript(
            tmp_path, _turn("on part sur la version pédagogique", "ok")
        )
        seen = {}

        def fake_call(prompt, model):
            seen["model"] = model
            return "[]"

        monkeypatch.setattr(turn_capture, "_call_claude", fake_call)
        turn_capture.capture_turn(path, PROJECT)
        assert seen["model"] == turn_capture.DEFAULT_MODEL

    def test_returns_empty_when_model_unavailable(
        self, tmp_path: Path, monkeypatch, _memory_tmpdb
    ):
        path = _write_transcript(
            tmp_path, _turn("on part sur la version pédagogique", "ok")
        )
        monkeypatch.setattr(turn_capture, "_call_claude", lambda p, m: None)
        assert turn_capture.capture_turn(path, PROJECT) == []
