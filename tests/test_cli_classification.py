"""The CLI's effect classification, pinned so a change has to be deliberate.

strictcli requires every command to declare ``effect="read_only"`` or
``effect="mutating"``; there is no default and a missing declaration is a
registration-time hard error. The classification answers exactly one question:
*should a dry run record this operation rather than perform it?*

Separately, a command may declare itself ``consequential``, which is what the
framework's confirm protocol keys on. It is NOT inferred from ``mutating`` --
that inference was measured at a ~1:10 signal-to-noise ratio across the fleet
and removed, because a guardrail that fires on two thirds of a CLI's commands
trains the reflex that hollows it out.

This file pins both tables in both directions. A new command shows up as an
unexpected entry; a reclassified one shows up as a mismatch. Either way the
edit has to come here, which is the point.
"""

from __future__ import annotations

from typing import Any

from fastware.cli import app

# All three of the dev-server commands mutate, and one of them is a surprise.
#
# `dev run` spawns the Vite dev server and the Granian backend as real child
# processes, writes a PID file, and registers the instance on disk -- which
# --daemon then leaves behind after detaching.
#
# `dev stop` sends SIGTERM (then SIGKILL after --grace) to every registered
# dev process and drops the corresponding registry entries.
#
# `dev status` is the surprise: listing the registry PRUNES it.
# `fastware.server.list_instances` unlinks every descriptor whose PID is no
# longer alive and every descriptor it cannot decode, so a plain `dev status`
# deletes files from disk. Nothing in its output says so. It is bookkeeping
# rather than anything the user asked for, and the argument for calling it
# read-only is real -- but it is still an unlink, and read_only is the
# classification that would make routing that unlink through
# `ctx.effects.remove` a hard error at call time. Over-classifying costs
# nothing here (a plain `mutating` command never prompts); under-classifying
# would have to be discovered later, by a failure.
EFFECTS = {
    "dev.run": "mutating",
    "dev.status": "mutating",
    "dev.stop": "mutating",
}

# Empty, deliberately.
#
# `consequential` means "this act is worth interrupting someone for". These
# three manage a local development environment on the developer's own machine:
# starting one is the point of `dev run`, and stopping the servers you just
# started is the point of `dev stop`. Nothing here touches production, a
# registry, or anything that outlives the session. `dev run` is additionally
# the kind of command that gets wrapped in a Makefile or a task runner, where
# an unanswerable prompt is a hang rather than a safety net.
CONSEQUENTIAL: set[str] = set()

# strictcli owns these four names at every level -- command flags, flag-set
# flags, mutex-group flags and app globals alike. `yes` names no framework flag
# any more (the skip flag is --approve-consequential) but stays banned so a
# consumer cannot restate it in the spelling the rename removed.
RESERVED_FLAG_NAMES = {
    "dry-run",
    "approve-consequential",
    "quiet",
    "verbose",
    "yes",
}


def _registry(container: Any) -> dict[str, Any]:
    """App stores commands on `_commands`, Group on `commands`.

    Written as an explicit None test rather than `or`, because an app whose
    commands all live in groups has an EMPTY `_commands` dict, and `or` would
    fall through it to an attribute App does not have.
    """
    found = getattr(container, "_commands", None)
    return container.commands if found is None else found


def _walk() -> dict[str, Any]:
    """Map dotted command path -> Command for every registered command."""
    found: dict[str, Any] = {}

    def visit(container: Any, prefix: str) -> None:
        for name, cmd in _registry(container).items():
            found[prefix + name] = cmd
        for name, group in container._groups.items():
            visit(group, prefix + name + ".")

    visit(app, "")
    return found


def test_every_command_is_classified_exactly_as_reviewed() -> None:
    declared = {path: cmd.effect for path, cmd in _walk().items()}
    assert declared == EFFECTS


def test_consequential_declarations_match_the_reviewed_set() -> None:
    """Both directions matter.

    A missing declaration removes a prompt somebody decided was owed; a stray
    one puts a blind ``Proceed? [y/N]`` in front of routine work and hangs
    every Makefile and task runner that calls it.
    """
    declared = {path for path, cmd in _walk().items() if cmd.consequential}
    assert declared == CONSEQUENTIAL


def test_no_command_redeclares_a_framework_reserved_flag_name() -> None:
    """A collision is a registration-time error, so reaching here means it built.

    Pinning the absence keeps a future flag from reintroducing one under a
    spelling that would break the CLI at import time.
    """
    assert not ({f.name for f in app._global_flags} & RESERVED_FLAG_NAMES)
    for path, cmd in _walk().items():
        names = {f.name for f in cmd.flags}
        collisions = names & RESERVED_FLAG_NAMES
        assert not collisions, f"'{path}' declares reserved flag(s) {sorted(collisions)}"


def test_no_handler_absorbs_kwargs_without_declaring_forwarding() -> None:
    """Guard v2 closed the `**kwargs` hole, and these three handlers were in it.

    All three took `**_kw` under the old framework and were rewritten to the
    ctx-first signature instead of claiming `forwarding=`. Declared forwarding
    waives the whole signature cross-check, which is worth having and is not
    worth spending on three handlers that name every flag they take.
    """
    for path, cmd in _walk().items():
        assert cmd.forwarding is None, f"'{path}' declares forwarding"


def test_the_reserved_quartet_reaches_the_context_not_the_handler(
    tmp_path, monkeypatch
) -> None:
    """--quiet is framework-owned and never lands in a handler's kwargs.

    Passing a quartet member must not become an unknown-flag error and must not
    be forwarded into the handler, which is what guard v2 would reject. With no
    [tool.fastware.dev] table present `dev status` exits 1 by design, and that
    is a handler-reached exit -- an unknown-flag error would never get there.
    """
    monkeypatch.chdir(tmp_path)
    result = app.test(["dev", "status", "--quiet"])
    assert result.exit_code == 1
    assert "[tool.fastware.dev]" in result.stderr
