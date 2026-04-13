"""Tests for the Docker analyzer."""

from token_savior.docker_analyzer import analyze_docker
from token_savior.dockerfile_annotator import annotate_dockerfile
from token_savior.models import ProjectIndex, StructuralMetadata


def _make_index(
    files: dict[str, StructuralMetadata], root_path: str = "/fake/project"
) -> ProjectIndex:
    return ProjectIndex(
        root_path=root_path,
        files=files,
        total_files=len(files),
        total_lines=sum(m.total_lines for m in files.values()),
    )


def _dockerfile_meta(text: str, name: str = "Dockerfile") -> StructuralMetadata:
    return annotate_dockerfile(text, name)


class TestNoDockerfiles:
    def test_empty_index(self):
        index = _make_index({})
        result = analyze_docker(index)
        assert result == "Docker Analysis -- no Dockerfiles or compose files found in project"

    def test_index_with_only_python_files(self):
        meta = StructuralMetadata(
            source_name="/fake/project/app.py",
            total_lines=3,
            total_chars=30,
            lines=["x = 1", "y = 2", "z = 3"],
            line_char_offsets=[0, 6, 12],
        )
        index = _make_index({"/fake/project/app.py": meta})
        result = analyze_docker(index)
        assert result == "Docker Analysis -- no Dockerfiles or compose files found in project"


class TestDockerfileFound:
    def test_header_shows_count(self):
        meta = _dockerfile_meta("FROM python:3.12\n", "/fake/project/Dockerfile")
        index = _make_index({"/fake/project/Dockerfile": meta})
        result = analyze_docker(index)
        assert "Docker Analysis -- Found 1 Dockerfile(s), 0 compose file(s)" in result

    def test_base_image_listed(self):
        meta = _dockerfile_meta("FROM python:3.12-slim\n", "/fake/project/Dockerfile")
        index = _make_index({"/fake/project/Dockerfile": meta})
        result = analyze_docker(index)
        assert "python:3.12-slim" in result

    def test_exposed_ports_listed(self):
        text = "FROM python:3.12\nEXPOSE 8080\n"
        meta = _dockerfile_meta(text, "/fake/project/Dockerfile")
        index = _make_index({"/fake/project/Dockerfile": meta})
        result = analyze_docker(index)
        assert "8080" in result

    def test_env_vars_listed(self):
        text = "FROM python:3.12\nENV APP_ENV=production\n"
        meta = _dockerfile_meta(text, "/fake/project/Dockerfile")
        index = _make_index({"/fake/project/Dockerfile": meta})
        result = analyze_docker(index)
        assert "APP_ENV" in result


class TestLatestTagWarning:
    def test_latest_tag_produces_warning(self):
        text = "FROM node:latest\n"
        meta = _dockerfile_meta(text, "/fake/project/Dockerfile")
        index = _make_index({"/fake/project/Dockerfile": meta})
        result = analyze_docker(index)
        assert "[warning]" in result
        assert "latest" in result

    def test_pinned_tag_no_warning(self):
        text = "FROM python:3.12-slim\n"
        meta = _dockerfile_meta(text, "/fake/project/Dockerfile")
        index = _make_index({"/fake/project/Dockerfile": meta})
        result = analyze_docker(index)
        # No warning about latest for pinned images
        assert "latest" not in result or "[warning]" not in result

    def test_explicit_latest_in_image_name(self):
        text = "FROM ubuntu:latest\n"
        meta = _dockerfile_meta(text, "/fake/project/Dockerfile")
        index = _make_index({"/fake/project/Dockerfile": meta})
        result = analyze_docker(index)
        assert "[warning]" in result


class TestMultiStage:
    def test_multi_stage_stages_shown(self):
        text = "FROM python:3.12 AS builder\nRUN pip install\nFROM python:3.12-slim AS runtime\nCOPY --from=builder /app /app\n"
        meta = _dockerfile_meta(text, "/fake/project/Dockerfile")
        index = _make_index({"/fake/project/Dockerfile": meta})
        result = analyze_docker(index)
        assert "builder" in result
        assert "runtime" in result

    def test_multi_stage_count(self):
        text = "FROM alpine:3 AS base\nFROM alpine:3 AS final\n"
        meta = _dockerfile_meta(text, "/fake/project/Dockerfile")
        index = _make_index({"/fake/project/Dockerfile": meta})
        result = analyze_docker(index)
        assert "Stages: 2" in result


