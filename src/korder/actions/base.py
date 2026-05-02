"""Action dataclass + module-level registry."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Callable

# Op tuple type. See actions/__init__.py for shape documentation.
Op = tuple


@dataclass(frozen=True)
class Action:
    """A registered action.

    name: stable identifier (snake_case) — referenced by the LLM
    description: short English description shown to the LLM
    triggers: locale → trigger phrases (case-insensitive substring match in
              regex parser; informational examples for the LLM prompt)
    op_factory: produces the (kind, value) op tuple to execute. Takes a
                params dict so future parameterized actions (volume_set,
                workspace_switch) can pass values through.

    Actions that produce spoken output (issue #2) opt in by calling
    ``emit_progress_speak(text, lang)`` from inside their callable —
    mirrors the existing ``emit_progress`` pattern. No flag on the
    Action itself; whether-to-speak is part of the action's runtime
    behavior, not its registration metadata.
    """
    name: str
    description: str
    triggers: dict[str, list[str]]
    op_factory: Callable[[dict], Op]
    parameters: dict = field(default_factory=dict)  # JSON-schema for params, future use

    def all_triggers(self) -> list[str]:
        out: list[str] = []
        for phrases in self.triggers.values():
            out.extend(phrases)
        return out


_REGISTRY: dict[str, Action] = {}


def register(action: Action) -> Action:
    if action.name in _REGISTRY:
        raise ValueError(f"action {action.name!r} already registered")
    _REGISTRY[action.name] = action
    return action


def get_action(name: str) -> Action | None:
    return _REGISTRY.get(name)


def all_actions() -> list[Action]:
    return list(_REGISTRY.values())


def trigger_phrase_map() -> dict[str, str]:
    """Returns lowercased trigger phrase → action name. Used by the regex
    parser to compile a union pattern. Raises if two actions claim the same
    phrase (would be a bug in registration)."""
    out: dict[str, str] = {}
    for action in _REGISTRY.values():
        for phrase in action.all_triggers():
            key = phrase.lower()
            if key in out and out[key] != action.name:
                raise ValueError(
                    f"trigger {phrase!r} claimed by both {out[key]!r} and {action.name!r}"
                )
            out[key] = action.name
    return out


def reset() -> None:
    """Clears the registry. For tests only."""
    _REGISTRY.clear()
