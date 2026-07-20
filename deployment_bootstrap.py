"""Protect in-process deployments from stale project modules."""

from __future__ import annotations

import sys
from collections.abc import Iterable, MutableMapping
from pathlib import Path
from types import ModuleType
from typing import Any


_RELEASE_STATE_ATTRIBUTE = "_codex_weather_brief_loaded_release"


def refresh_repo_modules_for_release(
    *,
    repo_root: str | Path,
    release: str,
    protected_paths: Iterable[str | Path] = (),
    modules: MutableMapping[str, ModuleType] | None = None,
    state: Any = None,
) -> tuple[str, ...]:
    """Evict cached repo modules when a reused process crosses a release boundary."""

    module_registry = sys.modules if modules is None else modules
    state_holder = sys if state is None else state
    previous_release = getattr(state_holder, _RELEASE_STATE_ATTRIBUTE, None)
    setattr(state_holder, _RELEASE_STATE_ATTRIBUTE, release)

    # A missing marker identifies a clean first boot or the first deployment of
    # this safeguard. Module-specific compatibility guards cover that one-time
    # transition; every later version change is handled generically here.
    if previous_release is None or previous_release == release:
        return ()

    root = Path(repo_root).resolve()
    protected = {Path(__file__).resolve()}
    protected.update(Path(path).resolve() for path in protected_paths)
    evicted: list[str] = []

    for module_name, module in tuple(module_registry.items()):
        module_file = getattr(module, "__file__", None)
        if not module_file:
            continue
        try:
            module_path = Path(module_file).resolve()
        except (OSError, RuntimeError, TypeError, ValueError):
            continue
        if module_path in protected:
            continue
        if module_path.parent != root and root not in module_path.parents:
            continue
        module_registry.pop(module_name, None)
        evicted.append(module_name)

    return tuple(sorted(evicted))
