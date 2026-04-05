"""Tests for the Dockerfile annotator."""

import pytest

from token_savior.dockerfile_annotator import annotate_dockerfile


class TestDockerfileBasic:
    def test_from_is_level_1(self):
        text = "FROM python:3.12-slim\n"
        meta = annotate_dockerfile(text, "Dockerfile")
        froms = [s for s in meta.sections if s.level == 1]
        assert len(froms) == 1
        assert "FROM" in froms[0].title

    def test_run_is_level_2(self):
        text = "FROM python:3.12-slim\nRUN apt-get update\n"
        meta = annotate_dockerfile(text, "Dockerfile")
        runs = [s for s in meta.sections if "RUN" in s.title]
        assert len(runs) == 1
        assert runs[0].level == 2

    def test_copy_is_level_2(self):
        text = "FROM python:3.12-slim\nCOPY . /app\n"
        meta = annotate_dockerfile(text, "Dockerfile")
        copies = [s for s in meta.sections if "COPY" in s.title]
        assert len(copies) == 1
        assert copies[0].level == 2

    def test_env_is_level_2(self):
        text = "FROM python:3.12-slim\nENV APP_ENV=production\n"
        meta = annotate_dockerfile(text, "Dockerfile")
        envs = [s for s in meta.sections if "ENV" in s.title]
        assert len(envs) == 1
        assert envs[0].level == 2

    def test_source_name_default(self):
        text = "FROM ubuntu:22.04\n"
        meta = annotate_dockerfile(text)
        assert meta.source_name == "<dockerfile>"

    def test_source_name_custom(self):
        text = "FROM ubuntu:22.04\n"
        meta = annotate_dockerfile(text, "Dockerfile")
        assert meta.source_name == "Dockerfile"

    def test_total_lines(self):
        text = "FROM python:3.12\nRUN echo hi\nCOPY . .\n"
        meta = annotate_dockerfile(text, "Dockerfile")
        assert meta.total_lines == 3

    def test_total_chars(self):
        text = "FROM python:3.12\n"
        meta = annotate_dockerfile(text, "Dockerfile")
        assert meta.total_chars == len(text)

    def test_functions_classes_imports_empty(self):
        text = "FROM python:3.12\nRUN echo hi\n"
        meta = annotate_dockerfile(text, "Dockerfile")
        assert meta.functions == []
        assert meta.classes == []
        assert meta.imports == []

    def test_line_range_populated(self):
        text = "FROM python:3.12\nRUN echo hi\n"
        meta = annotate_dockerfile(text, "Dockerfile")
        from_section = next(s for s in meta.sections if "FROM" in s.title)
        assert from_section.line_range.start == 1
        run_section = next(s for s in meta.sections if "RUN" in s.title)
        assert run_section.line_range.start == 2


class TestDockerfileMultiStage:
    def test_multiple_from_creates_multiple_level1(self):
        text = "FROM python:3.12 AS builder\nRUN pip install\nFROM python:3.12-slim\nCOPY --from=builder /app /app\n"
        meta = annotate_dockerfile(text, "Dockerfile")
        froms = [s for s in meta.sections if s.level == 1]
        assert len(froms) == 2

    def test_instructions_under_each_from_are_level_2(self):
        text = "FROM python:3.12 AS builder\nRUN pip install reqs\nFROM python:3.12-slim\nCOPY --from=builder /app /app\n"
        meta = annotate_dockerfile(text, "Dockerfile")
        level2 = [s for s in meta.sections if s.level == 2]
        assert len(level2) == 2

    def test_from_title_includes_image(self):
        text = "FROM node:18-alpine AS runner\n"
        meta = annotate_dockerfile(text, "Dockerfile")
        assert "node:18-alpine" in meta.sections[0].title

    def test_second_from_resets_to_level_1(self):
        text = "FROM alpine:3 AS base\nRUN echo base\nFROM alpine:3 AS final\nRUN echo final\n"
        meta = annotate_dockerfile(text, "Dockerfile")
        second_from = [s for s in meta.sections if s.level == 1][1]
        assert "FROM" in second_from.title


