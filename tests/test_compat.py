"""The package must parse on the oldest Python it claims to support."""

from __future__ import annotations

import ast
import pathlib
import re
import sys

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
MODULES = sorted(SRC.rglob("*.py"))


def _floor() -> tuple[int, int]:
    text = (SRC.parent / "pyproject.toml").read_text(encoding="utf-8")
    major, minor = re.search(r'requires-python = ">=(\d+)\.(\d+)"', text).groups()
    return int(major), int(minor)


def _expressions(line: str):
    """The balanced {...} parts of a line, outermost only.

    Brace matching rather than a regex, because the case that matters is an
    f-string nested inside another and a regex stops at the inner one.
    """
    line = line.replace("{{", "").replace("}}", "")
    depth, start = 0, None
    for index, char in enumerate(line):
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0:
                yield line[start : index + 1]


def test_the_declared_floor_is_what_we_think():
    assert _floor() == (3, 10)


@pytest.mark.skipif(sys.version_info[:2] != _floor(), reason="only meaningful on the floor")
@pytest.mark.parametrize("module", MODULES, ids=lambda p: p.name)
def test_every_module_parses(module):
    ast.parse(module.read_text(encoding="utf-8"), filename=str(module))


@pytest.mark.parametrize("module", MODULES, ids=lambda p: p.name)
def test_no_backslash_inside_an_f_string(module):
    """Legal from 3.12 (PEP 701), a SyntaxError before it.

    A newer interpreter reports nothing and ast.parse ignores feature_version
    here, so it is checked textually. Backslashes in the literal part are fine;
    only the expression part is illegal.
    """
    for number, line in enumerate(module.read_text(encoding="utf-8").splitlines(), 1):
        if not re.search(r"\bf['\"]", line):
            continue
        for expression in _expressions(line):
            assert "\\" not in expression, (
                f"{module.name}:{number} has a backslash inside an f-string "
                f"expression, which needs Python 3.12:\n    {line.strip()}"
            )
