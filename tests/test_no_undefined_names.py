"""Static guard against undefined names in the ``cassn`` package.

Most of the GUI is not exercised by the test suite — a mistyped or renamed
local only surfaces as a ``NameError`` in the field, mid-collection. Two of
those shipped when the site-identity refactor renamed ``reserve_code``/``site``
to ``site_short_name`` and missed three call sites, one of them on the
CONFIG.TXT rename path every AudioMoth hits.

``symtable`` classifies a name that a function reads but never binds as an
implicit global, so anything not defined at module level (or a builtin) is a
name that cannot resolve at runtime.
"""

import builtins
import symtable
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "cassn"
BUILTIN_NAMES = frozenset(dir(builtins))


def _source_files() -> list[Path]:
    return sorted(p for p in PACKAGE_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def _undefined_names(path: Path) -> list[str]:
    """Return ``"scope -> name"`` for every unresolvable global read in ``path``."""
    top = symtable.symtable(path.read_text(encoding="utf-8"), str(path), "exec")
    module_names = {sym.get_name() for sym in top.get_symbols()}
    findings: list[str] = []

    def walk(table, trail: list[str]) -> None:
        for child in table.get_children():
            if child.get_type() == "function":
                for sym in child.get_symbols():
                    # is_global() here means "not local and not a closure cell":
                    # the name must exist at module scope or as a builtin.
                    if not (sym.is_referenced() and sym.is_global()):
                        continue
                    name = sym.get_name()
                    if name not in module_names and name not in BUILTIN_NAMES:
                        findings.append(f"{'.'.join(trail + [child.get_name()])} -> {name}")
            walk(child, trail + [child.get_name()])

    walk(top, [])
    return findings


@pytest.mark.parametrize("path", _source_files(), ids=lambda p: p.name)
def test_module_has_no_undefined_names(path):
    findings = _undefined_names(path)
    assert not findings, f"undefined name(s) in {path.name}: " + "; ".join(findings)
