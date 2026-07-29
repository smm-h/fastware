"""Custom selfdoc directive: generate a markdown table of optional extras from pyproject.toml."""

from __future__ import annotations

import os
import re


# Descriptions inferred from extra name and its dependencies.
_EXTRA_DESCRIPTIONS = {
    "auth": "JWT authentication and bcrypt password hashing",
    "logging": "Structured logging via structlog",
    "dev": "Hot reload, Vite dev proxy, WebSocket proxy",
    "testing": "Async and sync test clients via httpx",
    "mcp": "MCP (Model Context Protocol) server support",
    "pydantic": "Pydantic model validation and serialization",
    "all": "All optional extras combined",
}


def _parse_optional_dependencies(text: str) -> dict[str, list[str]]:
    """Parse [project.optional-dependencies] from pyproject.toml content."""
    extras: dict[str, list[str]] = {}
    in_section = False
    current_extra: str | None = None
    in_array = False
    current_deps: list[str] = []

    for line in text.splitlines():
        stripped = line.strip()

        if stripped == "[project.optional-dependencies]":
            in_section = True
            continue

        # Another top-level section starts -- stop parsing.
        if in_section and re.match(r"^\[(?!project\.optional-dependencies)", stripped):
            if current_extra is not None and current_deps:
                extras[current_extra] = current_deps
            break

        if not in_section:
            continue

        # Key = [...] or key = [single-line]
        m = re.match(r'^(\w[\w-]*)\s*=\s*\[', stripped)
        if m:
            # Save previous extra if any.
            if current_extra is not None and current_deps:
                extras[current_extra] = current_deps

            current_extra = m.group(1)
            current_deps = []

            # Check for single-line array: key = ["a", "b"]
            if "]" in stripped:
                in_array = False
                deps = re.findall(r'"([^"]+)"', stripped)
                current_deps = deps
                extras[current_extra] = current_deps
                current_extra = None
                current_deps = []
            else:
                in_array = True
            continue

        if in_array:
            if stripped == "]":
                in_array = False
                if current_extra is not None:
                    extras[current_extra] = current_deps
                    current_extra = None
                    current_deps = []
                continue
            # Parse quoted dependency strings.
            dep_match = re.match(r'^"([^"]+)"', stripped)
            if dep_match:
                current_deps.append(dep_match.group(1))

    return extras


def _dep_name(dep: str) -> str:
    """Extract the bare package name from a dependency string like 'httpx>=0.28'."""
    return re.split(r"[>=<\[!~;]", dep)[0].strip()


def resolve(attrs: dict, config: dict, body: list[str]) -> str:
    """Generate a markdown table of optional extras."""
    project_root = config.get("_project_root", ".")
    toml_path = os.path.join(project_root, "pyproject.toml")

    if not os.path.isfile(toml_path):
        return "<!-- selfdoc: pyproject.toml not found -->"

    with open(toml_path, encoding="utf-8") as f:
        content = f.read()

    extras = _parse_optional_dependencies(content)
    if not extras:
        return "<!-- selfdoc: no optional-dependencies found -->"

    lines = ["| Extra | Dependencies | Description |", "|-------|-------------|-------------|"]
    for name, deps in extras.items():
        dep_names = ", ".join(f"`{_dep_name(d)}`" for d in deps)
        description = _EXTRA_DESCRIPTIONS.get(name, "")
        lines.append(f"| `[{name}]` | {dep_names} | {description} |")

    return "\n".join(lines)
