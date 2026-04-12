"""Analyzer for Dockerfile and docker-compose files in a project index."""

from __future__ import annotations

import os
import re

from token_savior.models import ProjectIndex, StructuralMetadata

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_EXPOSE_RE = re.compile(r"\bEXPOSE\b", re.IGNORECASE)
_PORT_NUMBER_RE = re.compile(r"\b(\d{2,5})\b")

# Patterns used to detect ENV/ARG variable names from section titles
_ENV_TITLE_RE = re.compile(r"^(?:ENV|ARG)\s+([A-Z_][A-Z0-9_]*)", re.IGNORECASE)

# Pattern for docker-compose port mappings like "3000:3000" or "80:8080"
_COMPOSE_PORT_RE = re.compile(r"(\d{2,5}):\d{2,5}")


def _is_dockerfile(path: str) -> bool:
    """Return True if *path* looks like a Dockerfile."""
    basename = os.path.basename(path)
    # Matches: Dockerfile, Dockerfile.dev, backend.dockerfile, etc.
    return (
        basename == "Dockerfile"
        or basename.startswith("Dockerfile.")
        or basename.lower().endswith(".dockerfile")
        or basename.lower() == "dockerfile"
    )


def _is_compose_file(path: str) -> bool:
    """Return True if *path* looks like a docker-compose file."""
    basename = os.path.basename(path).lower()
    return basename in ("docker-compose.yml", "docker-compose.yaml") or basename.startswith(
        "docker-compose."
    )


def _extract_base_images(meta: StructuralMetadata) -> list[str]:
    """Extract base image names from FROM sections (level 1)."""
    images = []
    for sec in meta.sections:
        if sec.level == 1 and sec.title.upper().startswith("FROM "):
            # e.g. "FROM python:3.12-slim AS builder" -> "python:3.12-slim"
            rest = sec.title[5:].strip()  # strip "FROM "
            # Take the first token (image name), ignore " AS <alias>"
            image = rest.split()[0] if rest else rest
            images.append(image)
    return images


def _extract_stages(meta: StructuralMetadata) -> list[str]:
    """Extract stage aliases from FROM instructions (multi-stage)."""
    stages = []
    for sec in meta.sections:
        if sec.level == 1 and sec.title.upper().startswith("FROM "):
            rest = sec.title[5:].strip()
            tokens = rest.split()
            # "FROM image AS alias" -> alias at index 2 after "AS" keyword
            if len(tokens) >= 3 and tokens[1].upper() == "AS":
                stages.append(tokens[2])
    return stages


def _extract_exposed_ports(meta: StructuralMetadata) -> list[str]:
    """Extract ports from EXPOSE sections."""
    ports = []
    for sec in meta.sections:
        if sec.title.upper().startswith("EXPOSE "):
            value = sec.title[7:].strip()
            # EXPOSE can list multiple ports: "EXPOSE 80 443 8080"
            for port in value.split():
                ports.append(port.strip())
    return ports


def _extract_env_arg_vars(meta: StructuralMetadata) -> list[str]:
    """Extract ENV/ARG variable names from sections."""
    vars_: list[str] = []
    for sec in meta.sections:
        m = _ENV_TITLE_RE.match(sec.title)
        if m:
            vars_.append(m.group(1))
    return vars_


def _extract_copy_sources(meta: StructuralMetadata) -> list[str]:
    """Extract source paths from COPY/ADD sections."""
    sources = []
    for sec in meta.sections:
        title = sec.title
        if title.upper().startswith("COPY ") or title.upper().startswith("ADD "):
            # Strip instruction, split tokens, first token is source
            # Handle --from= flag: "COPY --from=builder /app /dest"
            rest = title.split(None, 1)[1] if " " in title else ""
            tokens = rest.split()
            # Skip flags like --from=...
            non_flag_tokens = [t for t in tokens if not t.startswith("--")]
            if len(non_flag_tokens) >= 2:
                sources.append(non_flag_tokens[0])
    return sources


def _check_path_exists(source: str, root_path: str) -> bool:
    """Check if *source* (relative or absolute) exists under *root_path*."""
    if os.path.isabs(source):
        return os.path.exists(source)
    return os.path.exists(os.path.join(root_path, source))


def _display_path(path: str, root_path: str) -> str:
    """Return a project-relative path without inventing ../../ prefixes."""
    if not path:
        return path
    if os.path.isabs(path):
        try:
            return os.path.relpath(path, root_path) if root_path else path
        except ValueError:
            return path
    return path


def _find_env_files(index: ProjectIndex) -> dict[str, StructuralMetadata]:
    """Return all .env files from the index."""
    return {
        path: meta
        for path, meta in index.files.items()
        if os.path.basename(path) == ".env"
        or os.path.basename(path).startswith(".env.")
        or path.endswith(".env")
    }


def _env_file_keys(env_files: dict[str, StructuralMetadata]) -> set[str]:
    """Collect all variable names defined in .env files."""
    keys: set[str] = set()
    for meta in env_files.values():
        for sec in meta.sections:
            keys.add(sec.title.strip())
    return keys


