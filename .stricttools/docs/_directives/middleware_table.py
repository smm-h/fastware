"""Custom selfdoc directive: generate a markdown table of middleware classes from middleware.py."""

from __future__ import annotations

import os
import re


# Maps middleware class name to (purpose, create_app parameter).
_MIDDLEWARE_INFO = {
    "RequestIDMiddleware": (
        "Assigns or propagates a unique X-Request-Id per request",
        "`request_id`",
    ),
    "RequestTimingMiddleware": (
        "Logs method, path, status, and duration; ring buffer for recent requests",
        "`request_timing`",
    ),
    "CORSMiddleware": (
        "Preflight OPTIONS handling and CORS response header injection",
        "`cors_origins`",
    ),
    "TrustedHostMiddleware": (
        "Rejects requests from unlisted Host headers (DNS rebinding protection)",
        "`trusted_hosts`",
    ),
    "ViteDevProxy": (
        "Proxies unmatched requests to a Vite dev server (backend-first routing)",
        "`vite_dev_port`",
    ),
}


def resolve(attrs: dict, config: dict, body: list[str]) -> str:
    """Generate a markdown table of middleware classes."""
    project_root = config.get("_project_root", ".")
    middleware_path = os.path.join(project_root, "src", "fastware", "middleware.py")

    if not os.path.isfile(middleware_path):
        return "<!-- selfdoc: middleware.py not found -->"

    with open(middleware_path, encoding="utf-8") as f:
        content = f.read()

    # Find all class definitions in the file.
    classes = re.findall(r"^class\s+(\w+)", content, re.MULTILINE)
    if not classes:
        return "<!-- selfdoc: no middleware classes found -->"

    lines = [
        "| Middleware | Purpose | `create_app` Parameter |",
        "|-----------|---------|----------------------|",
    ]
    for cls in classes:
        info = _MIDDLEWARE_INFO.get(cls)
        if info:
            purpose, param = info
        else:
            purpose = ""
            param = ""
        lines.append(f"| `{cls}` | {purpose} | {param} |")

    return "\n".join(lines)