class TestDockerfileComments:
    def test_comments_are_ignored(self):
        text = "# This is a comment\nFROM python:3.12\n# Another comment\nRUN echo hi\n"
        meta = annotate_dockerfile(text, "Dockerfile")
        comment_sections = [s for s in meta.sections if "#" in s.title]
        assert len(comment_sections) == 0

    def test_blank_lines_ignored(self):
        text = "FROM python:3.12\n\n\nRUN echo hi\n"
        meta = annotate_dockerfile(text, "Dockerfile")
        assert len(meta.sections) == 2  # FROM + RUN


class TestDockerfileEnvAndArg:
    def test_env_variable_name_in_title(self):
        text = "FROM python:3.12\nENV DATABASE_URL=postgres://localhost/db\n"
        meta = annotate_dockerfile(text, "Dockerfile")
        env_section = next(s for s in meta.sections if "ENV" in s.title)
        assert "DATABASE_URL" in env_section.title

    def test_arg_variable_name_in_title(self):
        text = "FROM python:3.12\nARG BUILD_VERSION=1.0\n"
        meta = annotate_dockerfile(text, "Dockerfile")
        arg_section = next(s for s in meta.sections if "ARG" in s.title)
        assert "BUILD_VERSION" in arg_section.title

    def test_multiple_env_vars(self):
        text = "FROM python:3.12\nENV APP_ENV=prod\nENV DEBUG=false\n"
        meta = annotate_dockerfile(text, "Dockerfile")
        env_sections = [s for s in meta.sections if "ENV" in s.title]
        assert len(env_sections) == 2


class TestDockerfileLongRun:
    def test_run_command_truncated_to_60_chars(self):
        long_cmd = "apt-get install -y " + "a" * 100
        text = f"FROM ubuntu:22.04\nRUN {long_cmd}\n"
        meta = annotate_dockerfile(text, "Dockerfile")
        run_section = next(s for s in meta.sections if "RUN" in s.title)
        # Title should be truncated: "RUN " + ~60 chars
        assert len(run_section.title) <= 70  # some wiggle room for "RUN " prefix

    def test_short_run_not_truncated(self):
        text = "FROM ubuntu:22.04\nRUN echo hello\n"
        meta = annotate_dockerfile(text, "Dockerfile")
        run_section = next(s for s in meta.sections if "RUN" in s.title)
        assert "echo hello" in run_section.title


class TestDockerfileAllInstructions:
    def test_expose_instruction(self):
        text = "FROM python:3.12\nEXPOSE 8080\n"
        meta = annotate_dockerfile(text, "Dockerfile")
        expose = next(s for s in meta.sections if "EXPOSE" in s.title)
        assert expose.level == 2
        assert "8080" in expose.title

    def test_workdir_instruction(self):
        text = "FROM python:3.12\nWORKDIR /app\n"
        meta = annotate_dockerfile(text, "Dockerfile")
        wd = next(s for s in meta.sections if "WORKDIR" in s.title)
        assert wd.level == 2

    def test_cmd_instruction(self):
        text = 'FROM python:3.12\nCMD ["python", "app.py"]\n'
        meta = annotate_dockerfile(text, "Dockerfile")
        cmd = next(s for s in meta.sections if "CMD" in s.title)
        assert cmd.level == 2

    def test_entrypoint_instruction(self):
        text = 'FROM python:3.12\nENTRYPOINT ["/entrypoint.sh"]\n'
        meta = annotate_dockerfile(text, "Dockerfile")
        ep = next(s for s in meta.sections if "ENTRYPOINT" in s.title)
        assert ep.level == 2

    def test_no_from_instructions_still_parses(self):
        """Edge case: Dockerfile fragment without FROM."""
        text = "RUN apt-get update\nEXPOSE 80\n"
        meta = annotate_dockerfile(text, "Dockerfile")
        # Should still parse instructions at level 2 (or level 1 as fallback)
        assert len(meta.sections) >= 1

    def test_empty_dockerfile(self):
        meta = annotate_dockerfile("", "Dockerfile")
        assert meta.sections == []
        assert meta.total_lines == 0
