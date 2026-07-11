"""Framework browser assets (service workers, registration snippet, update
client) shipped as package data and rendered with per-app substitutions.

Templates live under ``fastware/_assets/`` and are loaded via
``importlib.resources`` so they resolve from the installed distribution rather
than a source-tree-relative path. Rendering is plain ``{{TOKEN}}`` replacement
-- never f-strings -- so the JavaScript templates stay readable and lintable and
``app.py`` stays free of embedded JS.
"""

from __future__ import annotations

import importlib.resources
import re
from typing import Mapping

__all__ = ["load_template", "render_asset"]

_PACKAGE = "fastware"
_ASSET_DIR = "_assets"
_PLACEHOLDER_RE = re.compile(r"\{\{[A-Z0-9_]+\}\}")


def load_template(name: str) -> str:
    """Return the raw text of a template shipped as package data.

    Resolves via :func:`importlib.resources.files` so it works from an installed
    wheel, a zipapp, or an editable checkout -- never a ``__file__``-relative
    path.
    """
    resource = importlib.resources.files(_PACKAGE).joinpath(_ASSET_DIR, name)
    return resource.read_text(encoding="utf-8")


def render_asset(name: str, substitutions: Mapping[str, str]) -> str:
    """Render template *name*, replacing every ``{{TOKEN}}`` with its value.

    Raises :class:`KeyError` if the rendered text still contains an unresolved
    ``{{TOKEN}}`` placeholder -- a typo guard so a missing substitution fails
    loudly at generation time instead of shipping broken JavaScript.
    """
    text = load_template(name)
    for key, value in substitutions.items():
        text = text.replace("{{" + key + "}}", value)
    leftover = _PLACEHOLDER_RE.search(text)
    if leftover is not None:
        raise KeyError(
            f"unresolved placeholder {leftover.group(0)} in asset {name!r}"
        )
    return text
