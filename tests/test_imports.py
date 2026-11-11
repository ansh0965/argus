"""Every module must at least compile and import.

The daemon imports most of the package lazily, inside functions, so a syntax
error in a rarely used module (cli.py, say) would otherwise surface only when
someone runs that one command.
"""
import importlib
import py_compile
from pathlib import Path

import pytest

import argus

PKG = Path(argus.__file__).parent
MODULES = sorted(
    f"argus.{p.relative_to(PKG).with_suffix('').as_posix().replace('/', '.')}"
    for p in PKG.rglob("*.py") if p.name not in ("__init__.py", "__main__.py"))


@pytest.mark.parametrize("path", sorted(PKG.rglob("*.py")), ids=lambda p: p.name)
def test_compiles(path):
    py_compile.compile(str(path), doraise=True)


@pytest.mark.parametrize("name", MODULES)
def test_imports(name):
    importlib.import_module(name)


def test_cli_parser_builds_and_every_command_has_a_handler():
    from argus import cli
    p = cli.build_parser()
    sub = next(a for a in p._actions if a.dest == "cmd")
    for name, sp in sub.choices.items():
        assert sp.get_default("fn") is not None, name
