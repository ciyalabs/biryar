"""Everything the package imports is something it declares.

A development install pulls the dev extras, so a dependency listed only
there is present on every machine anyone writes code on — and absent from
the built image, which installs the runtime set alone. The failure then
appears for the first time in a container, as an ImportError in a traceback
nobody was looking at.

That happened with httpx: `biryar.amazon` has always imported it, it was
declared under dev, and the first thing to notice was a container exiting
with empty logs.

Relying on a transitive dependency fails the same way, later and more
confusingly: FastAPI could drop something tomorrow and this package would
stop importing for a reason that appears nowhere in its own metadata.
"""

from __future__ import annotations

import ast
import pathlib
import sys
import tomllib
from importlib.metadata import packages_distributions

ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "biryar"


def _declared() -> set[str]:
    """Distribution names in [project].dependencies, normalised."""
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
    names = set()
    for requirement in metadata["project"]["dependencies"]:
        # "pydantic[email]>=2.10" -> "pydantic"
        name = requirement.split(">=")[0].split("==")[0].split("[")[0]
        names.add(name.strip().lower().replace("_", "-"))
    return names


def _imported() -> set[str]:
    """Top-level module names the package imports."""
    names: set[str] = set()
    for path in PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            # level > 0 is a relative import: our own code.
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names


def test_every_import_is_a_declared_dependency() -> None:
    declared = _declared()
    distributions = packages_distributions()

    undeclared: dict[str, list[str]] = {}
    for module in sorted(_imported()):
        if module == "biryar" or module in sys.stdlib_module_names:
            continue
        providers = [
            name.lower().replace("_", "-") for name in distributions.get(module, [])
        ]
        if not any(provider in declared for provider in providers):
            undeclared[module] = providers

    assert not undeclared, (
        "these are imported at runtime but not declared in "
        f"[project].dependencies: {undeclared}. A dev-only or transitive "
        "dependency is present in every development install and absent from "
        "the built image."
    )


def test_the_package_ships_what_the_wheel_needs() -> None:
    """pyproject names a licence file, so the build reads it.

    Left out of a build context the wheel fails with 'License file does not
    exist', which names the missing file rather than the metadata asking for
    it. The Dockerfile has to copy it; this is the reminder of why.
    """
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
    licence = metadata["project"].get("license")
    if isinstance(licence, dict) and "file" in licence:
        referenced = ROOT / str(licence["file"])
        assert referenced.is_file(), f"pyproject names {referenced.name}, which is missing"

        # Specifically a COPY, not merely the word somewhere in the file.
        # The first version of this checked for the name anywhere, and the
        # comment explaining why the file matters was enough to satisfy it —
        # so it passed against the exact bug it was written for.
        copied = [
            line
            for line in (ROOT / "Dockerfile").read_text().splitlines()
            if line.strip().upper().startswith("COPY")
            and referenced.name in line
        ]
        assert copied, (
            f"pyproject's licence file {referenced.name} is not COPYed into "
            "the image, so the wheel build inside it will fail with "
            "'License file does not exist'"
        )
