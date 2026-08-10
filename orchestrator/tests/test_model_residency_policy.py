"""Residency policy is set centrally, not by individual callers.

Ollama's `keep_alive` is per-request and *last write wins*: any call that
sends one silently overrides both the server's OLLAMA_KEEP_ALIVE and an
operator's REASONER_KEEP_ALIVE=-1 pin.

Several feature modules used to pass short values (60s, 120s). Because they
run against the reasoner, a single background run would un-pin the 24 GB MoE
and schedule its eviction a minute later, leaving the next real request to
pay a multi-minute cold load. This test pins that lesson down.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# reflector deliberately uses keep_alive=0 to *unload* a model and free VRAM
# before the 35B loads. That is an explicit lifecycle action, not a residency
# policy opinion, so it stays exempt.
_EXEMPT = {"reflector.py"}

_ORCHESTRATOR = Path(__file__).resolve().parent.parent


def _keep_alive_calls(path: Path) -> list[tuple[int, str]]:
    """Line numbers and rendered values of any keep_alive a file sends.

    Covers both spellings: the ``keep_alive=`` kwarg and a ``"keep_alive"``
    key in a payload dict, since reflector uses the latter and a kwarg-only
    check would miss it.
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "keep_alive":
                    found.append((node.lineno, ast.unparse(kw.value)))
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == "keep_alive":
                    found.append((key.lineno, ast.unparse(value)))
    return found


@pytest.mark.parametrize(
    "module", ["health_goals.py", "engagement.py", "goals_chat.py"]
)
def test_feature_modules_do_not_override_keep_alive(module: str) -> None:
    """These run against the reasoner, so any keep_alive here un-pins it."""
    offenders = _keep_alive_calls(_ORCHESTRATOR / module)
    assert offenders == [], (
        f"{module} passes keep_alive at {offenders}. This overrides "
        "REASONER_KEEP_ALIVE=-1 and evicts the reasoner from VRAM. Let the "
        "server's policy apply instead."
    )


def test_no_new_keep_alive_overrides_appear_in_orchestrator() -> None:
    """Catch the same mistake in modules that don't exist yet."""
    offenders: dict[str, list[tuple[int, str]]] = {}
    for path in _ORCHESTRATOR.glob("*.py"):
        if path.name in _EXEMPT:
            continue
        calls = [
            (line, value)
            for line, value in _keep_alive_calls(path)
            # app.py owns residency policy: the pin and the operator override.
            if not (path.name == "app.py" and "KEEP_ALIVE" in value)
        ]
        if calls:
            offenders[path.name] = calls

    assert offenders == {}, (
        f"Unexpected keep_alive overrides: {offenders}. Residency is owned by "
        "the warmer in app.py and by OLLAMA_KEEP_ALIVE on the server."
    )
