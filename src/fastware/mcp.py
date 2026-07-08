"""MCP (Model Context Protocol) server factory providing role-based agent tool provisioning, tool filtering, and stdio-based server lifecycle management.

Provides role-filtered tool registration and server creation for MCP agent
processes. The role/tool domain model belongs to the caller: both
``create_mcp_server`` and ``register_tools_for_role`` take the *roles*
mapping explicitly -- the framework ships no built-in roles.

The ``mcp`` package is an optional dependency -- this module is importable
without it (and never imports it at module level), but ``create_mcp_server``
raises a clear error if the package is not installed.
"""

from __future__ import annotations

from collections.abc import Callable
from importlib.util import find_spec
from types import ModuleType
from typing import Any

# Cheap availability probe: find_spec does NOT execute the (heavy) mcp
# package. create_mcp_server checks this module-level flag, so tests can
# monkeypatch it to simulate a missing installation.
_MCP_AVAILABLE = find_spec("mcp") is not None

__all__ = [
    "create_mcp_server",
    "register_tools_for_role",
]

# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


def register_tools_for_role(
    server: Any,
    role: str,
    tool_modules: list[ModuleType],
    *,
    roles: dict[str, dict[str, Any]],
    default_role: str | None = None,
) -> dict[str, Callable[..., Any]]:
    """Register only the tools allowed by *role* on *server*.

    Each module in *tool_modules* must define a ``TOOLS`` dict mapping
    tool name to the callable. Only tools listed in the role's config
    are registered.

    Args:
        roles: Role definitions dict mapping role name to a config dict
            with a ``"tools"`` list. Required -- the framework has no
            built-in role model.
        default_role: Fallback role used when *role* is not in *roles*.
            If None, an unknown *role* is a hard error.

    Returns the dict of registered tool name -> function.
    """
    if role in roles:
        role_config = roles[role]
    elif default_role is not None:
        role_config = roles[default_role]
    else:
        raise KeyError(
            f"Unknown role {role!r} and no default_role given. "
            f"Known roles: {sorted(roles)}"
        )
    allowed = set(role_config["tools"])

    registry: dict[str, Callable[..., Any]] = {}
    for mod in tool_modules:
        mod_tools: dict[str, Callable[..., Any]] = getattr(mod, "TOOLS", {})
        for name, fn in mod_tools.items():
            if name in allowed:
                server.add_tool(fn, name=name)
                registry[name] = fn

    return registry


# ---------------------------------------------------------------------------
# Server factory
# ---------------------------------------------------------------------------


def create_mcp_server(
    name: str = "fastware-agent",
    *,
    role: str | None = None,
    tool_modules: list[ModuleType] | None = None,
    roles: dict[str, dict[str, Any]] | None = None,
    default_role: str | None = None,
) -> Any:
    """Create a FastMCP server with role-filtered tools.

    Parameters
    ----------
    name:
        Server name passed to FastMCP.
    role:
        Agent role. If *None*, *default_role* is used (required in that
        case when *tool_modules* is given).
    tool_modules:
        List of modules that define a ``TOOLS`` dict. If *None*, no
        tools are registered (caller must register manually).
    roles:
        Role definitions dict. Required when *tool_modules* is given --
        the framework has no built-in role model.
    default_role:
        Fallback role used when *role* is None or unknown.

    Returns
    -------
    FastMCP
        The configured server instance.

    Raises
    ------
    RuntimeError
        If the ``mcp`` package is not installed.
    ValueError
        If *tool_modules* is given without *roles*, or without either
        *role* or *default_role*.
    """
    if not _MCP_AVAILABLE:
        raise RuntimeError(
            "The 'mcp' package is required for MCP server support. "
            "Install it with: pip install mcp"
        )

    # Lazy import: the mcp package costs several hundred ms and is optional.
    from mcp.server.fastmcp import FastMCP

    server = FastMCP(name)

    if tool_modules is not None:
        if roles is None:
            raise ValueError(
                "roles is required when tool_modules is given -- the framework "
                "has no built-in role model; pass your application's roles dict."
            )
        if role is None:
            if default_role is None:
                raise ValueError(
                    "Either role or default_role must be given when "
                    "tool_modules is provided."
                )
            role = default_role
        register_tools_for_role(
            server, role, tool_modules, roles=roles, default_role=default_role
        )

    return server
