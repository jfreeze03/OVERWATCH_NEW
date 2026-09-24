"""Next-Fifty #25 (v4.589.0): the cost-coverage gauge must match what the app can actually drill, in both
directions. Every drillable status names >=1 existing, page-wired builder; every "Service total only"
names none. (QAS and PIPE were wrongly 'service total only'; Snowflake Intelligence was wrongly
'Drill ready'; the AI-functions grain is function x model, not user.)"""

from __future__ import annotations

import importlib
from pathlib import Path

from app.logic import cost_coverage as cc
from tests.test_cost_coverage_taxonomy import KNOWN_AI_SERVICE_TYPES, KNOWN_NON_AI_SERVICE_TYPES

_ROOT = Path(__file__).resolve().parents[1]
_UI_SRC = "\n".join(p.read_text(encoding="utf-8") for p in (_ROOT / "app" / "ui").rglob("*.py"))


def test_every_drillable_status_names_a_wired_builder():
    sample = (list(KNOWN_NON_AI_SERVICE_TYPES) + list(KNOWN_AI_SERVICE_TYPES) + list(cc._DRILL_COVERAGE)
              + ["HYBRID_TABLE_REQUESTS", "MYSTERY_SERVICE"])
    for s in sample:
        status = cc._coverage_for(s)[2]
        builders = cc.drill_builders_for(s)
        if status in cc._DRILLABLE_STATUSES:
            assert builders, f"{s}: '{status}' but no backing builder"
            for ref in builders:
                mod, fn = ref.split(".", 1)
                assert callable(getattr(importlib.import_module(f"app.data.{mod}"), fn, None)), ref
            assert any(f"{ref}(" in _UI_SRC for ref in builders), f"{s}: no builder is wired on a page"
        elif status == "Service total only":
            assert builders == (), f"{s}: service-total-only must name no drill builder"


def test_corrected_labels():
    assert cc._coverage_for("PIPE")[2] == "Object-ledger drill"
    assert cc._coverage_for("SNOWPIPE")[2] == "Object-ledger drill"
    assert cc._coverage_for("QUERY_ACCELERATION")[2] == "Drill ready"
    assert cc._coverage_for("SNOWFLAKE_INTELLIGENCE")[2] == "Service total only"
    assert cc._coverage_for("SNOWPIPE_STREAMING")[2] == "Service total only"
    assert cc._coverage_for("SNOWFLAKE_COCO_SNOWSIGHT")[2] == "Drill ready"      # rec #48 preserved
    assert cc._coverage_for("AI_SERVICES")[2] == "Service total only"
    assert cc._coverage_for("CORTEX_FUNCTIONS")[0] == "Function / model"
    assert cc.service_category("SNOWFLAKE_INTELLIGENCE") == "AI / Cortex"          # pricing unchanged


def test_no_false_not_read_claim():
    src = (_ROOT / "app/logic/cost_coverage.py").read_text(encoding="utf-8")
    assert "is read anywhere" not in src and "no per-pipe drill wired" not in src
