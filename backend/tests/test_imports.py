"""Every module in ``app/`` must be importable.

Trivially cheap, and it catches the one failure mode that is otherwise invisible
until the whole app refuses to start: a module that is missing, renamed or
misplaced. The package graph is deep (``app.server.app`` -> ``http_routes`` ->
``asr.factory`` -> ``asr.local``), so a single absent file takes down the server,
the tests and the preflight at once - with a ``ModuleNotFoundError`` that names
the leaf, not the cause.

Walking the directory rather than listing modules by hand also means a new file
is covered the moment it is added.
"""

from __future__ import annotations

import hashlib
import importlib
import pkgutil
from pathlib import Path

import pytest

import app

APP_DIR = Path(app.__file__).resolve().parent


def _discover_modules() -> list[str]:
    names = [module.name for module in pkgutil.walk_packages(app.__path__, prefix="app.")]
    # `pkgutil` only reports real packages when the directory is importable in
    # its own right; walking the filesystem is the belt-and-braces version.
    for path in sorted(APP_DIR.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        parts = list(path.relative_to(APP_DIR).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        name = ".".join(["app", *parts])
        if name not in names:
            names.append(name)
    return sorted(set(names))


MODULES = _discover_modules()


def test_the_module_list_is_not_trivially_empty():
    # Guards the guard: if discovery broke, the parametrised test would pass
    # vacuously.
    assert len(MODULES) >= 30
    assert "app.asr.local" in MODULES
    assert "app.server.websocket" in MODULES


@pytest.mark.parametrize("module_name", MODULES)
def test_module_imports(module_name: str):
    assert importlib.import_module(module_name) is not None


def test_no_module_content_is_duplicated_across_packages():
    """A file written to the wrong package is a bug that imports *cleanly*.

    Every module belongs to exactly one package, and the source of a misplaced
    module is character-identical to its correct copy - so nothing fails until
    someone deletes the real one. Comparing the source of every module against
    every other module in a different package catches the copy at rest.
    """
    seen: dict[str, Path] = {}
    duplicates: list[tuple[Path, Path]] = []
    for path in sorted(APP_DIR.rglob("*.py")):
        if "__pycache__" in path.parts or path.name == "__init__.py":
            continue
        source = path.read_bytes()
        if len(source) < 200:  # a stub is not evidence of anything
            continue
        digest = hashlib.sha256(source).hexdigest()
        if digest in seen:
            duplicates.append((seen[digest], path))
        else:
            seen[digest] = path
    assert not duplicates, f"identical module sources in two packages: {duplicates}"
