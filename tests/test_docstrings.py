"""Regression tests for corrected module docstrings."""

import fastware.types
import fastware.config
import fastware.__main__


def test_types_docstring_contains_asgi_types():
    """types.py docstring must name the types it defines (Scope, Receive, Send)."""
    doc = fastware.types.__doc__
    assert doc is not None
    assert "Scope" in doc, "docstring should mention Scope type"
    assert "Receive" in doc, "docstring should mention Receive type"
    assert "Send" in doc, "docstring should mention Send type"


def test_types_docstring_does_not_mention_non_existent_aliases():
    """types.py docstring must not mention ASGIApp or Middleware (aliases that don't exist)."""
    doc = fastware.types.__doc__
    assert doc is not None
    assert "ASGIApp" not in doc, "docstring should not mention non-existent ASGIApp alias"
    assert "Middleware" not in doc, "docstring should not mention non-existent Middleware alias"


def test_config_docstring_does_not_mention_environment_variables():
    """config.py docstring must not mention environment variable overrides (feature never implemented)."""
    doc = fastware.config.__doc__
    assert doc is not None
    # Check case-insensitive to catch various wordings
    doc_lower = doc.lower()
    assert "environment variable" not in doc_lower, (
        "config docstring should not mention environment variables "
        "(that feature was never implemented)"
    )


def test_main_docstring_exists_and_is_accurate():
    """__main__.py docstring should exist and not promise unimplemented CLI subcommands."""
    doc = fastware.__main__.__doc__
    assert doc is not None
    # Verify the docstring is accurate to what the CLI actually does
    # Currently a placeholder, so docstring should not advertise features
    assert "not yet implemented" in doc or "placeholder" in doc.lower() or (
        "python -m fastware" in doc and len(doc) < 100
    ), "docstring should be modest about CLI capabilities"