class TestDockerfileNamed:
    def test_dockerfile_dev_detected(self):
        meta = _dockerfile_meta("FROM python:3.12\n", "/fake/project/Dockerfile.dev")
        index = _make_index({"/fake/project/Dockerfile.dev": meta})
        result = analyze_docker(index)
        assert "Docker Analysis -- Found 1 Dockerfile(s), 0 compose file(s)" in result

    def test_backend_dockerfile_detected(self):
        meta = _dockerfile_meta("FROM python:3.12\n", "/fake/project/backend.dockerfile")
        index = _make_index({"/fake/project/backend.dockerfile": meta})
        result = analyze_docker(index)
        assert "Docker Analysis -- Found 1 Dockerfile(s), 0 compose file(s)" in result

    def test_relative_paths_do_not_gain_parent_prefixes(self):
        compose_meta = StructuralMetadata(
            source_name="deployment/docker-compose.local.yml",
            total_lines=2,
            total_chars=20,
            lines=["services:", "  api:"],
            line_char_offsets=[0, 10],
        )
        compose_meta.sections = []  # type: ignore[attr-defined]
        index = _make_index(
            {"deployment/docker-compose.local.yml": compose_meta},
            root_path="/fake/project",
        )
        result = analyze_docker(index)
        assert "deployment/docker-compose.local.yml:" in result
        assert "../../" not in result


class TestCopySources:
    def test_copy_source_exists_info(self, tmp_path):
        # Create a real source directory
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        dockerfile = tmp_path / "Dockerfile"

        text = "FROM python:3.12\nCOPY src/ /app/\n"
        meta = _dockerfile_meta(text, str(dockerfile))
        index = _make_index({str(dockerfile): meta}, root_path=str(tmp_path))
        result = analyze_docker(index)
        assert "[info]" in result
        assert "exists" in result

    def test_copy_source_not_found_warning(self, tmp_path):
        dockerfile = tmp_path / "Dockerfile"
        text = "FROM python:3.12\nCOPY nonexistent_dir/ /app/\n"
        meta = _dockerfile_meta(text, str(dockerfile))
        index = _make_index({str(dockerfile): meta}, root_path=str(tmp_path))
        result = analyze_docker(index)
        assert "[warning]" in result
        assert "NOT FOUND" in result

    def test_copy_with_from_flag_skips_source_check(self, tmp_path):
        """COPY --from=builder src should extract src correctly."""
        dockerfile = tmp_path / "Dockerfile"
        text = "FROM python:3.12\nCOPY --from=builder /app /final\n"
        meta = _dockerfile_meta(text, str(dockerfile))
        index = _make_index({str(dockerfile): meta}, root_path=str(tmp_path))
        result = analyze_docker(index)
        # /app is absolute, won't exist on tmp_path but that's expected
        # Just check it ran without error
        assert "Docker Analysis" in result

    def test_java_code_reference_suppresses_missing_env_warning(self, tmp_path):
        dockerfile = tmp_path / "Dockerfile"
        text = "FROM eclipse-temurin:21\nENV APP_PORT=8080\n"
        docker_meta = _dockerfile_meta(text, str(dockerfile))
        java_meta = StructuralMetadata(
            source_name=str(tmp_path / "src/Main.java"),
            total_lines=1,
            total_chars=34,
            lines=['System.getenv("APP_PORT");'],
            line_char_offsets=[0],
        )
        index = _make_index(
            {
                str(dockerfile): docker_meta,
                str(tmp_path / "src/Main.java"): java_meta,
            },
            root_path=str(tmp_path),
        )
        result = analyze_docker(index)
        assert "APP_PORT" in result
        assert "not found in any .env file" not in result
