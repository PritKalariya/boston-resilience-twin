"""The template's one module: a function the tests cover fully."""

from __future__ import annotations


def describe(component: str) -> str:
    """Return the line the template prints for a component folder name."""
    name = component.strip()
    if not name:
        raise ValueError("component name is empty")
    return f"component {name} is wired into CI"