def _code_contains(index: ProjectIndex, token: str) -> bool:
    """Return True if *token* appears in any line of any non-config file."""
    for path, meta in index.files.items():
        ext = os.path.splitext(path)[1].lower()
        if ext in (".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".cs", ".java"):
            for line in meta.lines:
                if token in line:
                    return True
    return False


# ---------------------------------------------------------------------------
# Compose helpers
# ---------------------------------------------------------------------------


def _extract_compose_services(meta: StructuralMetadata) -> list[str]:
    """Extract service names from docker-compose sections (level-2 under 'services')."""
    services: list[str] = []
    in_services = False
    for sec in meta.sections:
        if sec.title.strip() == "services":
            in_services = True
            continue
        if in_services:
            if sec.level == 2:
                services.append(sec.title.strip())
            elif sec.level == 1:
                # New top-level block — we're out of services
                in_services = False
    return services


def _extract_compose_ports(meta: StructuralMetadata) -> list[str]:
    """Extract host:container port mappings from a docker-compose metadata."""
    ports: list[str] = []
    for line in meta.lines:
        stripped = line.strip()
        # port entries look like:  - "3000:3000"  or  - 80:8080
        stripped = stripped.lstrip("- \"'").rstrip("\"'")
        m = _COMPOSE_PORT_RE.match(stripped)
        if m:
            ports.append(stripped)
    return ports


# ---------------------------------------------------------------------------
# Main analysis function
# ---------------------------------------------------------------------------


def analyze_docker(index: ProjectIndex) -> str:
    """Analyze Dockerfiles in *index* and return a formatted report.

    Checks:
    1. Exposed ports — list EXPOSE'd ports, cross-ref with code / .env
    2. ENV/ARG vars — cross-ref with .env files and code
    3. Base image info — flag `latest` tag usage
    4. COPY/ADD sources — check if source files/dirs exist in the project

    Returns a formatted string report.
    """
    dockerfile_files = {path: meta for path, meta in index.files.items() if _is_dockerfile(path)}
    compose_files = {path: meta for path, meta in index.files.items() if _is_compose_file(path)}

    total = len(dockerfile_files) + len(compose_files)

    if total == 0:
        return "Docker Analysis -- no Dockerfiles or compose files found in project"

    env_files = _find_env_files(index)
    env_keys = _env_file_keys(env_files)

    parts: list[str] = [
        "Docker Analysis -- "
        f"Found {len(dockerfile_files)} Dockerfile(s), {len(compose_files)} compose file(s)",
        "",
    ]

    # ------------------------------------------------------------------
    # Per-Dockerfile analysis
    # ------------------------------------------------------------------
    for path, meta in sorted(dockerfile_files.items()):
        rel_path = _display_path(path, index.root_path)
        parts.append(f"{rel_path}:")

        base_images = _extract_base_images(meta)
        stages = _extract_stages(meta)
        exposed_ports = _extract_exposed_ports(meta)
        env_vars = _extract_env_arg_vars(meta)
        copy_sources = _extract_copy_sources(meta)

        # Base image(s)
        if base_images:
            parts.append(f"  Base: {', '.join(base_images)}")

        # Stages (multi-stage)
        if stages:
            parts.append(f"  Stages: {len(base_images)} ({', '.join(stages)})")
        elif len(base_images) > 1:
            parts.append(f"  Stages: {len(base_images)} (unnamed)")

        # Exposed ports
        if exposed_ports:
            parts.append(f"  Exposed ports: {', '.join(exposed_ports)}")

        # ENV/ARG vars
        if env_vars:
            parts.append(f"  ENV/ARG vars: {', '.join(env_vars)}")

        # Issues
        issues: list[str] = []

        # Check for 'latest' tag
        for image in base_images:
            if image.endswith(":latest") or (":" not in image and "/" not in image):
                issues.append(f"[warning] Using 'latest' tag for base image {image}")

        # ENV/ARG vars not found in .env files
        for var in env_vars:
            if env_keys and var not in env_keys:
                if not _code_contains(index, var):
                    issues.append(f"[warning] ENV/ARG var '{var}' not found in any .env file")

        # COPY/ADD source existence
        for src in copy_sources:
            # Skip URL sources or glob-like sources
            if src.startswith("http://") or src.startswith("https://"):
                continue
            if "*" in src or "?" in src:
                continue
            exists = _check_path_exists(src, index.root_path)
            status = "exists \u2713" if exists else "NOT FOUND"
            severity = "info" if exists else "warning"
            issues.append(f"[{severity}] COPY/ADD src '{src}' -- {status}")

        if issues:
            parts.append("  Issues:")
            for issue in issues:
                parts.append(f"    {issue}")

        parts.append("")

    # ------------------------------------------------------------------
    # Per-docker-compose analysis
    # ------------------------------------------------------------------
    for path, meta in sorted(compose_files.items()):
        rel_path = _display_path(path, index.root_path)
        parts.append(f"{rel_path}:")

        services = _extract_compose_services(meta)
        ports = _extract_compose_ports(meta)

        if services:
            parts.append(f"  Services: {', '.join(services)}")
        if ports:
            parts.append(f"  Ports: {', '.join(ports)}")

        parts.append("")

    # Remove trailing blank line
    while parts and parts[-1] == "":
        parts.pop()

    return "\n".join(parts)
