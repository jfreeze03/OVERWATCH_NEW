"""Wave 2 adoption locks: entity-drilling (#25) and the audit-mode caption sweep (#1)."""
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_security_identity_tables_drill_to_entity_360():
    # #25: the four user-identity tables on Security now drill to Entity 360 instead of
    # rendering as an inert styled_table (failed_logins is included — its builder does
    # GROUP BY USER_NAME, one row per user, which the scoping skeptic caught).
    src = _src("app/ui/pages/security.py")
    for key in (
        'key=f"sec_mfa_{company}"',
        'key=f"sec_single_factor_{company}_{days}"',
        'key=f"sec_faillog_{company}_{days}"',
        'key=f"sec_reawakening_{company}"',
    ):
        assert key in src, key
    # each of the four (plus the pre-existing dormant-users table) is a USER drill
    assert src.count('key_col="USER_NAME", entity_type="USER"') >= 5


def test_overview_methodology_captions_are_audit_gated():
    # #1: the pure how-computed / provenance captions on Overview now render only in
    # audit mode via methodology_note; the primitive is imported.
    src = _src("app/ui/pages/overview.py")
    assert "    methodology_note,\n" in src  # imported in the components block
    assert src.count("methodology_note(") >= 5
    # The account-wide SCOPE disclaimer stays a plain caption — hiding it in operator
    # mode would strand the runway bar with no cue that it's account-wide (a
    # skeptic-caught over-reach, deliberately excluded from the sweep).
    assert 'st.caption("Whole-account contract commitment' in src
