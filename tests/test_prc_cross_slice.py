"""PR C cross-slice pins (Next-Fifty wave 3 remainder): the pieces the three slices built separately agree."""

from __future__ import annotations

import ast

from app.logic import ledger_measure, unread_maintenance
from tests._source import read


def test_unread_maintenance_levers_are_measurable_by_the_manual_verify():
    """#30 books ESTIMATED rows the #46(d) measured verify must recognise as OBJECT-basis items."""
    kinds = set(unread_maintenance.ARM_FINDING_TYPE.values())
    assert kinds == set(ledger_measure.OBJECT_FINDING_TYPES)
    assert {ledger_measure.ledger_basis(k) for k in kinds} == {"OBJECT"}


def test_canary_names_stay_unique():
    tree = ast.parse(read("app/data/canary.py"))
    names = [n.elts[0].value for n in ast.walk(tree)
             if isinstance(n, ast.Tuple) and len(n.elts) == 2 and isinstance(n.elts[0], ast.Constant)
             and isinstance(n.elts[0].value, str) and "." in n.elts[0].value]
    assert names, "no canary entries parsed"
    dupes = sorted({x for x in names if names.count(x) > 1})
    assert not dupes, f"duplicate canary names: {dupes}"
    for new in ("cost.maintenance_on_unread", "cost.unread_maintenance_proof"):
        assert new in names, new
