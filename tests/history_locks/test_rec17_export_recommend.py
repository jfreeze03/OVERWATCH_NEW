"""CoCo Tier-2 (Sec #17): a RECOMMEND column on the auditor export sheets."""
from pathlib import Path

import pandas as pd

from app.logic.least_privilege import recommend_for_sheet

_ROOT = Path(__file__).resolve().parents[2]


def test_recommend_for_sheet_adds_action_column_to_actionable_sheets():
    out = recommend_for_sheet("unused_roles_90d", pd.DataFrame({"ROLE_NAME": ["OLD_ROLE"]}))
    assert next(iter(out.columns)) == "RECOMMEND"
    assert out["RECOMMEND"].iloc[0].startswith("REVOKE")
    out2 = recommend_for_sheet("dormant_users", pd.DataFrame({"USER_NAME": ["JDOE"]}))
    assert out2["RECOMMEND"].iloc[0].startswith("REVIEW")


def test_recommend_for_sheet_leaves_evidence_and_bad_frames_alone():
    frame = pd.DataFrame({"ROLE_NAME": ["R"]})
    assert recommend_for_sheet("role_privilege_matrix", frame) is frame     # evidence-only
    assert recommend_for_sheet("unused_roles_90d", pd.DataFrame()).empty     # empty
    err = pd.DataFrame({"ERROR": ["boom"]})
    assert "RECOMMEND" not in recommend_for_sheet("unused_roles_90d", err).columns


def test_export_pack_applies_the_recommendation():
    src = (_ROOT / "app" / "ui" / "pages" / "security.py").read_text(encoding="utf-8")
    assert "recommend_for_sheet(" in src
